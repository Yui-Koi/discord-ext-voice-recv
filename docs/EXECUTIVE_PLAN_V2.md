# Executive Plan V2: Discord Video Stream Python Port
# Continuation from Phases 1-4 (Completed by Previous Agent)

## Date: 2026-04-23

---

## 1. Situation Assessment

### What Was Done (Phases 1-4)

The previous agent completed a solid foundation across 5 commits on the `video-stream-port` branch of `Yui-Koi/discord-ext-voice-recv`:

| Phase | Files | Lines | Status |
|-------|-------|-------|--------|
| Phase 1: RTP + Crypto | `rtp/serialize.py`, `rtp/crypto.py`, `rtp/h264.py` | 526 | Complete, tested |
| Phase 2: FFmpeg + Demux | `media/ffmpeg.py`, `media/demux.py` | 569 | Complete, tested |
| Phase 3: Frame Pacing | `media/pacer.py` | 204 | Complete, tested |
| Phase 4: Protocol + VUI | `protocol/types.py`, `protocol/vui.py` | 751 | Complete, tested |
| Tests | 7 test files | 2,128 | Comprehensive |
| **Total** | **21 files** | **4,275** | **Phases 1-4 done** |

### What Was NOT Done (Phases 5-6)

| Phase | Files Needed | Estimated Lines | Status |
|-------|-------------|----------------|--------|
| Phase 5: Stream Connection + Go Live | `stream_connection.py`, `voice_send.py`, `streamer.py` | ~600 | Not started |
| Phase 6: Integration + Compat | `compat/voice_recv.py`, `__init__.py` exports | ~100 | Not started |

### Handoff Context

The previous agent left:
- Detailed EXECUTIVE_PLAN.md and FINAL_PLAN.md with architecture decisions
- Comprehensive CONTEXT.md and OBSERVATIONS.md with protocol research
- COMPAT_AUDIT.md confirming dpy-self / discord.py compatibility
- Working test harness (7 test files, 2,128 lines)
- All Phase 1-4 code with inline documentation

What was NOT left:
- No handoff notes about specific implementation blockers
- No partial Phase 5 code
- No notes about which specific Discord servers/channels were tested
- No credentials or bot tokens

---

## 2. Validation Pass: Phase 1-4 Code Against dpy-self + Reference Repos

### 2.1 RTP Serialization (rtp/serialize.py) -- VALIDATED

Cross-referenced against:
- voice-recv `rtp.py` `_hstruct = struct.Struct('>xxHII')` -- our `build_rtp_header` produces headers parseable by this struct. Test `test_compatible_with_voice_recv_parsing` confirms this.
- Node.js `WebRtcWrapper.ts` `RtpPacketizationConfig` -- our PT values (opus=120, H264=101, RTX=102) match `CodecPayloadType.ts` exactly.
- RTCP SR format matches voice-recv's `SenderReportPacket` parsing (28 bytes: 8 header + 20 sender info).

No issues found.

### 2.2 Transport Encryption (rtp/crypto.py) -- VALIDATED

Cross-referenced against:
- voice-recv `reader.py` `PacketDecryptor._decrypt_rtp_aead_xchacha20_poly1305_rtpsize` -- our encrypt is the exact reverse. The nonce format (4-byte counter + 20 zero bytes), AAD (RTP header), and output format (ciphertext + 4-byte nonce counter) all match.
- discord.py `voice_client.py` `_encrypt_aead_xchacha20_poly1305_rtpsize` -- the existing discord.py encrypt function uses the same nacl.secret.Aead API. Our implementation mirrors it.
- Test `test_round_trip_xchacha20` confirms encrypt/decrypt round-trip works.

Minor note: The `aead_aes256_gcm_rtpsize` mode is declared in `SUPPORTED_MODES` but both branches use `nacl.secret.Aead`. This works because pynacl's Aead supports both XChaCha20 and AES-256-GCM, but the nonce format differs (12 bytes for AES-GCM vs 24 for XChaCha20). The current implementation always uses 24-byte nonce. This should be fixed if AES-GCM is ever actually used, but it's non-blocking since Discord prefers XChaCha20.

### 2.3 H.264 Packetization (rtp/h264.py) -- VALIDATED

