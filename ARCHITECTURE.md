# Architecture

Technical architecture documentation for discord-ext-voice-recv, covering the voice receive subsystem, video stream subsystem, protocol details, and integration design.

---

## 1. Voice Receive Subsystem

### 1.1 Overview

The voice receive subsystem (`discord.ext.voice_recv`) extends discord.py's VoiceClient to add inbound audio processing. It mirrors the AudioSource API: where AudioSource produces audio via a callback, AudioSink consumes it via a callback.

### 1.2 Connection Lifecycle

1. User calls `VoiceChannel.connect(cls=VoiceRecvClient)`.
2. `VoiceRecvClient.create_connection_state()` returns a `VoiceConnectionState` with the gateway hook.
3. The gateway hook (`hook()` in `gateway.py`) processes voice WebSocket opcodes:
   - Opcode 2 (READY): stores the bot user's SSRC
   - Opcode 4 (SESSION_DESCRIPTION): updates the PacketDecryptor's secret key
   - Opcode 5 (SPEAKING): maps SSRC to user ID, dispatches `voice_member_speaking_state`
   - Opcode 11 (CLIENT_CONNECT): dispatches `voice_member_connect`
   - Opcode 12 (VIDEO): maps SSRC, dispatches `voice_member_video`
   - Opcode 13 (CLIENT_DISCONNECT): destroys decoder, removes SSRC mapping
   - Opcode 18 (FLAGS): dispatches `voice_member_flags`
   - Opcode 20 (PLATFORM): dispatches `voice_member_platform`
4. User calls `vc.listen(sink)` which creates an `AudioReader` and starts the pipeline.

### 1.3 Audio Processing Pipeline

```
UDP Socket
    |
    v
AudioReader.callback(packet_data)
    |
    +-- RTP? --> PacketDecryptor.decrypt_rtp() --> RTPPacket
    |
    +-- RTCP? --> PacketDecryptor.decrypt_rtcp() --> RTCPPacket
    |
    v
PacketRouter.feed_rtp(packet) / feed_rtcp(packet)
    |
    +-- RTCP --> SinkEventRouter.dispatch('rtcp_packet')
    |
    +-- RTP --> PacketDecoder.push_packet(packet)
                    |
                    v
              HeapJitterBuffer (reordering)
                    |
                    v
              PacketDecoder.pop_data()
                    |
                    +-- decode opus (if sink wants PCM)
                    |
                    v
              sink.write(user, VoiceData)
```

### 1.4 Threading Model

The receive subsystem uses four threads:

- **AudioReader** (main callback thread): Receives UDP packets via `socket_listener`, decrypts, routes to PacketRouter
- **PacketRouter** (daemon thread): Pulls decoded packets from jitter buffer, calls `sink.write()`
- **SinkEventRouter** (daemon thread): Dispatches events (RTCP, speaking start/stop, member connect/disconnect) to sink listeners
- **SpeakingTimer** (daemon thread): Detects speaking start/stop from packet activity with a 200ms timeout
- **UDPKeepAlive** (daemon thread): Sends periodic keepalive packets every 5 seconds

### 1.5 Jitter Buffer

`HeapJitterBuffer` in `buffer.py` implements a heap-based reorder buffer:

- Packets are pushed with `heapq.heappush()` using the `_PacketCmpMixin` ordering (SSRC, sequence, timestamp)
- Packets older than 10000 sequence numbers from the last sent packet are dropped as stale
- A prefill mechanism waits for `prefill` packets before emitting, to allow reordering
- Packets are popped when the buffer has more than `prefill` items and the next sequence is ready
- The buffer signals the PacketRouter via a `MultiDataEvent` when data is available

### 1.6 Decryption

`PacketDecryptor` in `reader.py` supports four encryption modes:

- `xsalsa20_poly1305` -- Nonce from first 12 bytes of header
- `xsalsa20_poly1305_suffix` -- Nonce from last 24 bytes of payload
- `xsalsa20_poly1305_lite` -- Nonce from last 4 bytes of payload (counter mode)
- `aead_xchacha20_poly1305_rtpsize` -- Nonce from last 4 bytes (counter mode), header as AAD

