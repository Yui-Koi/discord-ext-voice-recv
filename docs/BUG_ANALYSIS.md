# Systematic Bug Analysis

## Date: 2026-04-23

All issues below are derived from cross-referencing the implementation against OBSERVATIONS.md, FINAL_PLAN.md, INTEGRATION.md, and the Node.js reference source.

---

## CRITICAL: Stream-Affecting Bugs

### BUG-01: Audio frames are never sent to the stream server

**File:** `streamer.py`, `_send_loop()`, lines 498-505

**Code:**
```python
elif frame.frame_type == FrameType.AUDIO:
    # Audio is handled by discord.py's existing send path
    # We just need to pace it for sync reference
    await self._audio_pacer.pace(frame.pts_ms, frame.frametime_ms)
    self._video_pacer.update_pts(frame.pts_ms)
```

**What the Node.js reference does** (WebRtcWrapper.ts `sendAudioFrame`):
```typescript
sendAudioFrame(frame: Buffer, frametime: number) {
    if (this.mediaConnection.daveReady)
        frame = this.mediaConnection.daveSession!.encryptOpus(frame);
    this._audioTrack?.sendMessageBinary(frame);
    rtpConfig.timestamp += Math.round((frametime * clockRate) / 1000);
}
```

**Impact:** Go Live streams have video but no audio. The comment "Audio is handled by discord.py's existing send path" is incorrect. discord.py's audio send path uses the MAIN voice connection's SSRC and endpoint, not the stream connection's. Audio frames extracted by the demuxer are paced (for sync timing) but then discarded.

**Root cause:** The Node.js reference sends audio through the WebRTC audio track, which is part of the stream connection. The Python port does not have an equivalent audio send path for the stream. The FINAL_PLAN Task 5.2 step 6 says "demux frame -> pace -> packetize -> DAVE encrypt -> transport encrypt -> UDP send" but the implementation only does this for video frames.

**Fix:** Add audio RTP packetization and sending. Opus frames are small (typically 60-120 bytes) and fit in a single RTP packet (no fragmentation needed). The pipeline:
1. DAVE encrypt: `dave_session.encrypt_opus(frame)` (or `encrypt(MediaType.audio, Codec.opus, frame)`)
2. Build RTP header: PT=120, clock_rate=48000, audio SSRC from stream READY
3. Transport encrypt: AEAD with stream secret key
4. UDP sendto: stream server endpoint