Cross-referenced against:
- Node.js `AnnexBHelper.ts` `splitNalu()` -- our `_split_nalu_impl` handles both 3-byte and 4-byte start codes correctly. Test `test_two_nalus_3byte_then_4byte` and `test_sps_pps_idr_keyframe` confirm.
- RFC 6184 FU-A format -- FU indicator byte `(nalu[0] & 0x60) | 28`, FU header start/end bits match the spec. Test `test_fu_a_fragmentation` confirms.
- Marker bit on last packet of frame -- confirmed by test `test_marker_only_on_last_packet`.

No issues found. Note: STAP-A aggregation is not implemented (documented as optional). Not needed for MVP.

### 2.4 FFmpeg Pipeline (media/ffmpeg.py) -- VALIDATED

Cross-referenced against:
- Node.js `Streamer.ts` + `BaseMediaStream.ts` critical flags -- all present: `-bf 0`, `-preset superfast`, `-forced-idr 1`, `-force_key_frames expr:gte(t,n_forced*1)`, `-pix_fmt yuv420p`.
- NUT container format (`-f nut pipe:1`) matches Node.js reference.
- Bitstream filter chain for H.264 input (h264_mp4toannexb, h264_metadata aud:remove) is NOT in the FFmpeg command. The Node.js lib applies these in the demuxer, not the FFmpeg command. Our PyAV demuxer handles Annex-B format directly, so this is fine.

No issues found.

### 2.5 NUT Demuxing (media/demux.py) -- VALIDATED

Cross-referenced against:
- Node.js `LibavDemuxer.ts` -- our PyAV-based demuxer produces the same frame types (video/audio) with PTS and duration.
- Opus TOC byte parsing (`parse_opus_duration`) matches RFC 6716 exactly. All 32 configurations covered. Tests confirm.
- Keyframe detection via `packet.is_keyframe` and fallback `_contains_idr_nalu` is robust.
- The `_wrap_pipe` method handles asyncio StreamReader to synchronous file-like conversion for PyAV. This is a critical piece that the Node.js lib doesn't need (Node.js uses streams natively).

Potential concern: PyAV's `av.open()` with pipe input and `format='nut'` has not been benchmarked for real-time performance. The previous agent noted this as an open research area. If latency exceeds 5ms/frame, fall back to raw Annex-B parsing.

### 2.6 Frame Pacing (media/pacer.py) -- VALIDATED

Cross-referenced against:
- Node.js `BaseMediaStream.ts` timing algorithm -- our `FramePacer.pace()` implements the same logic: `sleep = pts - start_pts + frametime - (now - start_time)`.
- Sync tolerance of 20ms matches Node.js default.
- Behind/ahead detection with partner stream matches.
- `no_sleep` mode for initial burst matches Node.js `readrate_initial_burst` concept.

No issues found.

### 2.7 Protocol Types (protocol/types.py) -- VALIDATED

Cross-referenced against:
- Node.js `CodecPayloadType.ts` -- all 6 codecs match exactly (opus PT=120, H264 PT=101/RTX=102, etc.).
- Node.js `utils.ts` `generateStreamKey` / `parseStreamKey` -- our implementation matches.
- Node.js `GatewayOpCodes.ts` -- STREAM_CREATE=18, STREAM_DELETE=19, STREAM_SET_PAUSED=22 all present.
- VIDEO opcode payload format matches Node.js `BaseMediaConnection.ts` `setVideoAttributes()`.

No issues found.

### 2.8 SPS VUI Rewriter (protocol/vui.py) -- VALIDATED

Cross-referenced against:
- Node.js `SPSVUIRewriter.ts` (222 lines) -- our Python port (547 lines) is more verbose due to Python's lack of compact bit manipulation, but the logic is identical.
- WebRTC C++ source `sps_vui_rewriter.cc` -- the key changes (force bitstream_restriction=1, max_num_reorder_frames=0, strip video_signal_type) are all present.
- Exp-Golomb reader/writer matches the TypeScript `AnnexBBitstreamReaderWriter.ts`.
- Emulation prevention byte handling (0x000003 skip/insert) is correct.
- Test `test_max_num_reorder_frames_is_zero` confirms the critical invariant.

No issues found.

---

## 3. What Needs to Be Built: Phase 5-6 Detailed Breakdown

### Phase 5A: Stream Connection (stream_connection.py) ~250 lines

This is the core missing piece. It manages a separate voice WebSocket connection to the stream server for Go Live.

