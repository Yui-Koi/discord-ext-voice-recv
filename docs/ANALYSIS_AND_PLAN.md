# Deep Analysis and Refactor Plan

## Date: 2026-04-23

---

## 1. Current State Assessment

### What Exists

Phases 1 through 6 are implemented. The codebase comprises 24 Python files totaling approximately 8,283 lines (implementation and tests combined). 205 unit/integration tests were passing in the previous agent's environment. The pipeline is architecturally complete: FFmpeg transcoding, NUT demuxing, H.264 packetization, DAVE frame encryption, transport encryption, frame pacing, Go Live stream lifecycle, and an optional voice-recv compatibility module.

### Dependency Environment

The previous agent's environment had: dpy-self 2.1.0, davey 0.1.0, pynacl 1.5.0, PyAV 17.0.1, websockets 16.0, FFmpeg 6.1.1. This machine does not have pynacl, websockets, PyAV, or dpy-self installed. The analysis below is based on code inspection, not runtime verification.

---

## 2. Architecture Overview

```
FFmpeg (subprocess, NUT pipe)
    |
    v
Demuxer (PyAV, sync fd bridge)
    |
    +---> VideoFrame ---> SPS VUI Rewrite ---> DAVE Encrypt ---> H.264 Packetize ---> Transport Encrypt ---> UDP Send
    |                                                                                                              |
    +---> AudioFrame ---> (discord.py existing audio send path, NOT used for Go Live stream audio)                 |
                                                                                                                    |
FramePacer (timing + A/V sync) ----> controls when frames are sent                                                 |
                                                                                                                    |
StreamConnection (separate voice WS) ----> SSRCs, secret key, DAVE session for stream                              |
                                                                                                                    |
VideoStreamer (orchestrator) ----> gateway events, lifecycle, FFmpeg management                                     |
```

The architecture is sound in principle. The separation between the main voice connection (handled by discord.py) and the stream connection (our StreamConnection) is correct. The issue is in implementation quality and several invariant violations.

---

## 3. Critical Bugs and Invariant Violations

### 3.1 Demuxer Blocks the Event Loop

**Location:** `media/demux.py`, `Demuxer.demux()` method.

The `_wrap_pipe()` method duplicates the file descriptor from asyncio's StreamReader and sets it to blocking mode. The `av.open()` and `container.demux()` calls are synchronous PyAV operations executed inside an `async for` generator. This means the entire event loop blocks on every PyAV read operation.

**Impact:** While PyAV is reading from the NUT pipe, no other coroutines can execute. This includes heartbeats to both the main gateway and the stream voice server, frame pacing, and any other concurrent tasks. If PyAV blocks for an extended period (e.g., waiting for FFmpeg to produce data), heartbeats will time out and the connection will be severed.

**Root cause:** PyAV's `av.open()` requires a synchronous file-like object with a `.read()` method. asyncio's StreamReader has an async `.read()`. The previous agent solved this with `os.dup()` + `fcntl` to set blocking mode, but this defeats the purpose of asyncio.

**Fix:** Wrap the synchronous PyAV iteration in `asyncio.to_thread()` or `loop.run_in_executor()`. Alternatively, use the raw Annex-B fallback approach: have FFmpeg output raw H.264 to one pipe (`-f h264 pipe:3`) and raw Opus to another (`-f data -c:a copy pipe:4`), then read from both pipes asynchronously using asyncio's stream reader directly. This avoids PyAV entirely for the demuxing step.

### 3.2 Dead Code in split_nalu()

**Location:** `rtp/h264.py`, `split_nalu()` function.

The function begins with an implementation attempt that discovers a start code, then hits a logic error in handling 4-byte vs 3-byte start codes. It contains a `break` statement after a `pass` comment ("Let me redo this more carefully"), then falls through to `_split_nalu_impl()`. The first ~30 lines of `split_nalu()` are dead code that never executes past the `break`.

**Impact:** No runtime impact (the function delegates to `_split_nalu_impl()` which works correctly). However, this is a code quality issue that signals rushed implementation and makes the function confusing to read.

**Fix:** Remove the dead code from `split_nalu()` and have it directly call `_split_nalu_impl()`, or inline `_split_nalu_impl()` as the sole implementation.

### 3.3 compat/voice_recv.py send_video_packet Sends to Wrong Endpoint

**Location:** `compat/voice_recv.py`, `VoiceSendRecvClient.send_video_packet()`.

