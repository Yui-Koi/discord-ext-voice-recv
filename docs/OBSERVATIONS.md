# Discord Video Stream - Deep Technical Observations

## Date: 2026-04-22

---

## 1. FFmpeg Pipeline Observations

### NUT Container Format
The library uses NUT format (`-f nut`) for the FFmpeg output pipe, not Matroska/MKV. NUT is a simple, low-overhead container designed for streaming. Key properties:
- Minimal framing overhead (important for real-time pipe reading)
- No seek table or index (streaming use case)
- Supports all codecs via simple codec ID signaling
- The demuxer in `LibavDemuxer.ts` accepts both `matroska` and `nut` as format options, but defaults to `nut`

NUT is read from a `PassThrough` stream piped from FFmpeg's stdout. The demuxer uses `Demuxer.open(input, { format: "nut", bufferSize: 8192 })`. The small 8KB buffer is intentional for low-latency.

### Critical FFmpeg Flags

**`-bf 0` (no B-frames):** This is essential. B-frames require future reference frames, which introduces reordering delay. For real-time streaming, B-frames are incompatible with low-latency delivery. Without `-bf 0`, the stream will have visible glitches or high latency.

**`-force_key_frames expr:gte(t,n_forced*1)`:** Forces an IDR keyframe every 1 second. This is a Discord requirement -- the SFU needs periodic keyframes for new viewers joining mid-stream and for error recovery. The `fixed_keyframe_interval` experiment mentioned in the READY payload may relate to this.

**`-pix_fmt yuv420p`:** Forces 4:2:0 chroma subsampling. This is the only pixel format Discord's decoder reliably handles.

**`-bufsize:v {bitrate/2}k`:** The VBV buffer size is set to half the average bitrate. This controls bitrate variability -- tighter buffer = more consistent bitrate but potentially lower quality. The ratio of maxrate to bufsize determines how much bitrate can spike.

**`-tune film` (default for x264):** The `film` tuning optimizes for live-action content. `animation` tuning would be better for screen recordings/game streams but worse for camera feeds. The library defaults to `film`.

**`-preset superfast`:** The library explicitly warns against `ultrafast`. In testing, `ultrafast` produces bitrate spikes that cause stream stuttering. `superfast` provides the best balance of speed and bitrate stability.

**`-forced-idr 1`:** Forces every keyframe to be an IDR frame (not just keyframes). This ensures the decoder can always recover from any point.

### Bitstream Filter Chain
For H.264 input, the demuxer applies a chain of bitstream filters:
1. `h264_mp4toannexb` -- Converts from AVCC length-prefixed format to Annex-B start-code-delimited format
2. `h264_metadata` with `aud: "remove"` -- Removes Access Unit Delimiters that would confuse the RTP packetizer
3. `dump_extra` -- Ensures extradata (SPS/PPS) is included in the stream

For HEVC, equivalent `hevc_mp4toannexb` and `hevc_metadata` filters are used.

For VP8/VP9/AV1, a `null` filter is used (passthrough).

### Volume Control via ZMQ
The library uses FFmpeg's `azmq` audio filter for real-time volume control. It:
1. Generates a random loopback IP (127.x.y.z) to avoid WSL2 conflicts
2. Creates a ZMQ REQ socket connected to `tcp://{ip}:42069`
3. Sends `volume@internal_lib volume {value}` commands
4. Awaits confirmation response

This is only available on Node.js (not Bun or Deno) because ZMQ has native compilation requirements.

### `readrate_initial_burst`
This FFmpeg flag controls initial read speed. When set (e.g., to 10 seconds), FFmpeg reads the first 10 seconds of input at maximum speed (no pacing), then switches to real-time reading. The library uses this for puppeteer-stream scenarios where the input is a live browser capture -- you want to fill the buffer quickly to reduce initial latency, then pace normally.

In the code: `vStream.sync = false; vStream.noSleep = aStream.noSleep = true;` during burst, then switches to normal pacing when `pts >= burstTime * 1000`.

---

## 2. H.264 SPS VUI Rewriting -- Why It Exists