**Responsibilities:**
1. Connect to `wss://{endpoint}/?v=8` (the stream voice server)
2. IDENTIFY with server_id, user_id, session_id, token, video=true, streams=simulcast
3. Handle READY: extract stream SSRCs (audio_ssrc, video_ssrc, rtx_ssrc from streams[0])
4. SELECT_PROTOCOL: send UDP protocol with codec configs (same as main voice)
5. Handle SESSION_DESCRIPTION (SELECT_PROTOCOL_ACK): extract secret_key, mode, dave_protocol_version
6. DAVE key exchange if dave_protocol_version > 0
7. Send VIDEO opcode (12) with stream SSRCs
8. Send SPEAKING opcode (5) with mode=2 (priority/soundshare)
9. Heartbeat management
10. Handle DAVE transitions (opcodes 21-31)

**Key invariants from Node.js reference:**
- `daveChannelId = BigInt(serverId) - 1n` (StreamConnection.ts line 18-20)
- `serverId = guildId` for guild, `channelId` for DM/call
- Speaking mode = 2 for Go Live (not 1 which is camera)
- Separate DAVE session from main voice connection
- Separate secret key from stream server's SESSION_DESCRIPTION

**Architecture decision:** Use `websockets` library for the stream WS (independent of dpy-self's curl_cffi transport). This was confirmed in COMPAT_AUDIT.md.

**Reference files:**
- Node.js `BaseMediaConnection.ts` (396 lines) -- full WS lifecycle
- Node.js `StreamConnection.ts` (18 lines) -- Go Live specific overrides
- voice-recv `gateway.py` -- opcode constants and hook pattern

### Phase 5B: Voice Send Extension (voice_send.py) ~150 lines

Extends discord.VoiceClient (or VoiceRecvClient) to add send capability.

**Responsibilities:**
1. Hold references to stream_connection, packetizers, encryptor, pacer
2. Provide `send_video_frame()` and `send_audio_frame()` methods
3. Manage separate video sequence number, timestamp, and nonce counter (independent from audio)
4. Access discord.py's VoiceClient.socket for UDP sending
5. Access discord.py's VoiceClient.secret_key and mode for transport encryption
6. Access dave_session for DAVE frame encryption

**Key insight from discord.py source:**
- `VoiceClient.socket` is the shared UDP socket
- `VoiceClient.secret_key` is the 32-byte key from SESSION_DESCRIPTION
- `VoiceClient.mode` is the encryption mode string
- `VoiceClient._connection` is the VoiceConnectionState with dave_session
- `VoiceClient._connection.add_socket_listener()` registers UDP packet callbacks
- `VoiceClient._connection.endpoint_ip` and `voice_port` are the UDP target

**Critical observation:** discord.py already manages audio SSRC, sequence, timestamp, and nonce counter. For video, we need our OWN counters because video uses a different SSRC and RTP stream.

**Reference files:**
- voice-recv `voice_client.py` `VoiceRecvClient` -- extension pattern
- discord.py `voice_client.py` -- VoiceClient internals
- voice-recv `reader.py` `UDPKeepAlive` -- socket sending pattern

### Phase 5C: Main API (streamer.py) ~250 lines

The user-facing API that orchestrates everything.

**Responsibilities:**
1. `join_voice(guild_id, channel_id)` -- join voice channel via discord.py
2. `start_go_live()` -- send STREAM_CREATE, wait for STREAM_CREATE + STREAM_SERVER_UPDATE, connect stream WS
3. `play(url, options)` -- start FFmpeg, demux, packetize, encrypt, send
4. `stop()` -- stop stream, send STREAM_DELETE
5. `leave()` -- leave voice channel
6. Gateway event hooking for STREAM_CREATE / STREAM_SERVER_UPDATE

**Gateway event flow (from Node.js Streamer.ts):**
```
1. User calls start_go_live()
2. Send STREAM_CREATE (op 18) via gateway: {type, guild_id, channel_id, preferred_region: null}
3. Send STREAM_SET_PAUSED (op 22): {stream_key, paused: false}
4. Gateway responds with STREAM_CREATE event: {stream_key, rtc_server_id}
5. Gateway responds with STREAM_SERVER_UPDATE event: {stream_key, endpoint, token}
6. Connect to stream voice server with endpoint + token
7. Stream WS handshake: IDENTIFY -> READY -> SELECT_PROTOCOL -> SESSION_DESCRIPTION
8. DAVE key exchange
9. Send VIDEO (op 12) with stream SSRCs
10. Send SPEAKING (op 5) with mode=2
11. Start media pipeline: FFmpeg -> demux -> packetize -> DAVE encrypt -> transport encrypt -> UDP send
```

**How to hook gateway events in discord.py:**
- discord.py dispatches `on_socket_raw_receive` for all gateway messages
- Or use `client.event` with custom event names
- Or listen for `VOICE_STATE_UPDATE` and `VOICE_SERVER_UPDATE` via discord.py's built-in dispatch
- For STREAM_CREATE/STREAM_SERVER_UPDATE, we need raw gateway access

**Reference files:**
- Node.js `Streamer.ts` (259 lines) -- complete lifecycle
- voice-recv `voice_client.py` -- discord.py integration pattern

### Phase 5D: DAVE Video Encryption Integration ~50 lines

Wire up dave_session.encrypt() for video frames.

**From COMPAT_AUDIT.md:**
```python
DaveSession.encrypt(media_type: MediaType, codec: Codec, packet: bytes) -> bytes
# MediaType.video = 1
# Codec.h264 = 4
```

**Order (confirmed from Node.js WebRtcWrapper.ts sendVideoFrame):**
1. FFmpeg outputs H.264 frame (Annex-B)
2. SPS VUI rewrite (if SPS NALU found)
3. DAVE encrypt: `dave_session.encrypt(MediaType.video, Codec.h264, frame)`
4. RTP packetize (NALU split -> FU-A)
5. Transport encrypt (AEAD)
6. UDP sendto

**Critical:** DAVE encrypt happens on the COMPLETE frame BEFORE RTP packetization. The encrypted frame is then split across RTP packets.

### Phase 6: Integration + Compatibility ~100 lines

1. `compat/voice_recv.py` -- optional VoiceSendRecvClient extending VoiceRecvClient
2. `__init__.py` exports -- clean public API
3. End-to-end integration test
4. README / documentation

---

## 4. Execution Plan

### Step 1: Environment Setup
- Install dependencies: `dpy-self[voice]`, `websockets`, `PyAV`, `pynacl`
- Verify all Phase 1-4 tests pass
- Verify voice-recv imports work with dpy-self

### Step 2: Phase 5A -- Stream Connection
- Implement `stream_connection.py` with full WS lifecycle
- IDENTIFY, READY, SELECT_PROTOCOL, SESSION_DESCRIPTION handling
- DAVE key exchange (MLS opcodes 25-31)
- Heartbeat management
- Unit test: mock WS, verify opcode sequences

### Step 3: Phase 5B -- Voice Send Extension
- Implement `voice_send.py` extending VoiceClient
- Separate video sequence/timestamp/nonce counters
- `send_video_frame()` with DAVE + RTP + transport encrypt
- `send_audio_frame()` reusing discord.py's existing path
- Unit test: verify packet construction and encryption

### Step 4: Phase 5C -- Main API
- Implement `streamer.py` with full lifecycle
- Gateway event hooking for STREAM_CREATE/STREAM_SERVER_UPDATE
- `join_voice()`, `start_go_live()`, `play()`, `stop()`, `leave()`
- Integration test: connect to Discord, verify stream creation

### Step 5: Phase 5D -- DAVE Video Integration
- Wire `dave_session.encrypt(MediaType.video, Codec.h264, frame)` into send path
- Handle passthrough mode (dave_protocol_version=0)
- Handle DAVE transitions (member join/leave)
- Test with multiple participants

### Step 6: Phase 6 -- Integration + Polish
- `compat/voice_recv.py` for unified client
- Clean `__init__.py` exports
- End-to-end test: join voice -> Go Live -> play video -> viewer sees it
- README with usage examples

---

## 5. Risk Assessment

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| PyAV NUT pipe latency | Medium | Medium | Fallback to raw Annex-B (-f h264 pipe:3) |
| Stream WS uses different DAVE session | Medium | High | Separate DaveSession instance, confirmed in Node.js |
| Gateway event hooking for STREAM_CREATE | Medium | Medium | Use `on_socket_raw_receive` or raw dispatch |
| Go Live daveChannelId mapping | Low | Low | `BigInt(serverId) - 1` confirmed in Node.js |
| UDP socket shared between voice + stream | Low | Low | Both go through same `socket.sendto()` |
| discord.py doesn't expose stream SSRCs | Low | Low | We get SSRCs from stream WS READY, not main voice |
| DAVE session for stream differs from voice | Medium | High | Create separate DaveSession in stream_connection.py |

---

## 6. Dependencies (Environment)

| Package | Version | Purpose | Status |
|---------|---------|---------|--------|
| dpy-self[voice] | >=2.1.0 | Gateway, voice WS, DAVE, UDP socket | Needs install |
| websockets | >=12.0 | Go Live stream voice WS | Needs install |
| PyAV | >=12.0 | NUT demuxing from FFmpeg pipe | Needs install |
| pynacl | >=1.5.0 | Transport encryption (AEAD) | Needs install |
| davey | >=0.1.5 | DAVE/E2EE (bundled with dpy-self[voice]) | Needs install |
| FFmpeg | system | Video/audio transcoding | Needs verify |

---

## 7. File Map (What Exists + What to Build)

```
discord_video_stream/
    __init__.py                  # EXISTS (1 line) -- needs exports
    streamer.py                  # BUILD  (~250 lines) -- Main API
    voice_send.py                # BUILD  (~150 lines) -- VoiceClient extension
    stream_connection.py         # BUILD  (~250 lines) -- Go Live WS + media
    rtp/
        __init__.py              # EXISTS (26 lines)
        serialize.py             # EXISTS (144 lines)
        h264.py                  # EXISTS (271 lines)
        crypto.py                # EXISTS (111 lines)
    media/
        __init__.py              # EXISTS (23 lines)
        ffmpeg.py                # EXISTS (214 lines)
        demux.py                 # EXISTS (355 lines)
        pacer.py                 # EXISTS (204 lines)
    protocol/
        __init__.py              # EXISTS (46 lines)
        types.py                 # EXISTS (204 lines)
        vui.py                   # EXISTS (547 lines)
    compat/
        __init__.py              # EXISTS (1 line) -- needs implementation
        voice_recv.py            # BUILD  (~80 lines) -- Optional integration
    tests/
        __init__.py              # EXISTS
        test_rtp_crypto.py       # EXISTS (313 lines)
        test_h264.py             # EXISTS (232 lines)
        test_ffmpeg_demux.py     # EXISTS (309 lines)
        test_pacer.py            # EXISTS (240 lines)
        test_protocol.py         # EXISTS (424 lines)
        test_integration.py      # EXISTS (156 lines)
        test_pipeline_real.py    # EXISTS (454 lines)
        test_stream_connection.py # BUILD  (~200 lines)
        test_go_live.py          # BUILD  (~150 lines)

EXISTING: 4,275 lines (21 files)
TO BUILD: ~1,580 lines (7 files)
TOTAL:    ~5,855 lines
```

---

## 8. Immediate Next Actions

1. **Install dependencies** in the working environment
2. **Run existing tests** to verify Phase 1-4 integrity
3. **Start Phase 5A** (stream_connection.py) -- this is the critical path item
4. **Then Phase 5B** (voice_send.py) -- depends on 5A for SSRCs and secret key
5. **Then Phase 5C** (streamer.py) -- depends on 5A and 5B
6. **Then Phase 5D** -- DAVE video integration (depends on 5B)
7. **Finally Phase 6** -- integration and polish

Phase 5A is the most complex piece. Everything else builds on it.

---

## 9. Key Technical Decisions Confirmed

| Decision | Rationale | Source |
|----------|-----------|--------|
| Use UDP path (not WebRTC) | discord.py provides UDP socket directly | COMPAT_AUDIT.md, CONTEXT.md |
| Use `websockets` for stream WS | Independent of dpy-self's curl_cffi | COMPAT_AUDIT.md |
| Use davey (not dave.py) | discord.py/dpy-self requires davey | COMPAT_AUDIT.md |
| Separate DaveSession for stream | Stream has own daveChannelId | Node.js StreamConnection.ts |
| Speaking mode=2 for Go Live | Confirmed in Node.js reference | StreamConnection.ts |
| daveChannelId = serverId - 1 | Confirmed in Node.js reference | StreamConnection.ts line 18-20 |
| DAVE encrypt before RTP packetize | Confirmed in Node.js reference | WebRtcWrapper.ts sendVideoFrame |
| Video needs own seq/ts/nonce | Different SSRC from audio | Architecture analysis |
| Use discord.py VoiceClient as base | Handles 60% of protocol | FINAL_PLAN.md |