```python
def send_video_packet(self, packet: bytes) -> None:
    self._connection.socket.sendto(
        packet,
        (self._connection.endpoint_ip, self._connection.voice_port),
    )
```

This sends video packets to the MAIN voice connection's endpoint. For Go Live, video must go to the STREAM server's endpoint (which is different). This is the same bug that was fixed in commit `68b6266` for the main pipeline, but the compat module was not updated.

**Impact:** If someone uses `VoiceSendRecvClient` and calls `send_video_packet()` directly (rather than through `VideoStreamer`), video will never reach Discord. Error 2012.

**Fix:** The compat module's `send_video_packet` should accept an endpoint parameter, or it should read from the stream connection's ready_params (as `VideoSender._send_udp` does after the fix).

### 3.4 Demuxer Async Generator Does Not Release Control

**Location:** `media/demux.py`, `Demuxer.demux()`.

The `for packet in container.demux()` loop is synchronous. Even though the function is an `async generator`, the loop body never yields control to the event loop until a `yield` statement is reached. If PyAV produces many packets in a burst (e.g., at the start of a file), the loop will consume all of them synchronously before yielding the first frame.

**Impact:** Burst reads cause temporary event loop starvation. Combined with 3.1, this compounds the heartbeat timeout risk.

**Fix:** Add an `await asyncio.sleep(0)` periodically inside the loop (e.g., every N packets) to yield control, or better, move the entire demux loop to a thread.

### 3.5 FFmpeg stderr Not Monitored

**Location:** `media/ffmpeg.py`, `FFmpegProcess`.

The process is started with `stderr=asyncio.subprocess.PIPE`, but nothing reads from it during operation. The only method is `read_stderr()` which blocks until the process ends. If FFmpeg produces enough stderr output (e.g., verbose logging with `-loglevel verbose`), the pipe buffer fills up, and FFmpeg blocks on writing to stderr, which blocks its stdout writes, which blocks the entire pipeline.

**Impact:** Pipeline deadlock under certain conditions (long-running streams with verbose FFmpeg output).

**Fix:** Spawn a background task to continuously drain stderr. Either discard it, log it, or buffer it in memory. The `-loglevel verbose` flag should probably be changed to `-loglevel warning` or `-loglevel error` for production to reduce stderr volume.

### 3.6 Stream Connection Has No Reconnection Logic

**Location:** `stream_connection.py`, `StreamConnection._receive_loop()`.

When the stream WebSocket connection closes with a resumable code (4015 or < 4000), the code sets `self.state.resuming = True` and logs an intent to resume, but never actually attempts reconnection. The `resume()` method exists but is never called automatically.

**Impact:** Any network hiccup or Discord server restart kills the stream permanently. The user must manually stop and restart.

**Fix:** Implement automatic reconnection with exponential backoff. After a resumable disconnect, wait briefly, call `resume()`, and if that fails, fall back to full reconnection.

### 3.7 Fire-and-Forget Send Tasks Swallow Errors

**Location:** `stream_connection.py`, `_send_json()` and `_send_binary()`; `streamer.py`, `_send_gateway()`.

All three methods use `asyncio.ensure_future()` with a done callback that logs the error. The calling code has no way to know if the send failed. For critical operations (IDENTIFY, SELECT_PROTOCOL, MLS_COMMIT_WELCOME), a silent failure means the handshake hangs indefinitely.

**Impact:** Silent failures in protocol handshake. The `connect()` method will timeout waiting for READY if IDENTIFY silently fails.

**Fix:** For critical operations, await the send directly or use an Event to signal completion. For non-critical operations (SPEAKING, heartbeats), fire-and-forget with logging is acceptable.

### 3.8 FramePacer Busy-Wait Loop

**Location:** `media/pacer.py`, `FramePacer.pace()`, the "ahead" branch.

```python
while self._sync_enabled and self._sync_partner is not None:
    partner_pts = self._sync_partner._pts
    if partner_pts is None:
        break
    delta = pts_ms - partner_pts
    if not self._is_ahead(delta, frametime_ms):
        break
    await asyncio.sleep(frametime_ms / 1000)
```

This polls the sync partner's PTS at `frametime_ms` intervals. For video at 30fps, this means checking every 33ms. This is wasteful; the pacer should be notified when the partner's PTS updates, rather than polling.

