# Context: Implementation Knowledge, Protocol Research, and Port Reference

Exhaustive domain knowledge, reference correspondence, architectural decisions, and protocol research for the discord-video-stream Python port. This document serves as the dev interface for understanding the port: what maps to what, why decisions were made, what constraints exist, and where the bodies are buried.

---

## 1. Reference Correspondence: Node.js to Python Line Mapping

### 1.1 File-Level Mapping with Line Counts

Node.js source (4,319 lines total) to Python implementation (4,662 lines, excluding 4,247 lines of tests):

**Ported with direct correspondence:**

| Node.js File | Lines | Python File | Lines | Notes |
|---|---|---|---|---|
| `voice/BaseMediaConnection.ts` | 640 | `stream_connection.py` | 980 | Core WS lifecycle. Python adds async/await, reconnection, guild-safety |
| `voice/WebRtcWrapper.ts` | 213 | `voice_send.py` | 533 | Node.js delegates to node-datachannel; Python implements manually |
| `voice/StreamConnection.ts` | 38 | `stream_connection.py` | (merged) | Merged into StreamConnection class |
| `voice/CodecPayloadType.ts` | 59 | `protocol/types.py` | 90 | Identical values |
| `voice/VoiceOpCodes.ts` | 36 | `stream_connection.py` | 28-56 | Identical enum values |
| `client/Streamer.ts` | 259 | `streamer.py` | 714 | Python adds gateway dispatch patching, FFmpeg management |
| `media/BaseMediaStream.ts` | 198 | `media/pacer.py` | 219 | Algorithm identical; Python uses asyncio.Event for sync |
| `media/VideoStream.ts` | 17 | `voice_send.py` | (merged) | Merged into VideoSender.send_frame() |
| `media/AudioStream.ts` | 18 | `voice_send.py` | (merged) | Merged into AudioSender.send_frame() |
| `media/LibavDemuxer.ts` | 291 | `media/demux.py` | 372 | Node.js uses node-av; Python uses PyAV |
| `media/newApi.ts` | 655 | `media/ffmpeg.py` | 271 | Node.js uses fluent-ffmpeg + ZMQ; Python uses subprocess |
| `processing/SPSVUIRewriter.ts` | 332 | `protocol/vui.py` | 547 | Python more verbose due to no compact bit ops |
| `processing/AnnexBBitstreamReaderWriter.ts` | 148 | `protocol/vui.py` | (merged) | Merged into BitstreamReader/BitstreamWriter classes |
| `processing/AnnexBHelper.ts` | 134 | `rtp/h264.py` | 102 | split_nalu + get_nalu_type only |
| `utils.ts` | 101 | `protocol/types.py` | 204 | Stream key, simulcast, encryption modes |

**Not ported (handled differently or not needed):**

| Node.js File | Lines | Reason |
|---|---|---|
| `voice/VoiceConnection.ts` | 19 | Python uses StreamConnection directly |
| `voice/VoiceMessageTypes.ts` | 263 | TypeScript type definitions, not needed |
| `media/LibavCodecId.ts` | 564 | PyAV handles codec IDs internally |
| `media/LibavDecoder.ts` | 47 | Stream preview decoder, not implemented |
| `media/encoders/` | 184 | FFmpeg encoder presets (nvenc, vaapi, software) |
| `client/GatewayEvents.ts` | 48 | TypeScript type definitions |

**Added new in Python (not in Node.js):**

| Python File | Lines | Purpose |
|---|---|---|
| `rtp/serialize.py` | 144 | Manual RTP header construction (Node.js uses node-datachannel) |
| `rtp/h264.py` | 226 | Manual H.264 NALU splitting + FU-A (Node.js uses node-datachannel) |
| `rtp/crypto.py` | 111 | Manual AEAD transport encryption (Node.js uses WebRTC DTLS-SRTP) |
| `compat/voice_recv.py` | 140 | discord-ext-voice-recv integration (no Node.js equivalent) |

### 1.2 Function-Level Correspondence

**BaseMediaConnection.ts -> stream_connection.py:**

