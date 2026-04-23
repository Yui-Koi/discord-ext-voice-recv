# Observations: Node.js Reference and Python Implementation

Domain-specific observations, quirks, pitfalls, invariants, and constraints discovered during analysis of `@dank074/discord-video-stream` (Node.js v6.0.0) and the Python port. Organized by subsystem. Each observation is tagged with its source and severity.

---

## 1. WebRTC vs UDP Transport

### OBS-01: Node.js uses WebRTC, Python uses raw UDP [CRITICAL]

The Node.js reference uses `node-datachannel` (a WebRTC library) for all media transport. This is architecturally significant because WebRTC provides built-in RTP packetization, pacing, RTCP handling, RTX retransmission, and DTLS-SRTP encryption. The Python port uses raw UDP with manual RTP construction.

**Why this matters**: The Node.js code never directly builds RTP headers or handles FU-A fragmentation. `H264RtpPacketizer` from `node-datachannel` does it internally. The Python port must implement all of this manually, which is where bugs like the dead code in `split_nalu()` originated.

**Pitfall**: When reading the Node.js code, don't look for RTP header construction -- it's inside `node-datachannel`'s C++ internals. The `WebRtcWrapper.ts` `sendVideoFrame()` just calls `this._videoTrack?.sendMessageBinary(frame)` and the library handles the rest.

**Reference**: `@lng2004/node-datachannel` npm package, specifically `H264RtpPacketizer`, `RtpPacketizer`, `PacingHandler`, `RtcpSrReporter`, `RtcpNackResponder`.

### OBS-02: SELECT_PROTOCOL uses "webrtc" not "udp" in Node.js [IMPORTANT]

The Node.js reference sends `protocol: "webrtc"` in SELECT_PROTOCOL (opcode 1), not `protocol: "udp"`. The Python port sends `protocol: "udp"`. Both work, but the response format differs:

- WebRTC: Session Description contains an SDP string (parsed for ICE credentials, fingerprint, candidates)
- UDP: Session Description contains `secret_key`, `mode`, and `dave_protocol_version`

The Python port correctly handles the UDP path. The Node.js reference generates its own SDP answer from the server's SDP offer.

**Reference**: `BaseMediaConnection.ts` `setProtocols()` method, lines 244-280.

### OBS-03: For send-only connections, UDP address/port can be randomized [DOCUMENTED]

From the Discord voice docs: "These fields are only used to receive RTC data. If you only wish to send frames and do not care about receiving, you can randomize these values." This means Go Live (which is send-only) does not need IP discovery. The Python port correctly randomizes these in `_send_select_protocol()`.

**Reference**: Discord Voice Connections docs, "Protocol Data Structure" section.

---

## 2. Stream Connection Lifecycle

### OBS-04: STREAM_CREATE uses type "guild" or "call" [IMPORTANT]

The STREAM_CREATE gateway opcode (18) requires a `type` field: `"guild"` for guild voice channels, `"call"` for DM/group channels. The Node.js reference determines this from `VoiceConnection.type` which returns `"guild" if this.guildId else "call"`. The Python port correctly implements this in `StreamConnection.type`.

**Pitfall**: If you pass `type: "guild"` for a DM channel, the stream will not be created. The gateway silently ignores the request.

**Reference**: `Streamer.ts` `signalStream()` method; `utils.ts` `generateStreamKey()`.

### OBS-05: STREAM_CREATE and STREAM_SERVER_UPDATE are separate gateway events [CRITICAL]

After sending STREAM_CREATE, the gateway responds with TWO events:
1. `STREAM_CREATE` -- contains `stream_key` and `rtc_server_id`
2. `STREAM_SERVER_UPDATE` -- contains `stream_key`, `endpoint`, and `token`

Both must be received before connecting to the stream voice server. The Python port waits for both using separate `asyncio.Event` objects.

**Pitfall**: The gateway may send STREAM_SERVER_UPDATE before STREAM_CREATE, or vice versa. The implementation must handle either order. The Python port handles this correctly because it waits for both independently.

**Reference**: `Streamer.ts` `createStream()` method, `GatewayEvents.ts` type definitions.

### OBS-06: daveChannelId = BigInt(serverId) - 1n for streams [CONFIRMED]