**Impact:** Minor CPU waste. The loop does yield to the event loop via `asyncio.sleep()`, so it does not block. But it introduces up to `frametime_ms` of latency when the partner catches up.

**Fix:** Use an `asyncio.Event` that the sync partner sets when its PTS updates. The ahead stream awaits the event instead of polling.

---

## 4. Architectural Issues and Trade-Offs

### 4.1 The NUT Container Choice

The NUT container was chosen because it has low overhead and is designed for streaming. However, PyAV's NUT support has quirks:
- `is_keyframe` is not reliably reported
- Duration information for Opus frames is sometimes missing
- The synchronous API conflicts with asyncio

**Alternative:** Raw Annex-B for video (`-f h264 pipe:3`) and raw Opus (`-f data -c:a copy pipe:4`). This eliminates PyAV entirely. FFmpeg outputs raw elementary streams, which we read directly from asyncio pipes. The H.264 stream is already in Annex-B format (start codes), so we just need to split on start codes and packetize. The Opus stream is raw Opus packets.

**Trade-off:** Raw pipes lose the container's framing, so we need a way to delimit frames. For H.264, start codes serve as delimiters. For Opus, we need to know frame boundaries. FFmpeg's `-f data` with `-c:a copy` outputs raw Opus frames, but without length prefixes. We would need to parse Opus TOC bytes to determine frame length, which is already implemented in `parse_opus_duration()`.

**Recommendation:** Implement the raw pipe approach as the primary path. Keep NUT as a fallback for cases where the raw approach fails.

### 4.2 The Gateway Event Hooking Pattern

The current implementation hooks into `on_socket_raw_receive` by chaining onto any existing handler. This is fragile:
- If another extension also chains, the order matters
- The chained handler is a closure that captures `prev`, creating a reference cycle
- dpy-self may not fire `on_socket_raw_receive` for all message types

**Alternative:** Use discord.py's `socket_raw_receive` event listener via `client.event()` or `client.add_listener()`. This is the official extension point and avoids chaining.

**Recommendation:** Switch to `client.add_listener()` for `on_socket_raw_receive`. This is cleaner and works with any number of concurrent listeners.

### 4.3 The Stream Connection Lifecycle

The current flow:
1. `VideoStreamer.join_voice()` connects to voice channel
2. `VideoStreamer.start_go_live()` sends STREAM_CREATE/STREAM_SET_PAUSED via main gateway
3. Waits for STREAM_CREATE and STREAM_SERVER_UPDATE events
4. Creates `StreamConnection` and calls `connect()`
5. `connect()` opens WS, sends IDENTIFY, waits for READY, sends SELECT_PROTOCOL

The problem is that steps 2-3 and 4-5 are tightly coupled but split across two classes. The `VideoStreamer` manages the gateway events, while `StreamConnection` manages the voice WS. If the gateway events arrive before `StreamConnection` is created, they are lost.

**Recommendation:** Merge the gateway event handling into `StreamConnection` itself. The stream connection should own the entire lifecycle: listening for gateway events, connecting to the WS, and managing the media pipeline. `VideoStreamer` becomes a thin facade that delegates to `StreamConnection`.

### 4.4 Audio Handling for Go Live

The current implementation has a gap: FFmpeg produces both video and audio frames, but the audio path is unclear. The `_send_loop` in `streamer.py` paces audio frames but does not actually send them anywhere. The comment says "Audio is handled by discord.py's existing send path", but this is incorrect for Go Live.

For Go Live, audio must be sent through the STREAM connection's RTP path, not the main voice connection's audio path. The stream has its own SSRCs and secret key. Audio needs its own RTP packetization (Opus PT 120), transport encryption, and UDP sending to the stream server's endpoint.

**Impact:** Audio is not being sent in Go Live streams. Viewers see video but hear nothing.

**Fix:** Add an audio sender analogous to `VideoSender`. It should:
1. Use the stream connection's audio SSRC
2. Packetize Opus frames (single RTP packet per Opus frame, no fragmentation needed)
3. Transport encrypt with the stream's secret key
4. Send to the stream server's endpoint

### 4.5 The SPS VUI Rewriter Completeness

The rewriter handles the standard SPS fields and VUI section. However, it does not handle:
- `seq_scaling_list_data()` for High profile (the current code copies scaling lists verbatim, which is correct, but the size calculation for the scaling list loop uses `size = 64 if i >= 6 else 16` which matches the H.264 spec)
- SPS extension (for MVC/SVC, not relevant for Discord)
- The `reserved_zero_2bits` fields

