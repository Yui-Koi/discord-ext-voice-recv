# What Went Wrong: Root Cause Analysis

## Date: 2026-04-23

---

## The Short Version

The previous agent treated this as a mechanical file-for-file port from Node.js to Python. Each TypeScript file got a corresponding Python file with equivalent logic. The problem is that Node.js and Python have fundamentally different concurrency models, different library ecosystems, and different idioms for handling I/O. The port faithfully reproduced the LOGIC but missed the EXECUTION MODEL. The result is code that looks correct on paper but has runtime behavior that diverges from the reference in critical ways.

---

## The Full Story

### What the Previous Agent Got Right

The research was excellent. CONTEXT.md is 28,000 bytes of precise, well-sourced protocol documentation. OBSERVATIONS.md captures every relevant detail from the Node.js reference. The FINAL_PLAN.md correctly identifies that discord.py handles 60% of the protocol layer and that we only need to build the send side. The architectural decision to use UDP instead of WebRTC, davey instead of dave.py, and discord.py's VoiceClient as the base are all correct.

Phases 1 through 4 are genuinely solid. The RTP serialization, transport encryption, H.264 packetization, FFmpeg command construction, NUT demuxing, frame pacing, protocol types, and SPS VUI rewriter all match the reference implementation. The 106 tests for these phases are well-designed and pass. No issues there.

### Where It Went Wrong: Phase 5

Phase 5 added three files: `stream_connection.py` (620 lines), `voice_send.py` (310 lines), and `streamer.py` (470 lines). These are the integration files that wire everything together. The problem is not in any single file but in the gaps between them and in the assumptions carried over from the Node.js reference.

### Mistake 1: The Concurrency Model Mismatch

The Node.js reference uses WebRTC (via node-datachannel) for media transport. This is important because WebRTC provides:

1. **Built-in RTP packetization** -- `H264RtpPacketizer` from node-datachannel handles NALU splitting, FU-A, STAP-A, marker bits, sequence numbers, timestamps
2. **Built-in pacing** -- `PacingHandler(25_000_000, 1)` paces individual RTP packets at 25 Mbps with burst size 1
3. **Built-in RTCP** -- `RtcpSrReporter` generates Sender Reports, `RtcpNackResponder` handles retransmission
4. **Built-in audio sending** -- `_audioTrack.sendMessageBinary(frame)` packetizes, encrypts, and sends Opus audio through the same WebRTC connection

The Python port uses raw UDP instead of WebRTC. This is the correct decision (WebRTC libraries in Python are immature), but it means ALL of the above must be implemented manually. The previous agent implemented items 1 and parts of 3, but items 2 and 4 are missing or incomplete.

**The pacing gap:** The Node.js reference has TWO levels of pacing:
- Application-level: `BaseMediaStream._write()` sleeps to maintain correct frame timing (this was ported as `FramePacer`)
- Packet-level: `PacingHandler(25_000_000, 1)` smooths out individual RTP packet delivery to prevent burst sending

The Python port only has application-level pacing. When a keyframe generates 50+ RTP packets, they are all sent in a tight burst with no inter-packet delay. This can overwhelm the UDP send buffer or Discord's receive buffer.

**The audio gap:** In the Node.js reference, audio is sent through the WebRTC audio track:
```typescript
// WebRtcWrapper.ts
sendAudioFrame(frame: Buffer, frametime: number) {
    if (this.mediaConnection.daveReady)
        frame = this.mediaConnection.daveSession!.encryptOpus(frame);
    this._audioTrack?.sendMessageBinary(frame);
}
```

The Python port's `_send_loop` in `streamer.py` paces audio frames but never sends them:
```python
elif frame.frame_type == FrameType.AUDIO:
    # Audio is handled by discord.py's existing send path
    await self._audio_pacer.pace(frame.pts_ms, frame.frametime_ms)
    self._video_pacer.update_pts(frame.pts_ms)
```

The comment is wrong. discord.py's existing send path sends audio through the MAIN voice connection (voice server A), not through the STREAM connection (voice server B). For Go Live, audio must go to the stream server with the stream's SSRCs and secret key. Viewers would see video but hear nothing.

### Mistake 2: The Blocking Demuxer

The Node.js demuxer uses Node's stream API:
```typescript
const vPipe = new PassThrough({ objectMode: true, writableHighWaterMark: 128 });
const aPipe = new PassThrough({ objectMode: true, writableHighWaterMark: 128 });
```

These are non-blocking, backpressure-aware streams. The demuxer reads packets from libav and writes them to the pipes. Downstream consumers read from the pipes. If a consumer is slow, the pipe buffers up to `highWaterMark` packets, then the write blocks (backpressure).

The Python port uses PyAV's synchronous API inside an async generator:
```python
async def demux(self, pipe) -> AsyncIterator[MediaFrame]:
    sync_pipe = self._wrap_pipe(pipe)  # os.dup + fcntl to set blocking
    container = av.open(sync_pipe, format='nut', ...)
    for packet in container.demux():  # synchronous loop
        yield self._process_packet(packet)  # yields to async caller
```

The `for packet in container.demux()` loop is synchronous. Each call to `container.demux()` may block waiting for data from FFmpeg. While it blocks, the entire asyncio event loop is frozen. No heartbeats, no pacing, no other coroutines run.