For `aead_xchacha20_poly1305_rtpsize`, the packet layout includes the RTP extension header in the encrypted region (the `rtpsize` format). The `adjust_rtpsize()` method on RTPPacket handles extracting the nonce from the last 4 bytes and adjusting the data boundaries.

### 1.7 Inherited vs Modified Code (relative to upstream imayhaveborkedit)

The following files are identical to the upstream reference repository:

- `rtp.py`, `buffer.py`, `enums.py`, `types.py`, `utils.py`, `silence.py`, `video.py`, `router.py`
- `extras/__init__.py`, `extras/localplayback.py`

The following files have modifications for DM/group voice support (the `dm-voice` / `video-stream-port` branch changes):

- `__init__.py` -- Adds `from .patches import apply_patch` and calls `apply_patch()` at module load
- `gateway.py` -- All `vc.guild.get_member(uid)` calls have fallback: `vc.guild.get_member(uid) if vc.guild else vc.client.get_user(uid)`. Also changes `vc.guild.me.id` to `vc.user.id` for the bot user's SSRC.
- `opus.py` -- `_get_user()` uses the same guild-safety pattern
- `reader.py` -- `SpeakingTimer._lookup_member()` uses guild-safety pattern
- `sinks.py` -- `SilenceGeneratorSink.on_voice_member_disconnect` type annotation changed to `Union[discord.User, discord.Member]`
- `voice_client.py` -- Log message handles `self.guild` being None
- `extras/speechrecognition.py` -- Type annotation updated for DM support

Fork-only additions:

- `patches.py` -- Monkey-patches `commands.Context.voice_client` to resolve voice clients in DM/group channels where `ctx.guild` is None
- `py.typed` -- PEP 561 marker file for type checking support

---

## 2. Video Stream Subsystem

### 2.1 Overview

The video stream subsystem (`discord_video_stream`) implements Discord Go Live streaming. It sends H.264 video and Opus audio through a separate voice WebSocket connection to a stream server, using discord.py's existing VoiceClient infrastructure for the UDP socket and DAVE session management.

### 2.2 Architecture

```
VideoStreamer (orchestrator)
    |
    +-- Gateway events (STREAM_CREATE, STREAM_SERVER_UPDATE)
    |
    +-- StreamConnection (separate voice WebSocket to stream server)
    |       |
    |       +-- SSRCs (audio, video, rtx)
    |       +-- Secret key
    |       +-- DAVE session (separate from main voice)
    |
    +-- FFmpegProcess (transcoding pipeline)
    |       |
    |       +-- NUT pipe stdout
    |
    +-- Demuxer (PyAV NUT container parsing)
    |       |
    |       +-- VideoFrame / AudioFrame
    |
    +-- FramePacer (timing + A/V sync)
    |
    +-- VideoSender / AudioSender
            |
            +-- SPS VUI rewrite
            +-- DAVE frame encryption
            +-- H.264 / Opus RTP packetization
            +-- Transport encryption (AEAD)
            +-- UDP sendto (stream server endpoint)
```

### 2.3 Go Live Lifecycle

1. **Join Voice**: `VideoStreamer.join_voice(guild_id, channel_id)` calls `channel.connect()` via discord.py, establishing the main voice connection.

2. **Start Go Live**: `VideoStreamer.start_go_live()`:
   - Sends STREAM_CREATE (gateway opcode 18) with `{type: "guild", guild_id, channel_id, preferred_region: null}`
   - Sends STREAM_SET_PAUSED (gateway opcode 22) with `{stream_key, paused: false}`
   - Waits for STREAM_CREATE event (extracts `rtc_server_id` and `stream_key`)
   - Waits for STREAM_SERVER_UPDATE event (extracts `endpoint` and `token`)
   - Creates `StreamConnection` and connects to the stream voice server