These are minor. The rewriter is functionally complete for Discord's use case.

---

## 5. Code Quality Issues

### 5.1 Inconsistent Import Handling

Every file has a `try: from .X import Y / except ImportError: from X import Y` pattern. This is because the tests use `sys.path.insert()` rather than proper package imports. The fallback imports break package isolation and make the code harder to reason about.

**Fix:** Use proper package imports everywhere. Run tests with `python -m pytest` from the package root, which handles package resolution correctly.

### 5.2 Unused Imports and Dead Code

- `streamer.py` imports `VideoSender` but the `_video_sender` field is set in `play()` and used in `_send_loop()`, yet `VideoSender` methods are called through `self._video_sender` which is correct but the field is also set to `None` in `stop()` before the send loop might have fully cleaned up.
- `voice_send.py` imports `struct` but never uses it directly (it is used indirectly through `rtp.serialize`).
- The `_rewrite_sps` boolean in `VideoSender` is always `True` and cannot be configured.

### 5.3 Type Annotation Inconsistencies

Some files use `from __future__ import annotations` while others do not. The `TYPE_CHECKING` guard is used inconsistently. Some methods lack return type annotations.

### 5.4 Missing __all__ in Some Modules

`stream_connection.py` defines `__all__`, but `voice_send.py` and `streamer.py` have incomplete `__all__` lists.

---

## 6. Validation Against dpy-self and discord-ext-voice-recv

### 6.1 dpy-self Compatibility (Confirmed)

- `VoiceClient` API is identical between discord.py 2.7.1 and dpy-self 2.1.0
- `VoiceConnectionState` attributes (socket, secret_key, dave_session, mode, endpoint_ip, voice_port) are confirmed present
- davey 0.1.0 API matches: `DaveSession.encrypt(media_type, codec, packet)`, `encrypt_opus()`, `ready`, `process_proposals()`, etc.
- Gateway dispatch: `socket_raw_receive` fires for all gateway messages
- WebSocket transport: dpy-self uses curl_cffi internally, but the external API is the same

### 6.2 voice-recv Compatibility (Confirmed with Caveat)

- `VoiceRecvClient` extends `discord.VoiceClient` correctly
- DM voice fixes present (guild.get_member fallback to client.get_user)
- `PacketDecryptor` supports `aead_xchacha20_poly1305_rtpsize` matching our `TransportEncryptor`
- Gateway hook pattern works with dpy-self's `DiscordVoiceWebSocket`
- **Caveat:** The compat module's `send_video_packet()` sends to the wrong endpoint (see 3.3)

### 6.3 Node.js Reference Alignment

All confirmed protocol details match:
- Go Live `daveChannelId = BigInt(serverId) - 1n` (StreamConnection.ts)
- Speaking mode = 2 for Go Live (StreamConnection.ts `setSpeaking`)
- Codec payload types: H264 PT 101, RTX 102, Opus PT 120
- SPS VUI rewriter logic matches WebRTC C++ source
- Frame pacing algorithm matches BaseMediaStream.ts
- DAVE encrypt before RTP packetize (WebRtcWrapper.ts `sendVideoFrame`)

### 6.4 One Discrepancy Found

The `StreamConnection` connects to `wss://{endpoint}/?v=9` while the Node.js reference uses `/?v=8`. The devlog notes this was changed from v8 to v9 in commit `68b6266`. Discord's voice WebSocket supports both versions. v9 adds `channel_id` to IDENTIFY, which the Python implementation sends. This is correct.

---

## 7. Refactor Plan

### Phase 7A: Critical Bug Fixes (Immediate)

| # | Issue | File | Fix |
|---|-------|------|-----|
| 1 | Compat module sends video to wrong endpoint | `compat/voice_recv.py` | Read endpoint from stream_conn.ready_params |
| 2 | FFmpeg stderr deadlock risk | `media/ffmpeg.py` | Add stderr drain task, change loglevel to warning |
| 3 | Dead code in split_nalu | `rtp/h264.py` | Remove abandoned first implementation |
| 4 | Demuxer blocks event loop | `media/demux.py` | Wrap PyAV iteration in asyncio.to_thread() |

### Phase 7B: Demuxer Refactor (High Priority)

Replace the PyAV-based demuxer with a raw pipe approach:

1. FFmpeg outputs raw H.264 Annex-B to `-f h264 pipe:3`
2. FFmpeg outputs raw Opus to `-f data -c:a copy pipe:4`
3. Use asyncio subprocess with extra pipes (3 and 4) via `pass_fds`
4. Read H.264 NALUs by scanning for start codes in the async pipe
5. Read Opus frames by parsing TOC bytes for frame length
6. This eliminates PyAV as a dependency entirely

Alternatively, keep PyAV but run it in a thread:

```python
async def demux(self, pipe) -> AsyncIterator[MediaFrame]:
    sync_pipe = self._wrap_pipe(pipe)
    container = av.open(sync_pipe, format=self._format, ...)
    loop = asyncio.get_event_loop()
    # Run blocking iteration in thread pool
    queue = asyncio.Queue(maxsize=32)

    def _demux_thread():
        for packet in container.demux():
            frame = self._process_packet(packet, ...)
            loop.call_soon_threadsafe(queue.put_nowait, frame)
        loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

    threading.Thread(target=_demux_thread, daemon=True).start()

    while True:
        frame = await queue.get()
        if frame is None:
            break
        yield frame
```

### Phase 7C: Audio Send Path for Go Live (High Priority)

Create `media/audio_sender.py` or extend `voice_send.py` to handle audio:

1. Accept Opus frames from the demuxer
2. Build RTP packets with Opus PT 120, clock rate 48000
3. Transport encrypt with stream secret key
4. Send to stream server endpoint
5. Maintain separate audio sequence/timestamp from video

### Phase 7D: Stream Connection Resilience (Medium Priority)

1. Implement automatic reconnection with exponential backoff
2. Add proper error propagation for critical WS sends
3. Add connection health monitoring (heartbeat RTT tracking)
4. Implement graceful degradation when DAVE is unavailable

### Phase 7E: Import and Test Infrastructure Cleanup (Medium Priority)

1. Remove all `try/except ImportError` fallback import patterns
2. Set up proper `pyproject.toml` with package metadata
3. Configure pytest properly with `conftest.py`
4. Add integration test that requires actual Discord credentials (marked as `@pytest.mark.integration`)

### Phase 7F: Performance and Polish (Low Priority)

1. Replace FramePacer polling with event-based sync notification
2. Add RTP header extension support (one-byte format, RFC 8285)
3. Add RTCP NACK handling for packet retransmission
4. Add stream preview support (REST API call)
5. Add multi-quality simulcast support

---

## 8. Execution Order

```
Phase 7A (Bug Fixes) -----> Phase 7B (Demuxer) -----> Phase 7C (Audio Send)
                                                            |
Phase 7D (Resilience) -----> Phase 7E (Cleanup) -----------+
                                                            |
                                                            v
                                                    Phase 7F (Polish)
```

Phase 7A is prerequisite for everything. Phase 7B and 7C can be parallelized. Phase 7D and 7E can be parallelized. Phase 7F depends on all prior phases.

---

## 9. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| PyAV thread safety | Medium | Use raw pipe approach instead |
| Discord protocol changes | Low | Pin to v9, monitor discord-api-docs |
| davey API changes | Low | Pin to 0.1.x, test against new versions |
| FFmpeg version incompatibility | Low | Test against FFmpeg 5.x and 6.x |
| Selfbot detection | Unknown | Use identical timing/patterns to Node.js lib |
| DAVE MLS group size limits | Low | Discord limits voice to 25 users |

---

## 10. Files Requiring Changes (Priority Order)

| File | Phase | Change Description |
|------|-------|--------------------|
| `compat/voice_recv.py` | 7A | Fix video endpoint bug |
| `media/ffmpeg.py` | 7A | Add stderr drain, change loglevel |
| `rtp/h264.py` | 7A | Remove dead code from split_nalu |
| `media/demux.py` | 7B | Thread-wrapped or raw pipe demuxer |
| `voice_send.py` | 7C | Add audio send capability |
| `stream_connection.py` | 7D | Add reconnection logic |
| `streamer.py` | 7D | Wire audio sender, improve error handling |
| `media/pacer.py` | 7F | Event-based sync notification |
| `protocol/types.py` | 7E | Cleanup |
| `rtp/serialize.py` | 7F | Add RTP header extensions |
| `rtp/crypto.py` | 7E | Add AES-256-GCM support |
| All `__init__.py` | 7E | Fix imports |
| All test files | 7E | Fix imports, add pytest markers |