- `identify()` L294-305 -> `identify()` L893-920: IDENTIFY payload construction. Python adds `channel_id` field (v9).
- `resume()` L307-316 -> `resume()` L937-960: RESUME payload. Python adds `channel_id` (v9).
- `handleReady(d)` L175-186 -> `_handle_ready(data)` L407-437: Extract SSRCs from streams array. Logic identical.
- `handleProtocolAck(d)` L188-255 -> `_handle_select_protocol_ack(data)` L461-480: Extract secret_key + mode. Node.js parses SDP; Python reads UDP fields directly.
- `initDave()` L257-279 -> `_init_dave()` L482-520: DAVE session init/reinit, MLS key package send. Logic identical.
- `setupEvents()` L318-406 -> `_handle_json_message(data)` L251-340: WS message dispatch. Logic identical, Python adds logging.
- `handleBinaryMessages(msg)` L408-455 -> `_handle_binary_message(msg)` L342-370: DAVE binary opcodes. Binary format parsing identical.
- `setupHeartbeat(interval)` L457-469 -> `_heartbeat_loop(interval)` L534-545: setInterval -> asyncio.sleep loop.
- `setProtocols()` L276-315 -> `_send_select_protocol()` L439-460: Node.js sends WebRTC SDP; Python sends UDP address/port.
- `setVideoAttributes(enabled, attr)` L317-360 -> `set_video_attributes(enabled, attrs)` L612-645: VIDEO opcode payload. Structure identical.
- `setSpeaking(speaking)` L365-373 -> `set_speaking(speaking)` L597-611: SPEAKING opcode. Node.js default=1, StreamConnection override=2. Python always=2.
- `executePendingTransition(id)` L280-295 -> `_execute_pending_transition(id)` L559-581: DAVE transition execution. Logic identical.
- `processInvalidCommit(id)` L229-236 -> `_process_invalid_commit(id)` L522-529: MLS invalid commit recovery. Logic identical.

**WebRtcWrapper.ts -> voice_send.py:**

- `sendVideoFrame(frame, frametime)` L89-126 -> `VideoSender.send_frame(frame, pts_ms, frametime_ms)` L231-290: THE critical function. Node.js: SPS rewrite -> DAVE encrypt -> `sendMessageBinary()` (WebRTC handles RTP). Python: SPS rewrite -> DAVE encrypt -> RTP packetize -> transport encrypt -> UDP send.
- `sendAudioFrame(frame, frametime)` L77-87 -> `AudioSender.send_frame(frame, pts_ms, frametime_ms)` L429-477: Node.js: DAVE encrypt -> `sendMessageBinary()`. Python: DAVE encrypt -> RTP build -> transport encrypt -> UDP send.
- `setPacketizer(videoCodec)` L128-175 -> `VideoSender.start()` L149-197: Node.js creates H264RtpPacketizer + RtcpSrReporter + RtcpNackResponder + PacingHandler chain. Python creates H264Packetizer + TransportEncryptor.

**Streamer.ts -> streamer.py:**

- `joinVoice(guild_id, channel_id)` L55-86 -> `join_voice(guild_id, channel_id)` L282-318: Gateway event registration for VOICE_STATE_UPDATE/VOICE_SERVER_UPDATE. Python uses discord.py's channel.connect().
- `createStream()` L88-130 -> `start_go_live()` L320-407: STREAM_CREATE + STREAM_SERVER_UPDATE event handling. Logic identical.
- `stopStream()` L132-143 -> `stop()` L451-505: STREAM_DELETE send + cleanup. Python adds FFmpeg stop, pacer cleanup.
- `leaveVoice()` L145-153 -> `leave()` L507-535: VOICE_STATE_UPDATE (guild_id=null) + disconnect.
- `signalStream()` L155-173 -> `_send_gateway()` L249-265: Gateway opcode send. Python chains onto on_socket_raw_receive.
- `playStream()` (newApi.ts L341-500) -> `play()` L409-449 + `_send_loop()` L451-505: Node.js pipes demuxer to VideoStream/AudioStream writable streams. Python uses async for loop with pacer.

**BaseMediaStream.ts -> media/pacer.py:**

- `_write(frame, _, callback)` L97-160 -> `pace(pts_ms, frametime_ms)` L124-190: Timing formula identical: `sleep = pts - startPts + frametime - (now - startTime)`. Node.js uses setTimeout callback; Python uses asyncio.sleep.
- `isAhead()` / `isBehind()` L70-84 -> `_is_ahead()` / `_is_behind()` L196-212: Delta check against sync tolerance. Logic identical.
- `resetTimingCompensation()` L85-87 -> `reset_timing()` L107-109: Reset start_time/start_pts.