The stream connection's DAVE channel ID is computed as `BigInt(serverId) - 1n`. This is NOT the same as the main voice connection's DAVE channel ID (which is `channelId`). This is confirmed in `StreamConnection.ts`:

```typescript
public override get daveChannelId() {
    const channelId = BigInt(this._serverId) - 1n;
    return channelId.toString();
}
```

The main voice connection uses `this.channelId` directly (in `VoiceConnection.ts`).

**Pitfall**: If you use the same DAVE channel ID for both connections, the MLS key exchange will fail because the server expects different channel IDs.

**Reference**: `StreamConnection.ts` line 18-20; `VoiceConnection.ts` line 7.

### OBS-07: serverId = guildId for guild, channelId for DM [IMPORTANT]

- Guild voice: `serverId = guildId`
- DM/group voice: `serverId = channelId`

This is confirmed in both Node.js (`VoiceConnection.ts`) and Python (`StreamConnection.server_id` property).

**Reference**: `VoiceConnection.ts` `get serverId()`.

### OBS-08: Speaking mode 2 for Go Live, mode 1 for camera [CRITICAL]

`StreamConnection.setSpeaking()` overrides the base class to use `speaking: speaking ? 2 : 0` (mode 2 = priority/soundshare). The base `BaseMediaConnection.setSpeaking()` uses `speaking: speaking ? 1 : 0` (mode 1 = normal speaking, for camera streams).

Getting this wrong means the stream will not register properly with Discord's UI. The Python port correctly uses mode 2 in `StreamConnection.set_speaking()`.

**Reference**: `StreamConnection.ts` `setSpeaking()` override; `BaseMediaConnection.ts` base `setSpeaking()`.

---

## 3. FFmpeg and Media Pipeline

### OBS-09: -bf 0 is non-negotiable for real-time streaming [CRITICAL]

B-frames require future reference frames, introducing reordering delay. For real-time Go Live, B-frames are incompatible with low-latency delivery. The Node.js reference forces `-bf 0` in the FFmpeg command. The Python port also sets `-bf 0`.

**Pitfall**: If the input video already has B-frames and you use `-vcodec copy` (no transcoding), the output will have B-frames and the stream will have visible glitches or high latency. The `no_transcoding` mode requires the input to already be B-frame free.

**Reference**: `newApi.ts` `prepareStream()`, `-bf 0` in output options.

### OBS-10: -preset superfast, NOT ultrafast [IMPORTANT]

The Node.js reference documentation explicitly warns: "ultrafast produces bitrate spikes that cause stream stuttering. superfast provides the best balance of speed and bitrate stability." The Python port uses `superfast`.

**Pitfall**: Using `ultrafast` seems like an optimization but actually degrades stream quality because the rate control is less precise.

**Reference**: Node.js README, "Performance tips" section; `PERFORMANCE.md`.

### OBS-11: NUT container for pipe output, not Matroska [IMPORTANT]

Both Node.js and Python use NUT format (`-f nut`) for the FFmpeg pipe output. NUT has lower framing overhead than Matroska, which reduces initial buffering delay. The Python PyAV demuxer opens with `format='nut'`.

**Pitfall**: If you change to Matroska format, the PyAV demuxer needs `format='matroska'` and the buffering behavior may differ.

**Reference**: `newApi.ts` `format: "nut"`; `media/demux.py` `self._format = format`.

### OBS-12: Bitstream filter chain for H.264 input [IMPORTANT]

The Node.js reference applies these bitstream filters when the input is H.264:
1. `h264_mp4toannexb` -- Converts AVCC (length-prefixed) to Annex-B (start-code-delimited)
2. `h264_metadata` with `aud: "remove"` -- Strips Access Unit Delimiters
3. `dump_extra` -- Ensures SPS/PPS extradata is present

The Python port does NOT apply these filters because FFmpeg's `-f nut` output already provides Annex-B format H.264. However, if using `-vcodec copy` (no transcoding), the input may be in AVCC format and the filters would be needed.

**Pitfall**: If you add `-vcodec copy` mode and the input is MP4/MKV (AVCC format), the H.264 data will have length prefixes instead of start codes, and `split_nalu()` will fail to find NALUs.

**Reference**: `LibavDemuxer.ts`, `BitStreamFilterAPI.create()` calls.

### OBS-13: Opus duration parsing from TOC byte [IMPORTANT]