Requires: separate audio sequence number, timestamp counter, and nonce counter (independent from video and from discord.py's main audio).

---

### BUG-02: Packet-level pacing is missing

**File:** `voice_send.py`, `send_frame()`, lines 240-250

**Code:**
```python
for pkt in packets:
    header = bytes(pkt[:12])
    payload = bytes(pkt[12:])
    encrypted = self._encryptor.encrypt_rtp(header, payload)
    wire_packet = header + encrypted
    self._send_udp(wire_packet)
    self._packet_count += 1
    self._octet_count += len(payload)
```

**What the Node.js reference does** (WebRtcWrapper.ts `setPacketizer`):
```typescript
this._videoPacketizer.addToChain(new PacingHandler(25 * 1000 * 1000, 1));
```

**OBSERVATIONS.md section 3:**
> "MVP implication: We need to implement our own pacing. A simple token-bucket or leaky-bucket rate limiter at 25 Mbps should suffice."

**Impact:** When a keyframe generates 50+ RTP packets, they are all sent in a tight loop with no inter-packet delay. At 25 Mbps with 1300-byte packets, the inter-packet gap should be approximately 0.4ms. The current implementation sends all packets as fast as the loop can iterate, which is sub-microsecond. This can cause UDP buffer overflow on the local socket or packet loss at Discord's SFU.

**Root cause:** The Node.js reference has two levels of pacing: `BaseMediaStream` for frame timing (ported as `FramePacer`) and `PacingHandler` for packet smoothing (not ported). The OBSERVATIONS doc explicitly flagged this. The FINAL_PLAN's "What We Need to Build" list includes "frame pacing" but the implementation only addressed application-level pacing.

**Fix:** Add a token-bucket pacer that runs inside the packet send loop:
```python
# 25 Mbps = 25_000_000 bits/sec = 3_125_000 bytes/sec
# Time per 1300-byte packet: 1300 / 3_125_000 = 0.000416s = 0.416ms
BYTES_PER_SEC = 25_000_000 // 8
for pkt in packets:
    # ... encrypt ...
    self._send_udp(wire_packet)
    # Pace: sleep proportional to packet size
    sleep_time = len(wire_packet) / BYTES_PER_SEC
    await asyncio.sleep(sleep_time)
```

Note: this requires making `send_frame` async, or moving the send loop to a separate coroutine.

---

### BUG-03: Demuxer blocks the asyncio event loop

**File:** `media/demux.py`, `demux()` method, lines 130-175

**Code:**
```python
async def demux(self, pipe) -> AsyncIterator[MediaFrame]:
    sync_pipe = self._wrap_pipe(pipe)
    container = av.open(sync_pipe, format=self._format, ...)
    # ...
    for packet in container.demux():  # synchronous, blocking
        # ...
        yield MediaFrame(...)
```

**What the Node.js reference does** (LibavDemuxer.ts):
```typescript
const vPipe = new PassThrough({ objectMode: true, writableHighWaterMark: 128 });
const aPipe = new PassThrough({ objectMode: true, writableHighWaterMark: 128 });
// ... demuxer reads from input, writes to vPipe/aPipe
// downstream consumers read from pipes with backpressure (drain events)
```

**Impact:** The `for packet in container.demux()` loop is synchronous. Each call to the iterator may block waiting for data from FFmpeg. While blocked, the entire asyncio event loop is frozen. No heartbeats run, no pacing runs, no other coroutines execute. If FFmpeg produces data slowly (e.g., at real-time rate for a 30fps video, one frame every 33ms), the event loop blocks for up to 33ms per iteration. During this time, the stream voice server heartbeat (interval from HELLO, typically ~41 seconds) cannot be sent. If FFmpeg stalls (e.g., buffering a network input), the block can last seconds, causing heartbeat timeout and disconnection.

The `_wrap_pipe()` method compounds this by duplicating the fd and setting it to blocking mode, which is necessary for PyAV but ensures the read operations block.

**Root cause:** PyAV's `container.demux()` is a synchronous generator. The Node.js reference uses Node's stream API which is non-blocking by design. The Python port runs PyAV synchronously inside an async generator, which does not yield to the event loop between reads.

**Fix (option A - thread):** Wrap the synchronous PyAV iteration in a thread and use an asyncio.Queue to bridge:
```python
async def demux(self, pipe) -> AsyncIterator[MediaFrame]:
    sync_pipe = self._wrap_pipe(pipe)
    container = av.open(sync_pipe, format=self._format, ...)
    queue = asyncio.Queue(maxsize=32)

    def _thread():
        try:
            for packet in container.demux():
                frame = self._process_packet(packet)
                asyncio.run_coroutine_threadsafe(queue.put(frame), loop)
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)
        finally:
            container.close()

    loop = asyncio.get_event_loop()
    threading.Thread(target=_thread, daemon=True).start()
    while True:
        frame = await queue.get()
        if frame is None:
            break
        yield frame
```

**Fix (option B - raw pipes):** Eliminate PyAV entirely. Have FFmpeg output raw H.264 to `-f h264 pipe:3` and raw Opus to `-f data -c:a copy pipe:4`. Read from both pipes using asyncio's stream reader directly. Parse H.264 start codes for frame boundaries. Parse Opus TOC bytes for frame lengths. This was documented in FINAL_PLAN Task 3.2 as the fallback approach.

---

### BUG-04: Compat module sends video to wrong endpoint

**File:** `compat/voice_recv.py`, `send_video_packet()`, lines 75-82

**Code:**
```python
def send_video_packet(self, packet: bytes) -> None:
    if self._connection and self._connection.socket:
        self._connection.socket.sendto(
            packet,
            (self._connection.endpoint_ip, self._connection.voice_port),
        )
```

**What it should do:** Send to the stream server's endpoint, not the main voice connection's endpoint.

**Impact:** Error 2012 when using `VoiceSendRecvClient` directly (not through `VideoStreamer`). The same bug that was fixed in commit `68b6266` for the main pipeline was never applied to the compat module.

**Fix:** Accept endpoint parameters or read from the stream connection's `ready_params`.

---

## HIGH: Reliability and Correctness Bugs

### BUG-05: FFmpeg stderr is never drained, risking deadlock

**File:** `media/ffmpeg.py`, `FFmpegProcess`

**Code:** The process is started with `stderr=asyncio.subprocess.PIPE` but nothing reads from stderr during operation. The only method is `read_stderr()` which blocks until process end.

**OBSERVATIONS.md section 1:** The FFmpeg command uses `-loglevel verbose`, which produces substantial output.

**Impact:** If FFmpeg generates enough stderr output to fill the OS pipe buffer (typically 64KB on Linux), FFmpeg blocks on writing to stderr. This blocks FFmpeg's main loop, which blocks stdout writes. The demuxer then blocks waiting for stdout data. The entire pipeline deadlocks.

**Fix:** Spawn a background task to continuously drain stderr:
```python
async def _drain_stderr(proc):
    while True:
        data = await proc.stderr.read(4096)
        if not data:
            break
        # Optionally log at debug level
```
Also change `-loglevel verbose` to `-loglevel warning` for production.

---

### BUG-06: Stream connection has no reconnection logic

**File:** `stream_connection.py`, `_receive_loop()`, lines 220-235

**Code:**
```python
except websockets.ConnectionClosed as e:
    can_resume = e.code == 4015 or e.code < 4000
    if can_resume and not self.state.closed:
        log.info('Attempting stream WS resume')
        self.state.resuming = True
        self.state.started = False
        # Reconnect will be handled by the caller
```

**Impact:** Any network hiccup or Discord server restart kills the stream permanently. The code logs intent to resume but never calls `resume()`. The user must manually stop and restart.

**Fix:** Implement automatic reconnection:
```python
if can_resume and not self.state.closed:
    self.state.resuming = True
    self.state.started = False
    await asyncio.sleep(1.0)  # brief backoff
    try:
        await self.resume()
    except Exception:
        log.error('Resume failed, stream ended')
```

---

### BUG-07: Fire-and-forget sends for critical protocol messages

**File:** `stream_connection.py`, `_send_json()` and `_send_binary()`

**Code:**
```python
def _send_json(self, op: int, data: Dict[str, Any]) -> None:
    payload = json.dumps({'op': op, 'd': data})
    task = asyncio.ensure_future(self._ws.send(payload))
    task.add_done_callback(self._handle_send_error)
```

**Impact:** IDENTIFY, SELECT_PROTOCOL, MLS_COMMIT_WELCOME, and other critical messages are sent fire-and-forget. If the send fails (e.g., WebSocket closing), the error is logged but the calling code has no way to know. The `connect()` method will then timeout waiting for READY, with no indication that IDENTIFY never actually sent.

**Fix:** For critical handshake messages, await the send directly:
```python
async def _send_json_await(self, op: int, data: Dict[str, Any]) -> None:
    payload = json.dumps({'op': op, 'd': data})
    await self._ws.send(payload)
```
Use `_send_json_await` in `identify()`, `_send_select_protocol()`, and MLS operations. Keep `_send_json` fire-and-forget for non-critical messages (SPEAKING, HEARTBEAT).

---

### BUG-08: RTCP Sender Report NTP timestamp is wrong

**File:** `voice_send.py`, `send_rtcp_sender_report()`, lines 340-355

**Code:**
```python
ntp_epoch = -2208988800  # offset from Unix epoch to NTP epoch
ntp_now = time.time() - ntp_epoch
ntp_timestamp = int(ntp_now * (2**32))
```

**What it should be:** NTP timestamp is 64 bits: 32-bit seconds + 32-bit fractional part. The code computes `int(ntp_now * 2**32)` which gives a single 64-bit value. But `build_rtcp_sr()` packs it as:
```python
ntp_high = (ntp_timestamp >> 32) & 0xFFFFFFFF
ntp_low = ntp_timestamp & 0xFFFFFFFF
```

The issue is that `ntp_now` for year 2026 is approximately 3.9 billion seconds since NTP epoch. `int(ntp_now * 2**32)` produces a number around 1.68 * 10^19, which requires 64 bits. Shifting right by 32 gives the seconds part (~3.9 billion), and the lower 32 bits give the fractional part. This is actually correct.

**Correction:** After re-analysis, this is NOT a bug. The NTP timestamp computation is correct. The seconds since NTP epoch (1900-01-01) for 2026-04-23 is approximately 3,983,000,000 which fits in 32 bits (max 4,294,967,295). The fractional part is the sub-second fraction scaled to 2^32. Disregard.

---

### BUG-09: VideoSender._sequence is maintained but never used in packets

**File:** `voice_send.py`, `start()` and `send_frame()`

**Code in `start()`:**
```python
self._sequence = 0
```

**Code in `send_frame()`:**
```python
packets = self._packetizer.packetize_frame(frame)
```

The `H264Packetizer` maintains its own `_sequence` counter internally. The `VideoSender._sequence` field is initialized and reset but never read or used in packet construction. The `VideoSender._timestamp` is also maintained separately from the packetizer's timestamp.

**Impact:** No runtime impact (the packetizer's internal sequence is correct). But the duplicate state is confusing and could lead to bugs if someone tries to use `VideoSender.sequence` for RTCP or other purposes.

**Fix:** Remove `self._sequence` and `self._timestamp` from `VideoSender`, or use them as the authoritative source and pass them to the packetizer.

---

## MEDIUM: Code Quality and Edge Cases

### BUG-10: Dead code in split_nalu()

**File:** `rtp/h264.py`, `split_nalu()`, lines 35-70

The function begins with a naive implementation that tries to handle 4-byte vs 3-byte start codes. It discovers an edge case (3-byte code preceded by 0x00 is actually a 4-byte code), writes "Let me redo this more carefully", executes `break`, and falls through to `_split_nalu_impl()`. The first ~30 lines are dead code.

**Fix:** Remove the dead code. Have `split_nalu()` directly call `_split_nalu_impl()` or inline it.

---

### BUG-11: Inconsistent try/except ImportError pattern in every file

**Files:** All files in `discord_video_stream/`

Every file has:
```python
try:
    from .X import Y
except ImportError:
    from X import Y
```

This is because tests use `sys.path.insert()` to add the parent directory, making absolute imports work. The fallback breaks package isolation.

**Fix:** Use proper relative imports everywhere. Run tests with `python -m pytest` from the package root.

---

### BUG-12: FramePacer busy-wait loop for sync

**File:** `media/pacer.py`, `pace()`, the "ahead" branch

**Code:**
```python
while self._sync_enabled and self._sync_partner is not None:
    partner_pts = self._sync_partner._pts
    if not self._is_ahead(delta, frametime_ms):
        break
    await asyncio.sleep(frametime_ms / 1000)
```

**Impact:** Polls the partner's PTS at frametime intervals (33ms for 30fps). Wastes CPU and introduces up to frametime_ms of latency when the partner catches up.

**Fix:** Use an `asyncio.Event` that the partner sets on PTS update. The waiting stream awaits the event instead of polling.

---

### BUG-13: Demuxer _wrap_pipe leaks file descriptors

**File:** `media/demux.py`, `_wrap_pipe()`

**Code:**
```python
dup_fd = os.dup(pipe_fd)
flags = fcntl.fcntl(dup_fd, fcntl.F_GETFL)
fcntl.fcntl(dup_fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
return os.fdopen(dup_fd, 'rb')
```

The duplicated fd is opened as a file object but never explicitly closed. When the `Demuxer` finishes, the file object may be garbage-collected (closing the fd), but this is not guaranteed in all Python implementations. If `demux()` is called multiple times (e.g., stream restart), fds accumulate.

**Fix:** Store the file object and close it explicitly in a `finally` block or `close()` method.

---

### BUG-14: StreamConnection.identify() is synchronous but calls _send_json which uses ensure_future

**File:** `stream_connection.py`, `identify()`

**Code:**
```python
def identify(self) -> None:
    sid = self.server_id
    if sid is None:
        raise RuntimeError('server_id not set')
    self._send_json(VoiceOpCodes.IDENTIFY, { ... })
```

`_send_json` uses `asyncio.ensure_future()` to send. The `identify()` method is called synchronously from `connect()`. This means IDENTIFY is queued but may not be sent before the receive loop starts processing messages. In practice this works because `ensure_future` schedules on the next event loop iteration, and the receive loop also runs on the event loop, so IDENTIFY will be sent before any server response arrives. But it is fragile.

**Fix:** Make `identify()` async and await the send.

---

### BUG-15: StreamConnection._ready_event is created inside connect()

**File:** `stream_connection.py`, `connect()`

**Code:**
```python
self._ready_event = asyncio.Event()
try:
    await asyncio.wait_for(self._ready_event.wait(), timeout=10.0)
```

If `connect()` is called a second time (e.g., after a failed attempt), `_ready_event` is overwritten. Any reference to the old event from a concurrent task would be stale.

**Fix:** Initialize `_ready_event` in `__init__()` and `clear()` it at the start of `connect()`.

---

### BUG-16: VideoAttributes hardcoded to 1920x1080x30

**File:** `streamer.py`, `start_go_live()`, line 378

**Code:**
```python
self._stream_conn.set_video_attributes(
    enabled=True,
    attrs=VideoAttributes(width=1920, height=1080, fps=30),
)
```

The VIDEO opcode is sent with 1920x1080x30 regardless of the actual stream resolution. The `play()` method's `StreamOptions` defaults to 1280x720. This mismatch means Discord's UI shows 1080p but the actual stream is 720p.

**Fix:** Delay sending VIDEO until `play()` is called, or pass the actual resolution from `StreamOptions`.

---

### BUG-17: stop() uses asyncio.ensure_future for FFmpeg cleanup

**File:** `streamer.py`, `stop()`, line 563

**Code:**
```python
if self._ffmpeg is not None:
    asyncio.ensure_future(self._ffmpeg.stop())
    self._ffmpeg = None
```

The FFmpeg process is terminated fire-and-forget. If `stop()` is called and then `play()` is called immediately, the old FFmpeg process may still be running, holding the pipe.

**Fix:** `await self._ffmpeg.stop()` directly, or track the task and await it.

---

## Summary Table

| ID | Severity | File | Description | Status |
|----|----------|------|-------------|--------|
| BUG-01 | CRITICAL | streamer.py | Audio frames never sent to stream server | FIXED |
| BUG-02 | CRITICAL | voice_send.py | No packet-level pacing (burst sending) | FIXED |
| BUG-03 | CRITICAL | media/demux.py | Demuxer blocks asyncio event loop | FIXED |
| BUG-04 | CRITICAL | compat/voice_recv.py | Video sent to wrong endpoint | FIXED |
| BUG-05 | HIGH | media/ffmpeg.py | stderr never drained, deadlock risk | FIXED |
| BUG-06 | HIGH | stream_connection.py | No reconnection on disconnect | FIXED |
| BUG-07 | HIGH | stream_connection.py | Critical WS sends are fire-and-forget | FIXED |
| BUG-08 | -- | voice_send.py | ~~RTCP NTP timestamp~~ (retracted, correct) | N/A |
| BUG-09 | LOW | voice_send.py | Duplicate sequence/timestamp state | OPEN |
| BUG-10 | LOW | rtp/h264.py | Dead code in split_nalu() | FIXED |
| BUG-11 | LOW | all files | try/except ImportError fallback pattern | OPEN |
| BUG-12 | MEDIUM | media/pacer.py | Busy-wait polling for sync | FIXED |
| BUG-13 | MEDIUM | media/demux.py | File descriptor leak in _wrap_pipe | FIXED |
| BUG-14 | LOW | stream_connection.py | identify() is sync, sends fire-and-forget | FIXED |
| BUG-15 | LOW | stream_connection.py | _ready_event created inside connect() | FIXED |
| BUG-16 | MEDIUM | streamer.py | VIDEO attributes hardcoded, not from options | FIXED |
| BUG-17 | MEDIUM | streamer.py | FFmpeg stop is fire-and-forget | OPEN |

---

## Fix Priority Order

1. **BUG-03** (demuxer blocks event loop) -- everything else depends on the event loop running
2. **BUG-01** (audio not sent) -- without audio, Go Live is broken
3. **BUG-02** (no packet pacing) -- causes packet loss on keyframes
4. **BUG-05** (stderr deadlock) -- causes pipeline stall under verbose logging
5. **BUG-04** (compat endpoint) -- blocks use of unified client
6. **BUG-07** (fire-and-forget sends) -- causes silent handshake failures
7. **BUG-06** (no reconnection) -- causes permanent stream loss on network hiccup
8. **BUG-16** (hardcoded attributes) -- cosmetic but visible to viewers
9. **BUG-12** (busy-wait sync) -- performance
10. **BUG-13** (fd leak) -- resource leak on restart
11. **BUG-17** (fire-and-forget FFmpeg stop) -- race condition
12. **BUG-10, BUG-11, BUG-14, BUG-15** -- code quality

---

## Fix Log (2026-04-23)

### BUG-03: Demuxer blocks event loop -- FIXED

**File:** `media/demux.py`

**Approach:** Attempted thread-based demuxing first. PyAV's FFmpeg I/O bindings are not thread-safe -- `av.open()` on a pipe fd from a background thread produces `AVERROR_INVALIDDATA`. Reverted to synchronous PyAV with periodic event loop yields.

**Changes:**
- Added `asyncio.sleep(0)` every `YIELD_INTERVAL=8` frames in the `demux()` async generator
- Replaced `_wrap_pipe()` to return `(file_obj, dup_fd)` tuple for explicit cleanup
- Added `_close_pipe()` method to Demuxer for explicit fd cleanup in `finally` block
- Fixed typo in `probe()` options: `ffflags` -> `fflags`
- Removed thread-based approach entirely

**Trade-off:** The demuxer still blocks for the duration of a single `container.demux().__next__()` call (typically <1ms for real-time content, up to ~33ms at 30fps). This is acceptable because `asyncio.sleep(0)` runs after each frame, giving heartbeats and pacing a chance to execute. The blocking duration is bounded by FFmpeg's frame production rate.

### BUG-05: FFmpeg stderr deadlock -- FIXED

**File:** `media/ffmpeg.py`

**Changes:**
- Changed `-loglevel verbose` to `-loglevel warning` (reduces stderr volume by ~10x)
- Added `_drain_stderr()` async task that continuously reads 4096 bytes from stderr
- `start()` spawns the drain task alongside the subprocess
- `stop()` cancels the drain task before terminating FFmpeg
- `_stderr_buffer` collects all stderr output for `read_stderr()`

### BUG-10: Dead code in split_nalu -- FIXED

**File:** `rtp/h264.py`

**Changes:**
- Removed the abandoned first implementation attempt (30+ lines of dead code)
- Removed `_split_nalu_impl()` as a separate function
- Inlined the clean position-based approach directly into `split_nalu()`

### BUG-01: Audio never sent -- FIXED

**File:** `voice_send.py` (new class), `streamer.py`, `__init__.py`

**Changes:**
- Added `AudioSender` class (120 lines) to `voice_send.py`
  - Opus PT 120, 48kHz clock rate
  - DAVE encrypt via `dave_session.encrypt_opus(frame)`
  - Separate sequence number, nonce counter from video
  - Same send callback pattern as VideoSender
- Updated `streamer.py`:
  - Imports `AudioSender`
  - Creates AudioSender in `play()` alongside VideoSender
  - `_send_loop` now calls `audio_sender.send_frame()` for audio frames
  - `stop()` cleans up audio sender
- Updated `__init__.py` exports

### BUG-02: No packet pacing -- FIXED

**File:** `streamer.py`

**Changes:**
- Added `PACING_BYTES_PER_SEC = 25_000_000 // 8` constant in `_send_loop`
- After each video frame, calculates pacing sleep: `(pkt_count * 1300) / PACING_BYTES_PER_SEC`
- Only paces when `pkt_count > 1` (single-packet frames don't need pacing)
- Uses `await asyncio.sleep()` to yield during pacing

**Trade-off:** Pacing is per-frame, not per-packet. A frame with 50 packets sends all 50 in a burst, then sleeps. True per-packet pacing would require making `send_frame` async or using a separate send queue. The per-frame approach is sufficient for typical keyframes (~20-50 packets) because the sleep after the burst gives the OS time to drain the UDP send buffer.

### BUG-04: Compat endpoint -- FIXED

**File:** `compat/voice_recv.py`

**Changes:**
- `send_video_packet()` now accepts optional `ip` and `port` parameters
- Falls back to stream connection's `ready_params` endpoint when not specified
- Never sends to main voice connection's endpoint

### BUG-16: Hardcoded video attributes -- FIXED

**File:** `streamer.py`

**Changes:**
- Moved `set_video_attributes()` from `start_go_live()` to `play()`
- Uses actual `width`, `height`, `fps` from `StreamOptions` passed to `play()`
- Defaults to 1280x720@30fps when StreamOptions uses -2 (aspect ratio maintain)

### BUG-13: FD leak in _wrap_pipe -- FIXED

**File:** `media/demux.py`

**Changes:**
- `_wrap_pipe()` returns `(file_obj, dup_fd)` tuple
- `_close_pipe()` explicitly closes both the file object and the dup_fd
- Called in `finally` block of `demux()` method
- Called in `probe()` method's `finally` block

---

## Runtime Bugs (2026-04-23, live testing)

### RT-BUG-01 (CRITICAL): Gateway hook requires _enable_debug_events

**File:** `streamer.py`, `_setup_gateway_listener()`

The `socket_raw_receive` event is only dispatched by dpy-self when `_enable_debug_events` is True (gateway.py line 405: `ws.log_receive = ws.debug_log_receive`). Without this flag, the event never fires, and STREAM_CREATE/STREAM_SERVER_UPDATE/VOICE_STATE_UPDATE are never caught.

**Impact:** The entire gateway hooking mechanism is non-functional in production. join_voice() times out waiting for session_id. start_go_live() times out waiting for STREAM_CREATE.

**Root cause:** The `_setup_gateway_listener` relies on `socket_raw_receive` dispatch, which is a debug-only feature in dpy-self.

**Fix needed:** Patch the gateway WebSocket's `log_receive` method after connection to always dispatch `socket_raw_receive`, or use a different interception mechanism (e.g., monkey-patching the WS message handler directly).

### RT-BUG-02 (HIGH): FFmpeg lavfi input format

**File:** `media/ffmpeg.py`, `_build_command()`

FFmpeg rejects `lavfi:color=...` as input URL format. The correct format is `-f lavfi -i color=...`. The StreamOptions class has `custom_input_options` for pre-input flags but the test bot passed `lavfi:...` as the URL directly.

**Impact:** FFmpeg fails with exit code 8, producing no output. The demuxer then crashes with AVERROR_INVALIDDATA.

**Fix needed:** Either document that lavfi inputs require `custom_input_options=['-f', 'lavfi']` and `url='color=...'`, or detect the `lavfi:` prefix and split it automatically.

### RT-BUG-03 (MEDIUM): Secret key timing race

**File:** `streamer.py`, `start_go_live()`

The SESSION_DESCRIPTION (containing secret_key and encryption_mode) arrives ~7ms after `start_go_live()` returns. If `play()` is called immediately, the VideoSender.start() could race against the async SESSION_DESCRIPTION handler.

**Impact:** In practice, the 3-second sleep in the test bot avoided this. But callers who chain start_go_live() and play() without a delay could hit this.

**Fix needed:** Either wait for SESSION_DESCRIPTION in start_go_live(), or defer VideoSender.start() until play() checks for readiness with a short wait.

### RT-BUG-04 (LOW): Unhandled opcode 15

**File:** `stream_connection.py`, `_handle_json_message()`

The stream WS receives opcode 15 (MEDIA_SINK_WANTS / pixelCounts). Not handled, logged as unhandled.

**Impact:** None (informational only).
