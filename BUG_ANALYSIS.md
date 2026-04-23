# Bug Analysis: Known Issues to Tackle

Known bugs and architectural issues in the current implementation. Each bug includes severity, location, root cause, and suggested fix. These are categorized by subsystem.

**Context**: The current implementation was built as an MVP with a file-for-file port strategy from Node.js to Python. This introduced bugs due to: (1) differences in execution model (Node.js streams vs Python asyncio), (2) incomplete tracking of invariants and constraints, (3) sloppy acceptance criteria, (4) architecture that worked on paper but had runtime behavior diverging from the reference.

---

## Critical Bugs

### BUG-01: PyAV demuxer blocks the asyncio event loop

**Location**: `discord_video_stream/media/demux.py`, `Demuxer.demux()` method

**Root cause**: PyAV's `av.open()` and `container.demux()` are synchronous operations. The `_wrap_pipe()` method duplicates the file descriptor from asyncio's StreamReader and sets it to blocking mode. While PyAV reads from the pipe, the entire asyncio event loop is frozen. No heartbeats, no pacing, no other coroutines can execute.

**Impact**: If PyAV blocks for extended periods (e.g., waiting for FFmpeg to produce data), heartbeats to both the main gateway and stream voice server will time out, severing the connections.

**Suggested fix**: Wrap the PyAV iteration in `asyncio.to_thread()` or `loop.run_in_executor()`. Alternatively, implement raw Annex-B pipe approach: FFmpeg outputs raw H.264 to `-f h264 pipe:3` and raw Opus to `-f data -c:a copy pipe:4`, read from both pipes asynchronously using asyncio's stream reader directly.

### BUG-02: Demuxer async generator does not release control during burst reads

**Location**: `discord_video_stream/media/demux.py`, `Demuxer.demux()` inner loop

**Root cause**: The `for packet in container.demux()` loop is synchronous. Even though the function is an async generator, the loop body never yields control to the event loop until a `yield` statement is reached. If PyAV produces many packets in a burst (e.g., at the start of a file), the loop will consume all of them synchronously before yielding the first frame.

**Impact**: Burst reads cause temporary event loop starvation, compounding with BUG-01.

**Suggested fix**: `asyncio.sleep(0)` is called every 8 frames, but this doesn't help during the initial burst before the first yield. Consider running the entire demux loop in a thread with a queue-based bridge.

### BUG-03: FFmpeg stderr not monitored during operation

**Location**: `discord_video_stream/media/ffmpeg.py`, `FFmpegProcess`

**Root cause**: The process is started with `stderr=asyncio.subprocess.PIPE`, but during operation the only method is `read_stderr()` which blocks until the process ends. If FFmpeg produces enough stderr output (e.g., verbose logging), the pipe buffer fills up, FFmpeg blocks on writing to stderr, which blocks stdout writes, which deadlocks the entire pipeline.

**Impact**: Pipeline deadlock under certain conditions (long-running streams with verbose FFmpeg output).

**Suggested fix**: The current code has a background `_drain_stderr()` task, which is correct. Verify this task is actually running and not being cancelled prematurely. The `-loglevel warning` flag (not `verbose`) should be used in production.

**Status**: Partially fixed (drain task exists), but verify it works under load.

---

## High Severity Bugs

### BUG-04: Secret key timing race between start_go_live() and play()

**Location**: `discord_video_stream/streamer.py`, `start_go_live()` and `play()`

**Root cause**: `start_go_live()` returns before SELECT_PROTOCOL_ACK (containing the secret key) arrives from the stream server. The secret key is available by the time `play()` is called only if there is a delay between the two calls.

**Impact**: If `play()` is called immediately after `start_go_live()`, `VideoSender.start()` may not find the secret key and will raise `RuntimeError`.

**Suggested fix**: Add a short wait loop in `VideoSender.start()` that waits for `stream_conn.secret_key` to become available (e.g., up to 2 seconds with 100ms polling). Or add a `_wait_for_session_description()` helper that `play()` calls before starting the pipeline.

### BUG-05: Fire-and-forget send tasks swallow errors