When the NUT container does not provide accurate duration info for Opus frames, the demuxer parses the TOC byte per RFC 6716. The Python port implements this in `parse_opus_duration()`.

**Pitfall**: The TOC byte parsing table uses milliseconds (10, 20, 40, 60ms for SILK; 2.5, 5, 10, 20ms for CELT), not samples. The Python port converts to samples by multiplying by 48 (48 samples/ms at 48kHz).

**Reference**: RFC 6716 section 3.1; `LibavDemuxer.ts` `parseOpusPacketDuration()`; `media/demux.py` `parse_opus_duration()`.

---

## 4. RTP Packetization

### OBS-14: FU-A indicator byte formula [CRITICAL]

FU-A indicator byte = `(nalu[0] & 0x60) | 28`. The `0x60` mask preserves the NRI (nal_ref_idc) bits from the original NAL header. 28 is the FU-A NAL unit type.

FU-A header byte: bit 7 = start, bit 6 = end, bits 4-0 = original NAL unit type.

**Pitfall**: If you forget the `0x60` mask and just use `28`, the NRI bits will be zero, which may cause the decoder to treat the NALU as disposable.

**Reference**: RFC 6184 section 5.8; `rtp/h264.py` `_fu_a()` method.

### OBS-15: Marker bit on last packet of access unit [IMPORTANT]

The RTP marker bit MUST be set on the last RTP packet of each video access unit (frame). For multi-NALU frames (SPS + PPS + IDR), only the very last packet gets the marker bit.

**Pitfall**: Setting the marker bit on every packet, or on the last packet of each NALU instead of the last packet of the frame, will cause the decoder to incorrectly detect frame boundaries.

**Reference**: RFC 6184 section 5.1; `rtp/h264.py` `packetize_frame()`.

### OBS-16: Video uses 90kHz clock, audio uses 48kHz [IMPORTANT]

Video RTP timestamps use a 90kHz clock (90000 ticks per second). Audio (Opus) uses 48kHz (48000 ticks per second). PTS in milliseconds must be converted:

- Video: `rtp_timestamp = int(pts_ms * 90) & 0xFFFFFFFF`
- Audio: `rtp_timestamp = int(pts_ms * 48) & 0xFFFFFFFF`

**Pitfall**: Using the wrong clock rate will cause A/V sync issues. The FramePacer works in milliseconds, not RTP timestamps.

**Reference**: `CodecPayloadType.ts` `clockRate` values; `voice_send.py` timestamp conversion.

### OBS-17: No payload type should be 96 [CONSTRAINT]

From the Discord docs: "No payload type should be set to 96, as it is reserved for probe packets." The Node.js reference uses PT 101-120, which avoids 96.

**Reference**: Discord Voice Connections docs, "Codec Structure" section.

---

## 5. DAVE Protocol

### OBS-18: DAVE encrypt operates on complete frame BEFORE RTP packetization [CRITICAL]

The DAVE protocol encrypts the complete encoded frame before it is split across RTP packets. This means:

1. FFmpeg outputs a complete H.264 frame (Annex-B format)
2. SPS VUI rewrite (if applicable)
3. DAVE encrypt: `dave_session.encrypt(MediaType.video, Codec.h264, frame)`
4. RTP packetize: split the encrypted frame into NALUs, fragment with FU-A
5. Transport encrypt: AEAD encrypt each RTP packet
6. UDP send

The encrypted frame replaces the original in the RTP packet(s). The receiver must reconstruct the complete frame from RTP packets before DAVE decrypting.

**Pitfall**: If you DAVE encrypt AFTER RTP packetization (encrypt individual packets), the receiver cannot decrypt because DAVE operates on complete frames.

**Reference**: `WebRtcWrapper.ts` `sendVideoFrame()` -- encrypt happens before `this._videoTrack?.sendMessageBinary(frame)`.

### OBS-19: DAVE session is separate for stream connection [IMPORTANT]

The stream connection has its own DAVE session, separate from the main voice connection. This is because:
- Different `daveChannelId` (stream uses `serverId - 1`, voice uses `channelId`)
- Potentially different `dave_protocol_version`
- Different MLS group members

**Reference**: `BaseMediaConnection.ts` `_daveSession` field; each connection instance has its own.

### OBS-20: davey Python API vs JavaScript API [REFERENCE]