The SPS VUI rewriter (`SPSVUIRewriter.ts`, 332 lines) is a port of WebRTC's `sps_vui_rewriter.cc`. It modifies H.264 Sequence Parameter Set NALUs to add or rewrite the Video Usability Information section.

**What it does:**
- Parses the SPS NAL unit bit by bit (Exp-Golomb coded)
- Copies all fields verbatim EXCEPT:
  - `video_signal_type_present_flag` is set to 0 (strips color space info)
  - `bitstream_restriction_flag` is forced to 1
  - `max_num_reorder_frames` is forced to 0 (no frame reordering)
  - `max_dec_frame_buffering` is set to `max_num_ref_frames`
  - If no VUI existed, a new one is injected with just bitstream restriction

**Why it's needed:**
- Discord's WebRTC decoder (likely based on Chromium's) requires `bitstream_restriction` to be present
- `max_num_reorder_frames = 0` ensures no B-frame reordering (matches the `-bf 0` FFmpeg flag)
- Without this, the decoder may buffer frames incorrectly or reject the stream entirely

**When it runs:**
- Only for H.264 video (not H.265, VP8, VP9, or AV1)
- On every frame, but only rewrites if an SPS NALU is found in that frame
- The code checks `H264Helpers.getUnitType(el) === H264NalUnitTypes.SPS` (type 7)

**MVP implication:** We need to port this. It is ~200 lines of bitstream manipulation in TypeScript. Python implementation would use similar bit-level struct operations. If we skip it, the stream may still work (FFmpeg's x264 typically includes VUI), but `max_num_reorder_frames` may not be set to 0.

---

## 3. RTP Packetization Details

### H.264 NALU Processing
The flow from FFmpeg output to RTP:
1. FFmpeg outputs H.264 in Annex-B format (start codes 0x000001 or 0x00000001)
2. `splitNalu()` splits the frame into individual NALUs by finding start codes
3. Each NALU is examined: if it's an SPS, it gets VUI-rewritten
4. The NALUs are reassembled with start codes and passed to the RTP packetizer
5. The `H264RtpPacketizer` from `node-datachannel` handles FU-A fragmentation

**Emulation prevention:** The bitstream reader/writer handles 0x000003 emulation prevention bytes. In H.264 Annex-B, the byte sequence 0x000000, 0x000001, 0x000002, and 0x000003 in the raw bitstream must be escaped with a 0x03 byte to avoid confusion with start codes.

### Opus Packet Duration Detection
The demuxer (`LibavDemuxer.ts`) includes a function `parseOpusPacketDuration()` that reads the Opus TOC byte to determine frame duration. This is needed because the NUT container may not have accurate duration info for Opus frames.

The RFC 6716 TOC byte layout:
- Bits 7-3: Configuration number (determines bandwidth, frame size)
- Bit 2: Stereo flag
- Bits 1-0: Code (0 = 1 frame, 1-2 = 2 frames, 3 = CELT frame count in next byte)

Frame sizes are mapped from the configuration number (2.5ms to 60ms per frame).

### Codec Payload Types (from source)
The library uses hardcoded payload types:
```typescript
opus: PT 120, clock 48000
H264: PT 101, RTX 102, clock 90000
H265: PT 103, RTX 104, clock 90000
VP8:  PT 105, RTX 106, clock 90000
VP9:  PT 107, RTX 108, clock 90000
AV1:  PT 109, RTX 110, clock 90000
```

**Observation:** The Discord docs show a different example where the CLIENT chooses payload types dynamically (AV1 at PT 101/102, H264 at PT 103/104). The server example shows these in the READY streams. However, the working Node.js implementation uses fixed PTs. It is unclear if Discord's server respects client-specified PTs or if the docs example is aspirational. For the MVP, use the same fixed PTs as the Node.js lib.

### RTX Retransmission
Each video codec has an associated RTX (retransmission) payload type. RTX packets are used to retransmit lost RTP packets. The `node-datachannel` library's `RtcpNackResponder` handles this automatically -- when a NACK is received, it retransmits the requested packet using the RTX payload type and a separate RTX SSRC.

**Observation:** For send-only Go Live, RTX may not matter much because we don't expect to receive NACKs (we're sending, not receiving). But the pacing handler and RTCP SR reporter are still important.