**SPSVUIRewriter.ts -> protocol/vui.py:**

- `rewriteSPSVUI(buffer)` L10-332 -> `rewrite_sps_vui(nalu)` L252-420: Complete SPS parse + rewrite + write. Logic identical. Python more verbose due to no compact bit manipulation.
- `addBitstreamRestriction()` L263-280 -> `_write_bitstream_restriction(writer, max_num_ref_frames)` L476-490: Write motion_vectors, max_bytes, max_bits, log2_mv, max_reorder=0, max_buffering=max_ref.
- `AnnexBBitstreamReader` L1-76 -> `BitstreamReader` L66-148: Exp-Golomb read with emulation prevention. Logic identical.
- `AnnexBBitstreamWriter` L78-148 -> `BitstreamWriter` L151-249: Exp-Golomb write with emulation prevention. Logic identical.

**AnnexBHelper.ts -> rtp/h264.py:**

- `splitNalu(buf)` L68-81 -> `split_nalu(frame)` L43-102: Find start codes, split. Node.js returns Buffer array; Python returns bytes list.
- `H264Helpers.getUnitType(frame)` L56-58 -> `get_nalu_type(nalu)` L105-110: `frame[0] & 0x1F`. Identical.
- `H264NalUnitTypes` enum L3-19 -> `H264NalUnitTypes` class L37-45: Identical values.

**LibavDemuxer.ts -> media/demux.py:**

- `demux(input, {format})` L87-291 -> `demuxer.demux(pipe)` L212-340: Node.js uses node-av Demuxer + PassThrough streams + bitstream filters. Python uses PyAV av.open() + synchronous iteration with asyncio.sleep(0) yield.
- `parseOpusPacketDuration(frame)` L60-85 -> `parse_opus_duration(frame)` L300-335: TOC byte parsing per RFC 6716. Logic identical. Python returns samples (multiplied by 48 for 48kHz).

---

## 2. Architectural Decisions and Rationale

### 2.1 Why UDP Instead of WebRTC

**Decision**: Use raw UDP for media transport instead of WebRTC.

**Rationale**: Python's WebRTC libraries (aiortc, etc.) are immature compared to Node.js's node-datachannel. The UDP path avoids the entire WebRTC dependency chain (ICE negotiation, DTLS handshake, SDP exchange). Discord's voice docs explicitly support UDP and state that for send-only connections, the address/port can be randomized.

**Trade-off**: All RTP packetization, pacing, and RTCP handling must be implemented manually. The Node.js reference delegates all of this to node-datachannel's built-in H264RtpPacketizer, PacingHandler, and RtcpSrReporter.

**Net effect**: ~480 lines of new code (rtp/serialize.py + rtp/h264.py + rtp/crypto.py) that doesn't exist in Node.js, but eliminates the WebRTC dependency chain.

### 2.2 Why discord.py VoiceClient as Base

**Decision**: Build on top of discord.py's VoiceClient rather than implementing the voice protocol from scratch.

**Rationale**: discord.py already handles ~60% of the protocol layer: gateway connection, voice WS handshake, DAVE/MLS key exchange (via davey), UDP socket, secret key storage, SSRC assignment, heartbeat, reconnection. Building a standalone implementation would duplicate ~1,500 lines of protocol handling.

**Trade-off**: Tightly coupled to discord.py's internal API (_connection, socket, endpoint_ip, voice_port). These are not part of discord.py's public API and may change between versions.

### 2.3 Why davey Instead of dave.py

**Decision**: Use davey (Snazzah's Rust implementation) for DAVE, not dave.py (DisnakeDev's C++ bindings).

**Rationale**: discord.py v2.7+ explicitly depends on davey. Using dave.py would create a conflicting dependency. davey is bundled with discord.py[voice].

### 2.4 Why websockets Library for Stream WS

**Decision**: Use the `websockets` Python library for the stream voice WebSocket, not dpy-self's internal transport (curl_cffi).