Python davey:
```python
dave_session.encrypt(media_type, codec, packet)  # MediaType.video=1, Codec.h264=4
dave_session.encrypt_opus(packet)
dave_session.process_proposals(operation_type, proposals, expected_user_ids)
```

JavaScript davey (`@snazzah/davey`):
```javascript
daveSession.encrypt(MediaType.VIDEO, Codec.H264, frame)
daveSession.encryptOpus(frame)
daveSession.processProposals(optype, proposals, [...connectedUsers])
```

Both expose the same underlying Rust API. The Python bindings use snake_case, JavaScript uses camelCase.

**Reference**: `davey` PyPI package (0.1.0); `@snazzah/davey` npm package (0.1.8).

### OBS-21: Passthrough mode when DAVE version is 0 [IMPORTANT]

When `dave_protocol_version` is 0 (no DAVE), or during transitions, the DAVE session operates in passthrough mode. `dave_session.set_passthrough_mode(True, timeout_frames)` allows frames to pass through unencrypted.

The Python port handles this correctly: `_dave_encrypt()` returns the frame unchanged when `dave_session is None or not dave_ready`.

**Reference**: DAVE protocol whitepaper, "Passthrough Mode" section.

---

## 6. SPS VUI Rewriting

### OBS-22: SPS VUI rewriter is a port of WebRTC's C++ implementation [REFERENCE]

The SPS VUI rewriter is derived from WebRTC's `sps_vui_rewriter.cc`:
- C++ source: https://webrtc.googlesource.com/src/+/5f2c9278f35e47ff72eb191669d473b7400c9f3e/common_video/h264/sps_vui_rewriter.cc
- TypeScript port: `SPSVUIRewriter.ts` in the Node.js reference
- Python port: `protocol/vui.py`

All three implementations are functionally identical.

### OBS-23: max_num_reorder_frames = 0 is the critical invariant [CRITICAL]

The most important change the rewriter makes is forcing `max_num_reorder_frames = 0`. This tells the decoder that no frame reordering occurs, which matches the `-bf 0` FFmpeg flag. If these are inconsistent (e.g., `-bf 0` is set but `max_num_reorder_frames` is not 0), the decoder may buffer frames expecting reordering, causing latency or glitches.

**Reference**: `sps_vui_rewriter.cc` line 283; `SPSVUIRewriter.ts` `addBitstreamRestriction()`.

### OBS-24: High profile SPS has extra fields [IMPORTANT]

H.264 High profile (profile_idc = 100) and related profiles (110, 122, 244, 44, 83, 86, 118, 128, 138, 144) have additional fields after `profile_idc`:
- `chroma_format_idc`
- `separate_colour_plane_flag` (if chroma_format_idc == 3)
- `bit_depth_luma_minus8`, `bit_depth_chroma_minus8`
- `qpprime_y_zero_transform_bypass_flag`
- `seq_scaling_matrix_present_flag` + scaling lists

The Python port correctly handles all of these in `rewrite_sps_vui()`.

**Reference**: H.264 spec, section 7.3.2.1.1; `protocol/vui.py` HIGH_PROFILES set.

### OBS-25: Emulation prevention bytes must be handled [IMPORTANT]

In H.264 RBSP (Raw Byte Sequence Payload), the byte sequence `0x00 0x00 0x03` is an emulation prevention sequence. The `0x03` byte must be skipped when reading and inserted when writing to avoid confusion with start codes.

The Python `BitstreamReader` handles this by checking for `0x00 0x00 0x03` at each byte boundary and skipping the `0x03`. The `BitstreamWriter` inserts `0x03` when the pending byte would create a `0x00 0x00 0x00/01/02/03` sequence.

**Pitfall**: Forgetting emulation prevention handling will produce corrupt SPS NALUs that the decoder cannot parse.

**Reference**: H.264 spec, section 7.4.1; `protocol/vui.py` BitstreamReader/BitstreamWriter.

---

## 7. Frame Pacing and A/V Sync

### OBS-26: Frame pacing formula [REFERENCE]

The frame pacing formula (from Node.js `BaseMediaStream._write()` and Python `FramePacer.pace()`):

```
sleep_ms = (pts - start_pts + frametime) - (now - start_time)
```

Where:
- `pts` = presentation timestamp in milliseconds
- `start_pts` = PTS of the first frame
- `frametime` = frame duration in milliseconds
- `now` = current wall clock time
- `start_time` = wall clock time when first frame was sent