3. **Stream Handshake**: `StreamConnection.connect()`:
   - Opens WebSocket to `wss://{endpoint}/?v=9`
   - Server sends HELLO (opcode 8) with heartbeat interval
   - Client sends IDENTIFY (opcode 0) with `server_id`, `user_id`, `session_id`, `token`, `video: true`, `streams: simulcast`
   - Server sends READY (opcode 2) with `ssrc`, `ip`, `port`, `modes`, `streams[]`
   - Client sends SELECT_PROTOCOL (opcode 1) with UDP transport, codec configs, encryption mode
   - Server sends SELECT_PROTOCOL_ACK (opcode 4) with `secret_key`, `mode`, `dave_protocol_version`
   - DAVE key exchange if `dave_protocol_version > 0`

4. **Play**: `VideoStreamer.play(url)`:
   - Starts FFmpeg with the input URL, outputting NUT format to stdout
   - Creates Demuxer to extract video/audio frames from the NUT pipe
   - Creates VideoSender and AudioSender with the stream connection's SSRCs and secret key
   - Creates FramePacers for video and audio with sync linkage
   - Starts send loop: demux frame -> pace -> packetize -> encrypt -> send

5. **Stop**: `VideoStreamer.stop()`:
   - Cancels send loop and RTCP task
   - Stops senders
   - Sends STREAM_DELETE (gateway opcode 19)
   - Stops stream connection

6. **Leave**: `VideoStreamer.leave()`:
   - Calls stop()
   - Sends VOICE_STATE_UPDATE to leave channel
   - Disconnects voice client

### 2.4 Stream Key Format

Stream keys identify Go Live streams:

- Guild: `guild:{guild_id}:{channel_id}:{user_id}`
- Call/DM: `call:{channel_id}:{user_id}`

### 2.5 Key Invariants

- The stream connection is separate from the main voice connection (different WebSocket, different SSRCs, different secret key, potentially different DAVE session)
- `serverId` = `guild_id` for guild channels, `channel_id` for DM/call
- `daveChannelId` = `serverId - 1` (BigInt arithmetic, confirmed from Node.js reference)
- Speaking mode = 2 (priority/soundshare) for Go Live, not 1 (normal speaking) which is for camera streams
- Video packets must go to the STREAM server's endpoint (from READY `ip`/`port`), NOT the main voice connection's endpoint. This was the root cause of error 2012.
- DAVE encrypt operates on the COMPLETE frame BEFORE RTP packetization
- Video and audio use separate SSRC, sequence numbers, timestamps, and nonce counters

---

## 3. Protocol Details

### 3.1 Voice WebSocket Opcodes

Opcodes 0-20 and 21-31 (DAVE):

- 0: IDENTIFY -- Credentials, video flag, simulcast streams, max DAVE version
- 1: SELECT_PROTOCOL -- UDP transport, codec configs, encryption mode preference
- 2: READY -- SSRCs, IP, port, encryption modes, simulcast streams
- 3: HEARTBEAT -- Timestamp and sequence acknowledgment
- 4: SELECT_PROTOCOL_ACK / SESSION_DESCRIPTION -- Secret key, encryption mode, DAVE version
- 5: SPEAKING -- Speaking bitmask (0=none, 1=normal, 2=priority/soundshare)
- 6: HEARTBEAT_ACK
- 7: RESUME
- 8: HELLO -- Heartbeat interval
- 9: RESUMED
- 11: CLIENT_CONNECT -- User IDs of connected members
- 12: VIDEO -- Audio/video SSRCs, stream attributes (resolution, framerate)
- 13: CLIENT_DISCONNECT
- 14: SESSION_UPDATE
- 18: FLAGS -- Member voice flags
- 20: PLATFORM -- Member platform
- 21: DAVE_PREPARE_TRANSITION
- 22: DAVE_EXECUTE_TRANSITION
- 23: DAVE_TRANSITION_READY
- 24: DAVE_PREPARE_EPOCH
- 31: MLS_INVALID_COMMIT_WELCOME