**Location**: `discord_video_stream/stream_connection.py`, `_send_json()` and `_send_binary()`; `discord_video_stream/streamer.py`, `_send_gateway()`

**Root cause**: All three methods use `asyncio.ensure_future()` with a done callback that logs the error. The calling code has no way to know if the send failed. For critical operations (IDENTIFY, SELECT_PROTOCOL, MLS_COMMIT_WELCOME), a silent failure means the handshake hangs indefinitely.

**Impact**: Silent failures in protocol handshake. The `connect()` method will timeout waiting for READY if IDENTIFY silently fails.

**Suggested fix**: For critical operations (IDENTIFY, SELECT_PROTOCOL, MLS ops), use `_send_json_await()` / `_send_binary_await()` which await the send directly. For non-critical operations (SPEAKING, heartbeats), fire-and-forget with logging is acceptable.

**Status**: Partially fixed -- `_send_json_await` / `_send_binary_await` exist and are used for IDENTIFY and SELECT_PROTOCOL. Verify all critical paths use await.

### BUG-06: Stream connection reconnection logic is incomplete

**Location**: `discord_video_stream/stream_connection.py`, `_receive_loop()` and `_attempt_reconnect()`

**Root cause**: The reconnection logic exists (`_attempt_reconnect()` with exponential backoff) but may not handle all edge cases:
- If the WebSocket URL changes (new endpoint from STREAM_SERVER_UPDATE), the reconnect URL is stale
- The DAVE session state may be inconsistent after reconnection
- The `_ready_params` may need to be refreshed

**Impact**: Network hiccups or Discord server restarts may kill the stream permanently despite the reconnection attempt.

**Suggested fix**: After successful reconnect, verify DAVE session state. If reconnect fails, propagate the failure to the VideoStreamer so it can attempt a full restart.

---

## Medium Severity Bugs

### BUG-07: Duplicate sequence/timestamp state in VideoSender

**Location**: `discord_video_stream/voice_send.py`, `VideoSender`

**Root cause**: `VideoSender` has its own `_sequence`, `_timestamp`, and `_nonce_counter` fields, but the `H264Packetizer` also maintains its own `_sequence` and `_timestamp`. The `VideoSender.send_frame()` calls `self._packetizer.set_timestamp()` to sync the timestamp, but the sequence numbers are maintained separately. This means `VideoSender._sequence` and `H264Packetizer._sequence` may diverge.

**Impact**: Cosmetic issue, no runtime impact because the packetizer's sequence is the one actually used in the RTP headers. The `VideoSender._sequence` is never read by any external code.

**Suggested fix**: Remove the duplicate `_sequence` and `_timestamp` from `VideoSender`. Use the packetizer's state as the source of truth.

### BUG-08: Unhandled opcode 15 (MEDIA_SINK_WANTS) in stream WS

**Location**: `discord_video_stream/stream_connection.py`, `_handle_json_message()`

**Root cause**: The stream voice WebSocket receives opcode 15 (pixel counts / MEDIA_SINK_WANTS) which is not handled. The current code logs it as "Unhandled stream WS op=15".

**Impact**: None functionally. The debug log noise is undesirable.

**Suggested fix**: Add a no-op handler for opcode 15 to suppress the log message.

### BUG-09: FramePacer busy-wait loop when stream is ahead

**Location**: `discord_video_stream/media/pacer.py`, `FramePacer.pace()`, the "ahead" branch

**Root cause**: When the video stream is ahead of audio, the pacer waits in a loop checking `self._sync_partner._pts`. Although it uses `asyncio.Event` notification (not polling), the event is only set when the partner calls `update_pts()`. If the partner is slow to update, the video pacer waits.

**Impact**: Minor latency (up to frametime_ms) when the partner catches up. Not a correctness issue.

**Suggested fix**: Already fixed in current code (uses `asyncio.Event` notification). Verify the event is being signaled correctly.

### BUG-10: try/except ImportError pattern breaks package isolation

**Location**: All `discord_video_stream/*.py` files

**Root cause**: Every module has `try: from .X import Y / except ImportError: from X import Y` fallback imports. This is because tests use `sys.path.insert()` rather than proper package imports.