**Rationale**: dpy-self replaces aiohttp with curl_cffi for all WebSocket connections. Using curl_cffi directly would tie the implementation to dpy-self's internals. The `websockets` library is independent, async-native, and avoids coupling.

**Trade-off**: The stream WS and the main voice WS use different WebSocket implementations. This is fine because they are completely independent connections.

### 2.5 Why PyAV for Demuxing

**Decision**: Use PyAV for NUT container demuxing.

**Rationale**: PyAV is the most mature Python binding to FFmpeg's libav. It supports NUT format directly and provides packet-level access with PTS, duration, and keyframe information.

**Trade-off**: PyAV's API is synchronous, which blocks the asyncio event loop. Mitigated by asyncio.sleep(0) every 8 frames. A raw Annex-B pipe approach would avoid this but requires manual frame delimiting.

### 2.6 Why NUT Container

**Decision**: Use NUT format for FFmpeg pipe output, not Matroska.

**Rationale**: NUT has lower framing overhead than Matroska, reducing initial buffering delay. It's designed for streaming with no seek table or index. Both Node.js and Python use NUT.

---

## 3. Protocol Constraints and Invariants

### 3.1 Transport Layer

- Wire format: `[12-byte RTP header][encrypted payload][4-byte nonce counter]`
- RTP header is AAD (authenticated but not encrypted)
- Nonce: 24 bytes for XChaCha20 (4 counter + 20 zero), 12 bytes for AES-GCM (4 counter + 8 zero)
- Counter wraps at 2^32
- Must support `aead_xchacha20_poly1305_rtpsize` (required)
- Should prefer `aead_aes256_gcm_rtpsize` when available (hardware AES-NI)

### 3.2 H.264 Packetization

- MTU: 1300 bytes payload max
- FU-A indicator: `(nalu[0] & 0x60) | 28`
- FU-A header: start(7) | end(6) | reserved(5) | type(4-0)
- Marker bit: ONLY on last RTP packet of access unit
- Video clock: 90 kHz
- Audio clock: 48 kHz
- No payload type 96 (reserved for probe packets)

### 3.3 SPS VUI

- `bitstream_restriction_flag` MUST be 1
- `max_num_reorder_frames` MUST be 0 (matches `-bf 0`)
- `max_dec_frame_buffering` MUST equal `max_num_ref_frames`
- `video_signal_type` stripped (set present flag to 0)
- High profile (100, 110, 122, etc.) has extra SPS fields that must be preserved

### 3.4 Go Live Specifics

- Stream key format: `guild:{guild_id}:{channel_id}:{user_id}` or `call:{channel_id}:{user_id}`
- daveChannelId = BigInt(serverId) - 1 (NOT channelId)
- Speaking mode = 2 (priority/soundshare), NOT 1 (normal)
- Separate voice WS, SSRCs, secret key, DAVE session
- UDP socket shared with main voice connection (different destination)
- READY streams array: first stream's ssrc = video_ssrc, rtx_ssrc

### 3.5 FFmpeg Flags

- `-bf 0`: Non-negotiable for real-time
- `-preset superfast`: NOT ultrafast
- `-forced-idr 1`: Every keyframe is IDR
- `-force_key_frames expr:gte(t,n_forced*1)`: 1s keyframe interval
- `-pix_fmt yuv420p`: Only 4:2:0 supported
- `-f nut pipe:1`: NUT to stdout

---

## 4. Implementation Details

### 4.1 Gateway Event Hooking

The VideoStreamer hooks into the Discord gateway by chaining onto `on_socket_raw_receive`:

```python
# streamer.py _setup_gateway_listener()
prev = getattr(self._client, 'on_socket_raw_receive', None)
async def _chained_handler(data):
    if prev:
        result = prev(data)
        if asyncio.iscoroutine(result):
            await result
    await _on_socket_raw_receive(data)
self._client.on_socket_raw_receive = _chained_handler
```

dpy-self only dispatches `socket_raw_receive` when `_enable_debug_events` is True. The `_ensure_gateway_dispatch()` method patches the gateway WS `log_receive` to always dispatch. This is called in `join_voice()` after `channel.connect()`.

### 4.2 UDP Socket Sharing