DAVE binary opcodes (25-30):

- 25: MLS_EXTERNAL_SENDER
- 26: MLS_KEY_PACKAGE
- 27: MLS_PROPOSALS
- 28: MLS_COMMIT_WELCOME
- 29: MLS_ANNOUNCE_COMMIT_TRANSITION
- 30: MLS_WELCOME

Binary message format (server-to-client): `[2-byte sequence BE][1-byte opcode][payload]`
Binary message format (client-to-server): `[1-byte opcode][payload]`

### 3.2 Gateway Opcodes for Stream Lifecycle

- VOICE_STATE_UPDATE (4) -- Join/leave voice channel
- STREAM_CREATE (18) -- Request Go Live stream creation
- STREAM_DELETE (19) -- Delete Go Live stream
- STREAM_SET_PAUSED (22) -- Unpause/pause stream

### 3.3 Codec Payload Types

Hardcoded payload types (matching the Node.js reference):

- Opus: PT 120, clock rate 48000
- H.264: PT 101, RTX PT 102, clock rate 90000
- H.265: PT 103, RTX PT 104, clock rate 90000
- VP8: PT 105, RTX PT 106, clock rate 90000
- VP9: PT 107, RTX PT 108, clock rate 90000
- AV1: PT 109, RTX PT 110, clock rate 90000

### 3.4 RTP Packet Format

RTP header layout (12 bytes fixed):

- Byte 0: V(2) P(1) X(1) CC(4) -- Version=2, no padding, no extension, CC=0
- Byte 1: M(1) PT(7) -- Marker bit + payload type
- Bytes 2-3: Sequence number (16-bit, wraps at 65536)
- Bytes 4-7: Timestamp (32-bit, wraps at 2^32)
- Bytes 8-11: SSRC (32-bit)

The marker bit is set on the last RTP packet of each video frame (access unit). For audio, it is typically False.

The header format matches voice-recv's parsing: `struct.Struct('>xxHII')` reads sequence, timestamp, and ssrc starting at byte 2.

### 3.5 H.264 Packetization

NAL unit types relevant to packetization:

- Type 1: Non-IDR coded slice (P-frame)
- Type 5: IDR coded slice (keyframe)
- Type 6: SEI
- Type 7: SPS (Sequence Parameter Set)
- Type 8: PPS (Picture Parameter Set)
- Type 9: AUD (Access Unit Delimiter)

Annex-B frames are split into NALUs by scanning for start codes (3-byte `0x000001` or 4-byte `0x00000001`). Each NALU is either sent as a single RTP packet (if <= 1300 bytes) or fragmented using FU-A (if larger).

FU-A format:

- FU indicator byte: `(nalu[0] & 0x60) | 28` -- preserves NRI bits, type 28 = FU-A
- FU header byte: bit 7 = start, bit 6 = end, bits 4-0 = original NAL type
- Maximum fragment payload: 1298 bytes (1300 - 2 for FU indicator and header)

### 3.6 SPS VUI Rewriting

The SPS VUI rewriter in `protocol/vui.py` modifies H.264 Sequence Parameter Set NALUs for Discord compatibility. This is a port of WebRTC's C++ `sps_vui_rewriter.cc` (via the TypeScript port in the Node.js reference).

Changes applied to SPS:

- Force `bitstream_restriction_flag = 1`
- Force `max_num_reorder_frames = 0` (no B-frame reordering, matching the `-bf 0` FFmpeg flag)
- Set `max_dec_frame_buffering = max_num_ref_frames`
- Strip `video_signal_type` information (set present flag to 0)

The rewriter handles both cases: SPS with existing VUI (parses and modifies) and SPS without VUI (injects a minimal VUI with just bitstream restriction). High profile SPS fields (profile_idc in {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 144}) include extra fields after profile_idc (chroma_format_idc, bit_depth, scaling lists) that are copied verbatim.