**Impact**: Code quality issue. The fallback imports break package isolation and make the code harder to reason about. If a module is imported both ways, it may be loaded twice with different state.

**Suggested fix**: Use proper package imports everywhere. Run tests with `python -m pytest` from the package root, which handles package resolution correctly.

---

## Low Severity Issues

### BUG-11: _connected_users not populated initially

**Location**: `discord_video_stream/stream_connection.py`, `_connected_users`

**Root cause**: The `_connected_users` set is populated via CLIENTS_CONNECT events. But the initial MLS proposals may reference users who connected before us. The `process_proposals` call passes `list(self._connected_users)` as `expected_user_ids`, which may be empty initially.

**Impact**: Could cause DAVE key exchange to fail if the server expects specific user IDs. Low severity because `expected_user_ids` may be optional in the davey API.

### BUG-12: AudioSender sequence counter wraps at wrong boundary

**Location**: `discord_video_stream/voice_send.py`, `AudioSender._next_seq()`

**Root cause**: The sequence wraps at `& 0xFFFF` (65535), which is correct for 16-bit sequence numbers. No bug here, just noting for completeness.

### BUG-13: Stream preview not implemented

**Location**: `discord_video_stream/streamer.py`, `set_stream_preview()`

**Root cause**: The method exists but only logs "REST call not yet implemented". The Node.js reference implements this via `guild.members.me.voice.postPreview(data)`.

**Impact**: No stream preview image shown to viewers before they join.

**Suggested fix**: Implement via Discord REST API: `PUT /guilds/{guild.id}/voice-states/@me/preview` with base64-encoded JPEG body.

### BUG-14: RTCP Sender Reports not tested under real conditions

**Location**: `discord_video_stream/voice_send.py`, `VideoSender.send_rtcp_sender_report()`

**Root cause**: The RTCP SR construction and periodic sending are implemented, but the NTP timestamp computation has not been validated against a real Discord client receiver.

**Impact**: A/V sync on the receiver side may be affected if the NTP timestamp format is incorrect.

**Suggested fix**: Test with a real Discord client viewing the stream and verify A/V sync over 30+ seconds.

---

## Architectural Issues

### ARCH-01: File-for-file port strategy mismatch

The implementation used a file-for-file port strategy from Node.js to Python. This worked for logic-heavy modules (SPS VUI rewriter, protocol types, frame pacing) but failed for I/O-heavy modules (demuxer, WebSocket management) because Node.js and Python have fundamentally different concurrency models.

Node.js is single-threaded with non-blocking I/O streams. Python's asyncio is also single-threaded, but the libraries used (PyAV, pynacl) are synchronous. The port needed to bridge these two models.

**Lesson**: For future ports, identify what the source platform provides that the target does not, design the concurrency model first, then implement file by file.

### ARCH-02: Validation was code-level, not runtime-level

The validation pass verified that the LOGIC matches the reference, not that the BEHAVIOR matches. Issues like the demuxer blocking the event loop, audio not being sent, and packets going to the wrong endpoint required runtime testing to detect.

**Lesson**: Validation should include both code inspection and runtime profiling/testing.

### ARCH-03: Gateway event handling is fragile

The gateway event hooking chains onto `on_socket_raw_receive` by capturing the previous handler in a closure. This creates a reference cycle and may break if another extension also chains.

**Suggested improvement**: Use `client.add_listener()` for `on_socket_raw_receive`. This is the official extension point and works with any number of concurrent listeners.

---

## Priority Order

```
BUG-01 (event loop blocking) -----> BUG-02 (burst reads) -----> BUG-04 (key timing race)
BUG-03 (stderr deadlock) -----> BUG-05 (silent send failures)
BUG-07 (duplicate state) -----> BUG-08 (opcode 15) -----> BUG-10 (import pattern)
BUG-06 (reconnection) -----> BUG-11 (connected users) -----> BUG-13 (stream preview)
```

BUG-01 and BUG-02 are the highest priority because they affect the fundamental reliability of the media pipeline. BUG-04 is high priority because it causes a hard failure. The rest can be addressed incrementally.