The `_wrap_pipe()` method makes this worse by duplicating the file descriptor and setting it to blocking mode. This was necessary because PyAV requires a synchronous file object, but it means the read operations block the thread.

The CONTEXT.md document flagged this as an open research area:
> "PyAV NUT pipe latency not benchmarked. Fallback: raw Annex-B via `-f h264 pipe:3`"

The implementation went ahead with PyAV without benchmarking, and without implementing the fallback.

### Mistake 3: The Endpoint Confusion

The Node.js reference uses WebRTC for both the main voice connection and the stream connection. Each has its own `PeerConnection` with its own UDP transport:
```typescript
// BaseMediaConnection.ts
this._webRtcWrapper = new WebRtcConnWrapper(this);
// WebRtcWrapper.ts
this._webRtcConn = new PeerConnection("", { iceServers: [...] });
```

The Python port reuses discord.py's single UDP socket for both connections. This is correct (UDP is connectionless, same socket can send to different destinations), but the implementation confused which destination to send to.

The `send_udp` callback in `streamer.py` originally sent to the main voice connection's endpoint. Commit `68b6266` fixed this by reading the stream server's endpoint from `ready_params`. But the fix was applied only to `streamer.py`'s `send_udp` callback. The compat module's `send_video_packet()` still sends to the wrong endpoint.

### Mistake 4: The Validation Illusion

The EXECUTIVE_PLAN_V2 contains a section called "Validation Pass: Phase 1-4 Code Against dpy-self + Reference Repos" with green checkmarks for every component. This validation was code-level inspection, not runtime testing. It verified that the LOGIC matches the reference, not that the BEHAVIOR matches.

The validation correctly identified that:
- RTP headers are formatted correctly
- Transport encryption mirrors the decrypt side
- H.264 packetization follows RFC 6184
- FFmpeg flags match the reference
- SPS VUI rewriter produces correct output

But it did not identify that:
- The demuxer blocks the event loop (requires runtime profiling to detect)
- Audio is not being sent (requires end-to-end testing to detect)
- Packets are sent to the wrong endpoint (requires network-level inspection to detect)
- The pacing handler is missing (requires comparison of packet timing to detect)

The green checkmarks created a false sense of completeness. The EXECUTIVE_PLAN_V3 then focused narrowly on the endpoint bug (error 2012) as THE critical issue, missing the deeper architectural problems.

### Mistake 5: The Dead Code as Symptom

The `split_nalu()` function contains an abandoned first implementation attempt. The function tries to handle 4-byte vs 3-byte start codes, gets confused by the edge case where a 3-byte code is preceded by a 0x00 byte (making it a 4-byte code), writes a comment "Let me redo this more carefully", executes `break`, and falls through to `_split_nalu_impl()`.

This is a symptom of the broader pattern: the implementation was done quickly, without careful planning of invariants. The start code detection logic has a subtle edge case (is it a 3-byte code at position N, or a 4-byte code at position N-1?) that requires careful thought. The first attempt got it wrong, was abandoned in-place, and a second implementation was written. But the dead code was never cleaned up.

This pattern repeats at the architectural level. The demuxer's `_wrap_pipe()` is a workaround for PyAV's synchronous API. The `try/except ImportError` pattern in every file is a workaround for running tests with `sys.path.insert()` instead of proper package imports. The fire-and-forget `asyncio.ensure_future()` pattern is a workaround for not designing proper async error handling.

Each workaround is locally reasonable but collectively they indicate code that was written under time pressure without stepping back to reconsider the approach.

---

## The Core Architectural Mistake

The fundamental error was choosing a **file-for-file port** strategy when the source and target have different execution models.

Node.js is single-threaded with non-blocking I/O. Its stream API, WebRTC libraries, and event loop are designed for this model. Python's asyncio is also single-threaded with non-blocking I/O, but the libraries used (PyAV, pynacl) are synchronous. The port needed to bridge these two models, and the bridge (the `_wrap_pipe` hack, the synchronous `for` loop in an async generator) is where the problems live.

The correct approach would have been:

1. **Identify what Node.js provides that Python does not:** WebRTC packetization, pacing, RTCP, audio sending
2. **Decide how to replicate each piece:** Manual implementation, different library, or different architecture
3. **Design the concurrency model first:** Thread-based demuxer? Raw pipes? asyncio subprocess with multiple fds?
4. **Then implement file by file**

Instead, the approach was:
1. Map each TypeScript file to a Python file
2. Port the logic line by line
3. Work around Python's differences with hacks
4. Move on to the next file

This produced code that is logically correct but architecturally flawed.

---

## What Needs to Happen

The refactor is not about fixing individual bugs. It is about rethinking the parts of the architecture that do not translate cleanly from Node.js to Python. Specifically:

1. **Replace PyAV with raw pipes or thread-wrapped PyAV.** The demuxer must not block the event loop.
2. **Add audio sending to the stream path.** Audio must go to the stream server, not the main voice server.
3. **Add packet-level pacing.** Either implement a token bucket pacer or batch small sends.
4. **Clean up the dead code and workarounds.** The `split_nalu` dead code, the `try/except ImportError` pattern, the fire-and-forget sends.
5. **Fix the remaining endpoint bug in the compat module.**

These are not separate issues. They are all symptoms of the same root cause: the port strategy did not account for the differences between the Node.js and Python execution models.