The `BitstreamReader` and `BitstreamWriter` classes implement Exp-Golomb coding (unsigned and signed) with emulation prevention byte handling (skipping/inserting `0x03` in `0x000003` sequences).

---

## 4. Cryptography

### 4.1 Voice Receive Decryption

`PacketDecryptor` in `reader.py` decrypts incoming voice RTP packets using pynacl. The `aead_xchacha20_poly1305_rtpsize` mode (the current standard):

- Creates `nacl.secret.Aead(secret_key)`
- Nonce: 24 bytes, first 4 bytes = counter from packet suffix, remaining 20 bytes = zero
- AAD: RTP header bytes (including extension header if present, per `rtpsize` format)
- The nonce counter is extracted from the last 4 bytes of the packet data

### 4.2 Video Stream Transport Encryption

`TransportEncryptor` in `rtp/crypto.py` encrypts outgoing RTP packets. It is the reverse of voice-recv's decryptor:

- Creates `nacl.secret.Aead(secret_key)`
- Nonce: 24 bytes, first 4 bytes = incremental counter (BE uint32), remaining 20 bytes = zero
- AAD: the 12-byte RTP header
- Output: `encrypted_payload + 4-byte_nonce_counter`
- Counter wraps at 2^32

### 4.3 DAVE (Discord Audio Video Encryption)

DAVE is Discord's end-to-end encryption protocol, mandatory since March 1, 206. It operates at the frame level: complete encoded frames are encrypted BEFORE RTP packetization.

The DAVE protocol uses MLS (Messaging Layer Security) for key exchange:

1. Client sends MLS_KEY_PACKAGE with its serialized key package
2. Server sends MLS_EXTERNAL_SENDER with the external sender's public key
3. Server sends MLS_PROPOSALS with Add proposals for pending members
4. Client processes proposals, generates commit + optional welcome
5. Server broadcasts MLS_ANNOUNCE_COMMIT_TRANSITION or MLS_WELCOME
6. Clients execute the transition, establishing shared encryption keys

Frame encryption via `dave_session.encrypt(media_type, codec, frame)`:

- For video: `encrypt(MediaType.video, Codec.h264, frame_bytes)`
- For audio: `encrypt_opus(frame_bytes)`
- When DAVE is not ready or during transitions, frames pass through unencrypted (passthrough mode)

The stream connection maintains its own DAVE session, separate from the main voice connection's DAVE session. The stream's DAVE channel ID is computed as `serverId - 1`.

---

## 5. FFmpeg Integration

### 5.1 Command Construction

The FFmpeg process in `media/ffmpeg.py` constructs commands with these critical flags:

- `-bf 0` -- No B-frames (essential for low latency, B-frames require future reference frames)
- `-preset superfast` -- NOT ultrafast (ultrafast causes bitrate spikes)
- `-tune film` -- Optimizes for live-action content
- `-forced-idr 1` -- Every keyframe is an IDR frame (ensures decoder recovery)
- `-force_key_frames expr:gte(t,n_forced*1)` -- IDR keyframe every 1 second (required by Discord's SFU)
- `-pix_fmt yuv420p` -- Only 4:2:0 chroma subsampling supported by Discord
- `-bufsize:v {bitrate/2}k` -- VBV buffer at half the average bitrate
- `-f nut pipe:1` -- NUT container format to stdout

Audio encoding: libopus, stereo, 48kHz, configurable bitrate.

Input handling: URLs with `lavfi:` prefix are converted to `-f lavfi -i <filter>` format (FFmpeg does not accept `lavfi:` as a URL protocol).

### 5.2 NUT Demuxing

The `Demuxer` in `media/demux.py` uses PyAV to read NUT format from FFmpeg's stdout pipe:

1. `_wrap_pipe()` duplicates the asyncio StreamReader's file descriptor and sets it to blocking mode (required by PyAV's synchronous API)
2. `av.open()` opens the pipe with `format='nut'` and `buffer_size=8192`
3. `container.demux()` iterates packets synchronously
4. Packets are yielded as `MediaFrame` objects with type, data, PTS, duration, and keyframe flag
5. `asyncio.sleep(0)` is called every 8 frames to yield control to the event loop

