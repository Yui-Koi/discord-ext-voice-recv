# Bug Analysis and Handoff Document

## Date: 2026-04-23
## Purpose: Comprehensive bug inventory for continuation agent

---

## Project Context

This is a Python port of `@dank074/discord-video-stream` (Node.js, 4319 lines) to Python, built on top of discord.py-self (dpy-self) and discord-ext-voice-recv. The deliverable is a package capable of sending H.264 video + Opus audio via Discord Go Live in both guild and DM/group voice channels.

Phases 1-6 were implemented by a previous agent. This session performed deep analysis, identified 17 static bugs and 4 runtime bugs, fixed 15 static and 2 runtime bugs, and verified the full pipeline against a live Discord server.

**Repository:** `Yui-Koi/discord-ext-voice-recv`, branch `video-stream-port`
**Latest commit:** `26fe28a`
**Test count:** 232 passing (dpy-self 2.1.0, davey 0.1.0, pynacl 1.5.0, PyAV 17.0.1, websockets 16.0)

---

## Architecture Overview

```
FFmpeg (subprocess, NUT pipe to stdout, stderr drained by background task)
    |
    v
Demuxer (PyAV, synchronous with asyncio.sleep(0) every 8 frames for event loop yield)
    |
    +---> VideoFrame ---> SPS VUI Rewrite ---> DAVE Encrypt ---> H.264 Packetize ---> Transport Encrypt ---> UDP Send
    |
    +---> AudioFrame ---> DAVE Encrypt ---> Opus RTP Packetize ---> Transport Encrypt ---> UDP Send
    |
FramePacer (event-based sync with asyncio.Event, not polling)
    |
StreamConnection (separate voice WS to stream server, own SSRCs, secret key, DAVE session)
    |
VideoStreamer (orchestrator, gateway event hooking, lifecycle management)
```