Both the main voice connection and the stream connection use the same UDP socket (discord.py's `VoiceClient._connection.socket`). The socket is already bound to a local port. Different destinations are specified in `socket.sendto(packet, (ip, port))`.

The main voice connection sends to `endpoint_ip:voice_port` (from VOICE_SERVER_UPDATE).
The stream connection sends to `ready_params.ip:ready_params.port` (from stream READY).

### 4.3 PyAV Pipe Bridge

```python
# media/demux.py _wrap_pipe()
pipe_fd = pipe._transport.get_extra_info('pipe').fileno()
dup_fd = os.dup(pipe_fd)  # duplicate fd
fcntl.fcntl(dup_fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)  # set blocking
return os.fdopen(dup_fd, 'rb'), dup_fd  # return synchronous file object
```

This bridges asyncio's non-blocking pipe to PyAV's synchronous API. The duplicated fd is set to blocking mode so PyAV can read synchronously. The original fd remains non-blocking for asyncio.

### 4.4 SPS VUI Rewrite Pipeline

For each video frame:
1. `split_nalu(frame)` splits Annex-B into individual NALUs
2. For each NALU, check `get_nalu_type(nalu) == 7` (SPS)
3. If SPS found, call `rewrite_sps_vui(nalu)` which:
   - Parses the SPS bitstream (Exp-Golomb coded)
   - Copies all fields verbatim except VUI section
   - Forces bitstream_restriction=1, max_num_reorder_frames=0
   - Strips video_signal_type
   - Returns rewritten SPS NALU
4. Reassemble NALUs with start codes: `b'\x00\x00\x01' + nalu` for each
5. Pass reassembled frame to DAVE encrypt, then RTP packetize

### 4.5 H.264 FU-A Packetization

For NALUs exceeding 1300 bytes:

1. FU indicator byte: `(nalu[0] & 0x60) | 28` -- preserves NRI bits, type 28 = FU-A
2. Strip NAL header byte: `payload = nalu[1:]`
3. Split into fragments of max 1298 bytes (1300 - 2)
4. First fragment: FU header = `nal_type | 0x80` (start bit)
5. Middle fragments: FU header = `nal_type` (no bits set)
6. Last fragment: FU header = `nal_type | 0x40` (end bit)
7. Each fragment gets its own RTP header with same timestamp
8. Marker bit set ONLY on the last fragment of the entire frame (not per-NALU)

### 4.6 Frame Pacing with A/V Sync

Video and audio FramePacers are linked via `sync_partner`:

```
video_pacer = FramePacer(clock_rate=90000)
audio_pacer = FramePacer(clock_rate=48000)
video_pacer.sync_partner = audio_pacer
```

When video is ahead (delta > 20ms), it waits via `asyncio.Event` notification from the audio pacer. When behind (delta < -20ms), it skips sleep and resets timing.

The `update_pts(pts_ms)` method on each pacer signals the partner via `self._pts_updated.set()` / `clear()`, avoiding busy-wait polling.

### 4.7 Transport Encryption Pipeline

For each RTP packet:

1. Build 12-byte RTP header via `build_rtp_header()`
2. AEAD encrypt payload with header as AAD:
   ```python
   box = nacl.secret.Aead(secret_key)
   nonce = struct.pack('>I', counter) + b'\x00' * 20
   encrypted = box.encrypt(payload, header, nonce)
   ```
3. Append 4-byte nonce counter: `encrypted + struct.pack('>I', used_counter)`
4. Wire packet: `header + encrypted + nonce_counter`
5. Increment counter: `self._nonce_counter = (self._nonce_counter + 1) & 0xFFFFFFFF`

---

## 5. Voice Receive Subsystem Details

### 5.1 DM Voice Support

The fork adds DM/group voice support via:

- `patches.py`: Monkey-patches `commands.Context.voice_client` to find voice clients in DM channels where `ctx.guild` is None, by iterating `ctx.bot.voice_clients` and matching channel IDs
- Gateway hook: All `guild.get_member(uid)` calls have fallback `vc.guild.get_member(uid) if vc.guild else vc.client.get_user(uid)`
- `VoiceRecvClient`: Uses `vc.user.id` instead of `vc.guild.me.id` for the bot's own SSRC

### 5.2 Jitter Buffer

`HeapJitterBuffer` in `buffer.py` uses `heapq` for packet reordering:
- Packets older than 10000 sequence numbers from last sent are dropped
- Prefill mechanism waits for `prefill` packets before emitting
- Signals via `MultiDataEvent` when data is available
- Thread-safe via `threading.Event`

### 5.3 Decryption Modes

`PacketDecryptor` in `reader.py` supports 4 modes:

- `xsalsa20_poly1305`: Nonce from first 12 bytes of header
- `xsalsa20_poly1305_suffix`: Nonce from last 24 bytes of payload
- `xsalsa20_poly1305_lite`: Nonce from last 4 bytes (counter mode)
- `aead_xchacha20_poly1305_rtpsize`: Nonce from last 4 bytes, header as AAD

The `rtpsize` variants include the extension header in the encrypted region. `adjust_rtpsize()` on RTPPacket handles this.

---

## 6. Open Issues and Research Areas

### 6.1 PyAV Event Loop Blocking (Severity: Critical)

PyAV's synchronous `container.demux()` blocks the asyncio event loop. Current mitigation: `asyncio.sleep(0)` every 8 frames. Alternative: raw Annex-B pipes with asyncio subprocess using extra file descriptors.

### 6.2 Secret Key Timing Race (Severity: High)

`start_go_live()` returns before SELECT_PROTOCOL_ACK arrives. If `play()` is called immediately, `VideoSender.start()` may not find the secret key. Fix: add wait loop for `stream_conn.secret_key`.

### 6.3 Fire-and-Forget Sends (Severity: Medium)

`_send_json()` uses `asyncio.ensure_future()` which swallows errors. Critical operations should use `_send_json_await()`. Currently IDENTIFY and SELECT_PROTOCOL use await; verify all critical paths.

### 6.4 Reconnection Incompleteness (Severity: Medium)

`_attempt_reconnect()` has exponential backoff but doesn't handle:
- Changed WebSocket URL (new endpoint)
- DAVE session state inconsistency after reconnect
- Stale `_ready_params`

### 6.5 Stream Preview Not Implemented (Severity: Low)

`set_stream_preview()` only logs. Node.js reference decodes keyframes, resizes to 1024x576, converts to JPEG, uploads via REST API.

---

## 7. Reference Index

### Protocol Documentation
- Discord Voice Connections: https://docs.discord.food/topics/voice-connections
- DAVE Protocol Whitepaper v1.1: https://daveprotocol.com/
- RFC 3550 (RTP): https://www.rfc-editor.org/rfc/rfc3550
- RFC 6184 (H.264 RTP): https://www.rfc-editor.org/rfc/rfc6184
- RFC 6716 (Opus): https://www.rfc-editor.org/rfc/rfc6716
- RFC 8285 (RTP Header Extensions): https://www.rfc-editor.org/rfc/rfc8285
- RFC 9420 (MLS): https://www.rfc-editor.org/rfc/rfc9420

### Source Code
- Node.js reference: https://github.com/Discord-RE/Discord-video-stream (v6.0.0, 4319 lines)
- Python upstream voice-recv: https://github.com/imayhaveborkedit/discord-ext-voice-recv
- discord.py VoiceClient: https://github.com/Rapptz/discord.py/blob/master/discord/voice_client.py
- WebRTC SPS VUI rewriter (C++): https://webrtc.googlesource.com/src/+/5f2c9278f35e47ff72eb191669d473b7400c9f3e/common_video/h264/sps_vui_rewriter.cc
- davey (Rust DAVE): https://github.com/Snazzah/davey
- node-datachannel: https://github.com/murat-d/node-datachannel

### Packages
- davey (Python): https://pypi.org/project/davey/ (0.1.0)
- @snazzah/davey (JS): https://www.npmjs.com/package/@snazzah/davey (0.1.8)
- discord.py-self: https://pypi.org/project/discord.py-self/ (2.1.0)
- PyAV: https://pypi.org/project/av/
- PyNaCl: https://pypi.org/project/PyNaCl/
- websockets: https://pypi.org/project/websockets/

### Related Projects
- aixxe.net Discord Video Bot: https://aixxe.net/2021/04/discord-video-bot
- mrjvs Discord-video-experiment (original PoC): https://github.com/mrjvs/Discord-video-experiment
- DAVE implementation tracking: https://github.com/Discord-RE/Discord-video-stream/issues/102