The Opus duration parser (`parse_opus_duration()`) reads the TOC byte per RFC 6716 to determine frame duration when the container does not provide accurate duration info.

Keyframe detection uses `packet.is_keyframe` from PyAV, with a fallback that scans for IDR NALUs (type 5) in the Annex-B data.

---

## 6. Frame Pacing and A/V Sync

### 6.1 Timing Algorithm

`FramePacer` in `media/pacer.py` controls frame send timing:

1. On first frame: record `start_time` (wall clock via `time.monotonic()`) and `start_pts` (presentation timestamp in ms)
2. For each frame: `sleep_ms = (pts - start_pts + frametime) - (now - start_time)`
3. If `sleep_ms > 0`: sleep for that duration
4. If behind sync partner (delta < -tolerance): skip sleep, reset timing
5. If ahead of sync partner (delta > +tolerance): wait for partner to catch up using an `asyncio.Event` notification

### 6.2 Sync Partners

Video and audio FramePacers are linked via the `sync_partner` property. When video is ahead of audio, it waits; when behind, it catches up. The sync tolerance defaults to 20ms.

Circular sync partnerships are prevented: setting `pacer_b.sync_partner = pacer_a` when `pacer_a.sync_partner = pacer_b` raises `ValueError`.

The `update_pts()` method sets the current PTS and signals the partner via an `asyncio.Event`, avoiding busy-wait polling.

### 6.3 RTCP Sender Reports

`VideoSender.send_rtcp_sender_report()` constructs and sends RTCP Sender Reports (type 200) containing:

- SSRC of the sender
- NTP timestamp (seconds since 1900-01-01 epoch)
- RTP timestamp
- Total packet count
- Total octet count

Sender Reports are sent periodically (every 5 seconds) by a background task in `VideoStreamer`. They allow receivers to correlate NTP time with RTP timestamps for A/V synchronization.

---

## 7. Gateway Integration

### 7.1 Gateway Event Hooking

`VideoStreamer` hooks into the Discord gateway by chaining onto the `on_socket_raw_receive` handler. It intercepts:

- STREAM_CREATE (event) -- Extracts `rtc_server_id` and `stream_key`
- STREAM_SERVER_UPDATE (event) -- Extracts `endpoint` and `token`
- VOICE_STATE_UPDATE (event) -- Captures `session_id`
- VOICE_SERVER_UPDATE (event) -- No-op (handled by discord.py internally)

The gateway WebSocket is patched via `_ensure_gateway_dispatch()` to always dispatch `socket_raw_receive`, because dpy-self only fires this event when `_enable_debug_events` is True.

### 7.2 Main Gateway Sends

Opcodes sent to the MAIN gateway (not the voice WebSocket):

- STREAM_CREATE (18) -- `{type, guild_id, channel_id, preferred_region}`
- STREAM_SET_PAUSED (22) -- `{stream_key, paused}`
- STREAM_DELETE (19) -- `{stream_key}`
- VOICE_STATE_UPDATE (4) -- `{guild_id, channel_id, self_mute, self_deaf, self_video}`

### 7.3 Voice WebSocket Sends

Opcodes sent to the STREAM voice WebSocket:

- IDENTIFY (0) -- `{server_id, user_id, session_id, token, video, streams, max_dave_protocol_version}`
- SELECT_PROTOCOL (1) -- `{protocol: "udp", data: {address, port, mode}, codecs, rtc_connection_id}`
- SPEAKING (5) -- `{delay: 0, speaking: 2, ssrc}` (mode 2 = priority/soundshare)
- VIDEO (12) -- `{audio_ssrc, video_ssrc, rtx_ssrc, streams: [{type, rid, ssrc, active, quality, ...}]}`
- HEARTBEAT (3) -- `{t: timestamp_ms, seq_ack: last_sequence}`

---

## 8. Compatibility Layer

### 8.1 VoiceSendRecvClient