### Pacing Handler
The `PacingHandler` from `node-datachannel` is added to the video packetizer chain with a rate of 25 Mbps and burst size of 1. This smooths out packet delivery to avoid overwhelming the network with large keyframe bursts. Without pacing, a keyframe could generate hundreds of packets sent simultaneously, potentially causing UDP buffer overflow or packet loss.

**MVP implication:** We need to implement our own pacing. A simple token-bucket or leaky-bucket rate limiter at 25 Mbps should suffice.

---

## 4. Frame Pacing and A/V Sync

### BaseMediaStream Timing Algorithm
The frame pacing in `BaseMediaStream.ts` is worth understanding in detail:

1. On first frame: record `_startTime` (wall clock) and `_startPts` (presentation timestamp)
2. For each frame: compute `sleep = pts - startPts + frametime - (now - startTime)`
3. If `sleep > 0`: sleep for that duration
4. If `sleep <= 0` and behind: skip sleep, send immediately
5. If ahead (pts > other stream's pts + tolerance): wait in a loop

The sync tolerance is 20ms by default. If video is more than 20ms ahead of audio, it waits. If behind, it skips sleep.

**The "sync stream" concept:** Video and audio streams are linked via `syncStream`. Video's sync stream is audio, and they coordinate: if video gets ahead, it pauses; if behind, it catches up. This ensures lip sync.

**The `noSleep` mode:** Used during `readrate_initial_burst` and can be set manually. Frames are sent as fast as possible without any timing. This is useful for initial buffering.

**The `frametime` calculation:**
```typescript
frametime = (Number(duration) / timeBase.den) * timeBase.num * 1000
```
Converts from codec time base to milliseconds. For video at 30fps with 90kHz clock: frametime = 33.33ms.

### PTS Callback
The stream emits a `pts` event with the current presentation timestamp. This is used by the `readrate_initial_burst` logic to detect when the burst period is over.

---

## 5. Voice WebSocket Protocol Quirks

### Binary vs JSON Messages
- JSON messages: All standard opcodes (0-20, and DAVE opcodes 21-24, 31)
- Binary messages: Only DAVE MLS opcodes (25-30)
- Binary format: `[2-byte sequence (BE)] [1-byte opcode] [variable payload]`
- The sequence number is only present in SERVER->CLIENT binary messages

### Heartbeat
- Interval comes from HELLO opcode (8) as `heartbeat_interval`
- Client sends `{op: 3, d: {t: Date.now(), seq_ack: sequenceNumber}}`
- Server responds with `{op: 6, d: {t: same_timestamp}}`
- `seq_ack` is the last sequence number received from the server

### Connection Lifecycle
1. Connect to `wss://{server}/?v=9`
2. Server sends HELLO (heartbeat_interval)
3. Client sends IDENTIFY
4. Server sends READY (ssrc, ip, port, modes, streams)
5. Client sends SELECT_PROTOCOL (with SDP or UDP data + codecs)
6. Server sends SELECT_PROTOCOL_ACK (with SDP or secret_key + mode)
7. Client can now send VIDEO opcode to signal video attributes
8. Client sends SPEAKING to indicate it's sending media

### Resume
- Uses close codes: 4015 or any code < 4000 can be resumed
- RESUME opcode (7) includes server_id, session_id, token, seq_ack
- RESUMED opcode (9) confirms successful resume
- v8+ adds "buffered resuming" meaning the server buffers missed events

### One-Byte RTP Header Extensions
Discord uses RFC 8285 one-byte RTP header extensions. The extension IDs map to specific extension URIs. The `node-datachannel` library's `RtpPacketizationConfig` handles `playoutDelayId` (set to 5 for audio, 5 for video) to signal playout delay bounds.

---

## 6. Stream Lifecycle Details

### Go Live vs Camera

**Camera stream:**
- Uses existing VoiceConnection's WebRTC connection
- Signals `self_video: true` via VOICE_STATE_UPDATE
- Speaking flag = 1 (normal speaking)
- No separate stream server needed

**Go Live:**
- Creates a SEPARATE StreamConnection
- Requires STREAM_CREATE -> STREAM_SERVER_UPDATE -> new WebSocket/WebRTC connection
- Speaking flag = 2 (priority/soundshare speaking -- different from camera!)
- Has its own SSRC, separate from the voice connection
- `serverId` = `guildId` for guild channels, `channelId` for DM/call
- For Go Live, `daveChannelId` = `BigInt(serverId) - 1n` as string

### Stream Key Format
```
{type}:{guild_id?}:{channel_id}:{user_id}
```
- For guild: `guild:41771983423143937:123456:789012`
- For call: `call:123456:789012`

### Stream Preview
The `setStreamPreview` method calls `guild.members.me.voice.postPreview(data)` which is a Discord API call to set the preview image shown to viewers before they join. The image is JPEG, resized to 1024x576, base64 encoded. This is Go Live only (not camera).

### Stage Channel Handling
Stage channels require `setSuppressed(false)` before the bot can speak. This sends a gateway opcode to request stage speaker status. Stage channels also do NOT require DAVE/E2EE.

---

## 7. DAVE Protocol Flow

### Initial Connection with DAVE
1. IDENTIFY includes `max_dave_protocol_version: 1` (or whatever max is)
2. SELECT_PROTOCOL_ACK includes `dave_protocol_version: 1` (server-selected version)
3. If version > 0, client sends MLS_KEY_PACKAGE (binary opcode 26) with serialized key package
4. Server sends MLS_EXTERNAL_SENDER (binary opcode 25) with external sender public key + credential

### MLS Group Establishment
1. Server sends MLS_PROPOSALS (opcode 27) with Add proposals for all pending members
2. Each client processes proposals, generates commit + optional welcome
3. Client sends MLS_COMMIT_WELCOME (opcode 28) with commit (and welcome if new members)
4. Server picks "winning" commit, broadcasts MLS_ANNOUNCE_COMMIT_TRANSITION (opcode 29)
5. New members receive MLS_WELCOME (opcode 30)
6. All clients send DAVE_TRANSITION_READY (opcode 23)
7. Server sends DAVE_EXECUTE_TRANSITION (opcode 22)
8. Now frames are encrypted with the new key ratchet

### Frame Encryption
After the MLS group is established:
1. Each sender exports a key ratchet for their own SSRC from the MLS group
2. `Encryptor.set_key_ratchet(key_ratchet)` configures the encryptor
3. `Encryptor.assign_ssrc_to_codec(ssrc, codec)` maps SSRC to codec type
4. For each frame: `Encryptor.encrypt(media_type, ssrc, frame_bytes)` returns encrypted frame
5. The encrypted frame replaces the original in the RTP packet(s)

### Passthrough Mode
When `dave_protocol_version` is 0 (no DAVE), or during transitions, the encryptor operates in passthrough mode where frames pass through unencrypted. The library calls `dave_session.setPassthroughMode(true, timeout_frames)`.

### Protocol Transitions
- DAVE_PREPARE_TRANSITION (opcode 21): Server announces version change
- Client saves pending transition ID, sends DAVE_TRANSITION_READY (opcode 23)
- Server sends DAVE_EXECUTE_TRANSITION (opcode 22): Client switches to new version
- DAVE_PREPARE_EPOCH (opcode 24): MLS epoch change, includes epoch number
- epoch=1 means new group creation, epoch>1 means group update

### Invalid Commit Recovery
If a commit is unprocessable:
1. Client sends MLS_INVALID_COMMIT_WELCOME (opcode 31) with transition_id
2. Client resets local MLS state, generates new key package
3. Server proposes removal and re-addition of the client

---

## 8. UDP Transport Encryption Details

### aead_xchacha20_poly1305_rtpsize
This is the required encryption mode. Format:
```
[RTP header (unencrypted)] [encrypted payload] [4-byte nonce]
```
- The RTP header is treated as Additional Authenticated Data (AAD)
- The nonce is a 32-bit incremental counter (starts at 0, increments per packet)
- The 4-byte nonce is appended to the encrypted payload
- Encryption uses `pynacl.secret.Aead` with a 32-byte key from SELECT_PROTOCOL_ACK

### aead_aes256_gcm_rtpsize
Same layout, but uses AES-256-GCM instead of XChaCha20-Poly1305. Preferred when available (hardware AES acceleration). Uses Python's `cryptography.hazmat.primitives.ciphers.aead.AESGCM` or OpenSSL's EVP.

### RTP Size vs Fixed Size
The "rtpsize" variants include the RTP header extension data in the unencrypted header (treating it as part of the RTP header per SRTP convention). The deprecated "fixed size" variants use a fixed 12-byte unencrypted header.

### Nonce Management
The nonce counter is per-connection and wraps at 2^32. It must be synchronized between encryption and decryption. The 4-byte nonce appended to each packet allows the receiver to identify the correct nonce without maintaining state.

---

## 9. Integration Observations

### discord-ext-voice-recv Threading Model
The receive library uses threads extensively:
- `PacketRouter` thread: processes decoded packets from jitter buffer
- `SinkEventRouter` thread: dispatches events to sink listeners
- `SpeakingTimer` thread: detects speaking start/stop
- `UDPKeepAlive` thread: sends periodic UDP keepalive packets
- Main thread: receives UDP packets via socket listener callback

**For the Python port:** The send side should use `asyncio` instead of threads for consistency with modern Python patterns. The receive side's threading model is fine as-is.

### SSRC Mapping
Both the send and receive sides need to track SSRC <-> user ID mappings. The receive side (`VoiceRecvClient`) maintains `_ssrc_to_id` and `_id_to_ssrc` dicts. The send side needs to track its own audio SSRC, video SSRC, and RTX SSRC from the READY payload.

### VoiceData Container
The receive side wraps packets in `VoiceData` objects containing:
- `packet`: The raw RTP packet
- `source`: The member/user who sent it
- `pcm`: Decoded PCM audio (if not opus sink)
- `opus` property: Returns `packet.decrypted_data`

---

## 10. Miscellaneous Observations

### `noTranscoding` Mode
When enabled, the video codec is set to `copy` (passthrough). The input video stream is used as-is. Requirements:
- Keyframe every 1 second (for SFU recovery)
- No B-frames
- Correct resolution and frame rate for the target
- Correct codec (must match what Discord expects)

This mode skips all FFmpeg video processing, saving CPU. Useful when the input is already H.264 with the right properties.

### The `fixed_keyframe_interval` Experiment
Mentioned in the READY payload's `experiments` array. When enabled, the server may enforce a fixed keyframe interval. The library's `-force_key_frames expr:gte(t,n_forced*1)` ensures 1-second keyframe intervals regardless.

### Simulcast Streams
The library sends a single simulcast stream (`rid: "100"`, `quality: 100`). Discord supports multiple quality levels (the example shows `rid: "50"` at quality 50 and `rid: "100"` at quality 100), but the library only sends the highest quality.

### The `setStreamPreview` HTTP Call
This is a REST API call, not a voice gateway operation. It uses `guild.members.me.voice.postPreview(data)` which calls `PUT /guilds/{guild.id}/voice-states/@me/preview` with a base64-encoded JPEG body.

### Error Handling Patterns
- FFmpeg errors are caught via the `command.on("error")` event
- WebSocket errors are logged but don't stop the connection
- WebRTC state "closed" triggers reconnection attempt
- AbortSignal is used throughout for cancellation

### `PacingHandler` Configuration
`new PacingHandler(25 * 1000 * 1000, 1)` -- 25 Mbps rate, burst size 1. This means each RTP packet is individually paced. A burst size of 1 means no bursting allowed -- each packet must wait its turn based on the rate limit. This is very conservative and prevents any packet clustering.

### `RtcpSrReporter` and `RtcpNackResponder`
Both are added to the packetizer chain:
- `RtcpSrReporter`: Generates Sender Reports (RTCP SR) with NTP timestamps and RTP timestamp mapping. Used for synchronization and RTT estimation.
- `RtcpNackResponder`: Responds to NACK requests by retransmitting lost packets. Maintains a history of sent packets.

### The `streamPreview` Experimental Feature
Uses `pDebounce` to debounce the preview update. Only decodes keyframes (checks `AV_PKT_FLAG_KEY`). Decodes to RGBA, resizes to 1024x576, converts to JPEG. This is relatively expensive and is disabled by default.