**Reference**: `BaseMediaStream.ts` `_write()` method; `media/pacer.py` `pace()` method.

### OBS-27: Sync tolerance of 20ms [IMPORTANT]

The default sync tolerance is 20ms. If video is more than 20ms ahead of audio, it waits. If more than 20ms behind, it catches up (skips sleep). This prevents lip-sync drift while allowing small timing variations.

**Reference**: `BaseMediaStream.ts` `_syncTolerance = 20`; `media/pacer.py` `sync_tolerance_ms = 20.0`.

### OBS-28: PacingHandler rate = 25 Mbps, burst = 1 [IMPORTANT]

The Node.js reference adds `PacingHandler(25 * 1000 * 1000, 1)` to the video packetizer chain. This paces individual RTP packets at 25 Mbps with burst size 1 (no bursting allowed). The Python port implements equivalent pacing in the send loop.

**Why 25 Mbps**: This is very conservative. A 5 Mbps stream only needs 5 Mbps, but the 25 Mbps ceiling prevents burst-induced packet loss during keyframes. A keyframe might be 10x larger than a P-frame, generating many RTP packets in a burst.

**Reference**: `WebRtcWrapper.ts` `setPacketizer()` method.

---

## 8. Gateway and WebSocket Quirks

### OBS-29: dpy-self requires _enable_debug_events for socket_raw_receive [BUG]

dpy-self only dispatches `socket_raw_receive` events when `_enable_debug_events` is True. The VideoStreamer patches the gateway WebSocket to always dispatch. This is documented in `streamer.py` `_ensure_gateway_dispatch()`.

**Pitfall**: Without the patch, STREAM_CREATE and STREAM_SERVER_UPDATE events will never be received, and `start_go_live()` will hang until timeout.

**Reference**: `streamer.py` `_ensure_gateway_dispatch()` method.

### OBS-30: Binary WebSocket messages have 2-byte sequence prefix (server-to-client only) [IMPORTANT]

DAVE binary opcodes (25-30) use a binary WebSocket frame format:
- Server-to-client: `[2-byte sequence BE][1-byte opcode][payload]`
- Client-to-server: `[1-byte opcode][payload]` (NO sequence prefix)

The Python port correctly handles this asymmetry in `_handle_binary_message()` (reads 2-byte seq) and `_send_binary()` (does not include seq).

**Pitfall**: If you include the 2-byte sequence prefix in client-to-server binary messages, the server will misparse the opcode.

**Reference**: `BaseMediaConnection.ts` `handleBinaryMessages()` (reads `msg.readUint16BE(0)` for seq, `msg.readUint8(2)` for op); `sendOpcodeBinary()` (writes `[code][data]`, no seq).

### OBS-31: Heartbeat includes seq_ack [IMPORTANT]

The heartbeat payload includes `seq_ack`, which is the last sequence number received from the server. This allows the server to know which messages the client has received.

**Reference**: `BaseMediaConnection.ts` `setupHeartbeat()`.

### OBS-32: Resume uses close code 4015 or < 4000 [IMPORTANT]

When the WebSocket closes with code 4015 or any code < 4000, the connection can be resumed. The Python port implements this in `_receive_loop()` with `_attempt_reconnect()`.

**Reference**: `BaseMediaConnection.ts` WebSocket close handler; `stream_connection.py` `_receive_loop()`.

---

## 9. Codec and Stream Configuration

### OBS-33: Simulcast streams are hardcoded to a single quality [OBSERVATION]

Both Node.js and Python send a single simulcast stream: `[{type: "screen", rid: "100", quality: 100}]`. Discord supports multiple quality levels (the docs example shows rid "50" at quality 50 and rid "100" at quality 100), but neither implementation sends multiple streams.

**Reference**: `utils.ts` `STREAMS_SIMULCAST`; `protocol/types.py` `STREAMS_SIMULCAST`.

### OBS-34: Codec priority must be unique per type [CONSTRAINT]

From the Discord docs: "The preferred priority of the codec as a multiple of 1000 (unique per type)." All video codecs in the reference use priority 1000, which means they are equally preferred. Opus also uses 1000 for audio.

**Reference**: Discord Voice Connections docs, "Codec Structure" section.