When both `discord.ext.voice_recv` and `discord_video_stream` are installed, `discord_video_stream.compat.voice_recv` provides `VoiceSendRecvClient`:

- Extends `VoiceRecvClient` (inheriting all receive functionality)
- Adds `set_stream_components(stream_conn, video_sender)` for attaching stream components
- Adds `send_video_packet(packet, ip, port)` for sending video RTP over the shared UDP socket
- The UDP socket and DAVE session are shared between send and receive paths

When `discord.ext.voice_recv` is NOT installed, a stub class raises `ImportError` on instantiation.

### 8.2 DM/Group Voice Support

The fork adds DM/group voice support via:

- `patches.py` -- Monkey-patches `commands.Context.voice_client` to find voice clients in DM channels where `ctx.guild` is None, by iterating `ctx.bot.voice_clients` and matching channel IDs
- Gateway hook changes -- All `guild.get_member()` calls have fallback to `client.get_user()` when `guild` is None
- `VoiceRecvClient` SSRC lookup uses `vc.user.id` instead of `vc.guild.me.id` for the bot's own SSRC

---

## 9. Test Infrastructure

### 9.1 Voice Receive Tests

The voice receive subsystem has no dedicated test files in this repository. Testing is done via the example script (`examples/recv.py`) and manual verification.

### 9.2 Video Stream Tests

The video stream subsystem has comprehensive unit and integration tests:

- `test_h264.py` -- NALU splitting, FU-A fragmentation, packetizer correctness (18 tests)
- `test_rtp_crypto.py` -- RTP header construction, transport encryption round-trip, RTCP SR format (18 tests)
- `test_protocol.py` -- Codec configs, stream keys, VIDEO payload, bitstream reader/writer, SPS VUI rewriter (31 tests)
- `test_pacer.py` -- Frame pacing timing, sync behavior, no-sleep mode (14 tests)
- `test_ffmpeg_demux.py` -- FFmpeg command construction, Opus duration parsing, FFmpeg subprocess lifecycle, NUT demuxing pipeline (21 tests)
- `test_video_sender.py` -- VideoSender init/start/stop, frame pipeline, SPS VUI integration, DAVE passthrough, endpoint routing, RTCP SR (20 tests)
- `test_stream_connection.py` -- StreamConnection state, READY handling, SELECT_PROTOCOL_ACK, speaking mode, VIDEO opcode, binary messages, IDENTIFY, DAVE transitions (35 tests)
- `test_streamer.py` -- Gateway opcodes, stream key generation, payload construction, event parsing (26 tests)
- `test_integration.py` -- Full send pipeline end-to-end (packetize -> encrypt -> decrypt -> verify) (4 tests)
- `test_phase6_integration.py` -- Package exports, StreamConnection+VideoSender wiring, protocol round-trips, SPS VUI profiles, frame pacing, DAVE integration, voice-recv compatibility (39 tests)
- `test_pipeline_real.py` -- Real FFmpeg + PyAV pipeline with actual media files (requires test media, skipped by default)

All tests run with `python -m pytest` from the `discord_video_stream/tests/` directory, or individually with `python test_*.py`.

---

## 10. Dependencies

- `discord.py-self[voice]` >=2.1 -- Gateway, voice WebSocket, DAVE (davey), UDP socket
- `websockets` >=12.0 -- Go Live stream voice WebSocket (independent of dpy-self's curl_cffi)
- `av` (PyAV) >=12.0 -- NUT container demuxing from FFmpeg pipe
- `pynacl` >=1.5 -- Transport encryption (AEAD). Also required by discord.py for voice.
- `davey` >=0.1.0 -- DAVE/E2EE protocol. Bundled with discord.py-self[voice].
- `FFmpeg` (system) -- Video/audio transcoding

Not required:

- `dave.py` (DisnakeDev) -- discord.py uses davey, not dave.py
- WebRTC libraries -- The implementation uses the UDP path directly
- Separate DAVE implementation -- davey handles everything