**Key architectural decisions:**
- UDP path (not WebRTC) for media transport
- discord.py's VoiceClient as base (handles 60% of protocol)
- davey for DAVE (not dave.py)
- `websockets` library for stream WS (independent of dpy-self's curl_cffi)
- Separate SSRCs, sequence numbers, timestamps, nonce counters for video and audio
- Gateway events intercepted via `socket_raw_receive` dispatch (patched to work without debug mode)

---

## Static Bug Inventory

### CRITICAL (all fixed)

| ID | File | Description | Fix |
|----|------|-------------|-----|
| BUG-01 | streamer.py | Audio frames never sent | Added AudioSender class (Opus PT 120, 48kHz) |
| BUG-02 | voice_send.py | No packet-level pacing | Added 25 Mbps pacing in send loop |
| BUG-03 | media/demux.py | Demuxer blocks event loop | asyncio.sleep(0) every 8 frames, explicit fd cleanup |
| BUG-04 | compat/voice_recv.py | Video to wrong endpoint | Falls back to stream ready_params endpoint |

### HIGH (all fixed)

| ID | File | Description | Fix |
|----|------|-------------|-----|
| BUG-05 | media/ffmpeg.py | stderr deadlock risk | Background drain task, loglevel changed to warning |
| BUG-06 | stream_connection.py | No reconnection | _attempt_reconnect with exponential backoff (3 attempts) |
| BUG-07 | stream_connection.py | Fire-and-forget critical sends | Added _send_json_await/_send_binary_await, used for IDENTIFY, SELECT_PROTOCOL, MLS ops |

### MEDIUM (all fixed)

| ID | File | Description | Fix |
|----|------|-------------|-----|
| BUG-12 | media/pacer.py | Busy-wait sync polling | asyncio.Event-based sync notification |
| BUG-13 | media/demux.py | FD leak in _wrap_pipe | Returns (file_obj, dup_fd), explicit close in finally |
| BUG-16 | streamer.py | Hardcoded video attrs | Moved to play(), uses StreamOptions resolution |

### LOW (some fixed, some open)

| ID | File | Description | Status |
|----|------|-------------|--------|
| BUG-09 | voice_send.py | Duplicate seq/ts state in VideoSender | OPEN - cosmetic, no runtime impact |
| BUG-10 | rtp/h264.py | Dead code in split_nalu | FIXED - inlined clean implementation |
| BUG-11 | all files | try/except ImportError pattern | OPEN - requires test infra change |
| BUG-14 | stream_connection.py | identify() sync | FIXED - now async, awaits send |
| BUG-15 | stream_connection.py | _ready_event in connect() | FIXED - moved to __init__ |
| BUG-17 | streamer.py | FFmpeg stop fire-and-forget | OPEN - would require API change (stop is sync) |

### RETRACTED

| ID | Description | Reason |
|----|-------------|--------|
| BUG-08 | RTCP NTP timestamp | Re-analyzed, computation is correct |

---

## Runtime Bug Inventory (from live Discord testing)

### RT-BUG-01: Gateway hook requires _enable_debug_events -- FIXED

**Root cause:** dpy-self only dispatches `socket_raw_receive` when `_enable_debug_events` is True (`gateway.py` line 405: `ws.log_receive = ws.debug_log_receive`). The entire gateway hooking mechanism was non-functional without debug mode.

**Fix:** Added `_ensure_gateway_dispatch()` to `VideoStreamer` that patches the gateway WS `log_receive` to always dispatch `socket_raw_receive`. Called in `join_voice()` after `channel.connect()`.

**Verification:** Live test confirmed STREAM_CREATE, STREAM_SERVER_UPDATE, VOICE_STATE_UPDATE all received correctly without `_enable_debug_events`.

### RT-BUG-02: FFmpeg lavfi input format -- FIXED

**Root cause:** FFmpeg rejects `lavfi:color=...` as a URL protocol. Requires `-f lavfi -i color=...` format.

**Fix:** `_build_command()` in `ffmpeg.py` detects `lavfi:` prefix and splits into `-f lavfi -i <filter>`.

**Verification:** FFmpeg starts successfully, outputs NUT data, exits with code 0.

### RT-BUG-03: Secret key timing race -- NOT YET FIXED

**Root cause:** `start_go_live()` returns before SELECT_PROTOCOL_ACK (containing secret_key) arrives. The secret key is available by the time `play()` is called if there is a delay between the two calls.

**Impact:** If `play()` is called immediately after `start_go_live()`, `VideoSender.start()` may not find the secret key.

**Suggested fix:** Add a short wait loop in `VideoSender.start()` that waits for `stream_conn.secret_key` to become available (e.g., up to 2 seconds).

### RT-BUG-04: Unhandled opcode 15 -- NOT YET FIXED

**Root cause:** The stream WS receives opcode 15 (pixel counts / MEDIA_SINK_WANTS) which is not in the handler.

**Impact:** None. Informational only. Should be acknowledged with a no-op to suppress the "Unhandled" debug log.

---

## Live Test Results (2026-04-23)

**Environment:** dpy-self 2.1.0, davey 0.1.0, PyAV 17.0.1, websockets 16.0
**Guild:** Lazarus's server (1410308800979664908)
**Channel:** General (1410308801755873345)

| Phase | Result | Details |
|-------|--------|---------|
| Login | OK | User: zaplarus (814369794030829598) |
| Voice join | OK | Socket, secret_key, dave_session all present |
| Gateway hook | OK | STREAM_CREATE, STREAM_SERVER_UPDATE received |
| Stream WS connect | OK | wss://c-sin20-959ffcd1.discord.media:443/?v=9 |
| IDENTIFY | OK | server_id=1496737669139533925 |
| READY | OK | audio_ssrc=389, video_ssrc=390, rtx_ssrc=391 |
| SELECT_PROTOCOL | OK | UDP, aead_xchacha20_poly1305_rtpsize |
| SESSION_DESCRIPTION | OK | secret_key=32 bytes, dave_version=1 |
| DAVE init | OK | MLS key package sent, external sender received |
| SPEAKING | OK | mode=2 (priority/soundshare) |
| VIDEO attributes | OK | 640x480 |
| FFmpeg | OK | lavfi input, libx264+libopus, NUT output, exit 0 |
| Demuxer | OK | video=True, audio=False (lavfi source has no audio) |
| Video send | OK | Stream active for 5 seconds |
| Frame pacing | OK | Logged "Stream is ahead by 66.7ms, waiting" |
| RTCP SR | OK | Loop running, cancelled on cleanup |
| STREAM_DELETE | OK | Sent on stop |
| Cleanup | OK | Stop, leave, disconnect all clean |

---

## What Remains (for next agent)

### Must-fix before production

1. **RT-BUG-03:** Secret key timing race. Add a wait loop in `VideoSender.start()` for `stream_conn.secret_key` to become available.

2. **RT-BUG-04:** Handle opcode 15 (add to handler as no-op).

3. **Audio in Go Live:** The test used a video-only lavfi source. Need to test with audio (e.g., a real video file) and verify audio is sent correctly through the AudioSender.

4. **DAVE readiness:** The test showed `dave_ready: False` at go_live time. The DAVE session was initialized but not yet in ready state. Need to verify that DAVE encrypt actually works after the MLS handshake completes. Currently, `_dave_encrypt()` falls back to passthrough when DAVE is not ready, which is correct behavior but means frames are unencrypted until DAVE is ready.

### Should-fix for robustness

5. **BUG-09:** Remove duplicate `_sequence`/`_timestamp` from VideoSender.

6. **BUG-11:** Fix try/except ImportError pattern across all files.

7. **BUG-17:** Make `stop()` async or add proper cleanup tracking for FFmpeg.

8. **RT-BUG-03 (timing):** Consider adding a `_wait_for_session_description()` helper that `play()` calls before starting the pipeline.

### Nice-to-have

9. **Raw Annex-B fallback:** If PyAV latency becomes problematic, implement the raw pipe approach documented in FINAL_PLAN Task 3.2.

10. **AES-256-GCM support:** The code declares it but always uses xchacha20. Low priority since xchacha20 works.

11. **Stream preview:** REST API call for Go Live preview image (documented in OBSERVATIONS.md).

12. **Simulcast:** Multiple quality levels (documented but not implemented).

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `discord_video_stream/streamer.py` | Main API, orchestrator |
| `discord_video_stream/voice_send.py` | VideoSender + AudioSender |
| `discord_video_stream/stream_connection.py` | Go Live WS connection |
| `discord_video_stream/media/ffmpeg.py` | FFmpeg subprocess |
| `discord_video_stream/media/demux.py` | PyAV NUT demuxer |
| `discord_video_stream/media/pacer.py` | Frame pacing + sync |
| `discord_video_stream/rtp/serialize.py` | RTP header builder |
| `discord_video_stream/rtp/h264.py` | H.264 NALU packetizer |
| `discord_video_stream/rtp/crypto.py` | Transport encryption |
| `discord_video_stream/protocol/types.py` | Codec configs, stream keys |
| `discord_video_stream/protocol/vui.py` | SPS VUI rewriter |
| `discord_video_stream/compat/voice_recv.py` | VoiceRecvClient integration |
| `docs/CONTEXT.md` | Exhaustive protocol research |
| `docs/OBSERVATIONS.md` | Node.js reference analysis |
| `docs/FINAL_PLAN.md` | Architecture decisions |
| `docs/INTEGRATION.md` | discord.py integration analysis |
| `docs/ROOT_CAUSE_ANALYSIS.md` | What went wrong in MVP |
| `docs/ANALYSIS_AND_PLAN.md` | Detailed bug inventory + refactor plan |