### OBS-35: max_bitrate hardcoded to 10_000_000 in VIDEO opcode [OBSERVATION]

The Node.js reference hardcodes `max_bitrate: 10000 * 1000` (10 Mbps) in the VIDEO opcode payload. The Python port also uses 10_000_000. This is the maximum declared bitrate, not the actual sending bitrate.

**Reference**: `BaseMediaConnection.ts` `setVideoAttributes()`.

---

## 10. Python-Specific Observations

### OBS-36: PyAV synchronous API blocks the event loop [KNOWN ISSUE]

PyAV's `av.open()` and `container.demux()` are synchronous. The Python port wraps the asyncio pipe fd in blocking mode via `_wrap_pipe()` and calls `asyncio.sleep(0)` every 8 frames to yield control. This is a compromise: it blocks for up to 8 frames at a time, then yields.

**Mitigation**: For production use, consider wrapping the entire PyAV iteration in `asyncio.to_thread()` or using raw Annex-B pipes to avoid PyAV entirely.

**Reference**: `media/demux.py` `_wrap_pipe()` and `demux()`.

### OBS-37: try/except ImportError pattern for test compatibility [KNOWN ISSUE]

Every `discord_video_stream` module has `try: from .X import Y / except ImportError: from X import Y` fallback imports. This is because tests use `sys.path.insert()` instead of proper package imports. This pattern breaks package isolation.

**Reference**: All `discord_video_stream/*.py` files.

### OBS-38: struct.Struct is faster than struct.pack/unpack [OPTIMIZATION]

The reference repo uses `struct.Struct('>xxHII')` as a class-level constant for repeated parsing. This is significantly faster than calling `struct.pack()` / `struct.unpack()` because the Struct object pre-compiles the format. The Python port's `rtp/serialize.py` uses `struct.Struct('>BBHII')` for the same reason.

**Reference**: `discord/ext/voice_recv/rtp.py` `_hstruct`; `rtp/serialize.py` `_HEADER_FMT`.

---

## References and Resources

### Protocol Documentation
- Discord Voice Connections (comprehensive unofficial docs): https://docs.discord.food/topics/voice-connections
- DAVE Protocol Whitepaper: https://daveprotocol.com/
- DAVE Protocol Repository: https://github.com/discord/dave-protocol
- RFC 3550 (RTP): https://www.rfc-editor.org/rfc/rfc3550
- RFC 6184 (H.264 RTP Payload Format): https://www.rfc-editor.org/rfc/rfc6184
- RFC 6716 (Opus Codec): https://www.rfc-editor.org/rfc/rfc6716
- RFC 8285 (RTP Header Extensions): https://www.rfc-editor.org/rfc/rfc8285
- RFC 9420 (MLS Protocol): https://www.rfc-editor.org/rfc/rfc9420

### Implementation References
- discord-video-stream (Node.js, primary reference): https://github.com/Discord-RE/Discord-video-stream
- discord-ext-voice-recv (Python, upstream): https://github.com/imayhaveborkedit/discord-ext-voice-recv
- discord.py VoiceClient: https://github.com/Rapptz/discord.py/blob/master/discord/voice_client.py
- Snazzah/davey (Rust DAVE, Python/JS bindings): https://github.com/Snazzah/davey
- WebRTC SPS VUI Rewriter (original C++): https://webrtc.googlesource.com/src/+/5f2c9278f35e47ff72eb191669d473b7400c9f3e/common_video/h264/sps_vui_rewriter.cc
- node-datachannel (WebRTC for Node.js): https://github.com/murat-d/node-datachannel
- aixxe.net Discord Video Bot writeup: https://aixxe.net/2021/04/discord-video-bot
- mrjvs/Discord-video-experiment (original PoC): https://github.com/mrjvs/Discord-video-experiment

### PyPI / npm Packages
- davey (Python): https://pypi.org/project/davey/
- @snazzah/davey (JavaScript): https://www.npmjs.com/package/@snazzah/davey
- @dank074/discord-video-stream (Node.js): https://www.npmjs.com/package/@dank074/discord-video-stream
- discord.py: https://pypi.org/project/discord.py/
- discord.py-self: https://pypi.org/project/discord.py-self/
- PyAV: https://pypi.org/project/av/
- PyNaCl: https://pypi.org/project/PyNaCl/
- websockets: https://pypi.org/project/websockets/
