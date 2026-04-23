# Investigation: Aspect-Oriented Programming for Development Workflow

## Executive Summary

AOP is a strong fit for this project's specific pain points. The codebase has cross-cutting concerns scattered across modules: protocol logging, encryption/decryption tracing, packet lifecycle instrumentation, timing measurements, and error propagation. AOP lets you instrument all of these from a single point without touching the core logic. Combined with IPC-based debugging and enhanced TDD, this forms the basis of a development workflow that accelerates bug fixing and provides deep introspection into the running bot.

**Verdict: Yes, AOP is a huge help here. Not as a framework to adopt wholesale, but as a targeted instrumentation layer.**

---

## 1. Why AOP Fits This Project

### 1.1 Cross-Cutting Concerns in the Codebase

The video stream subsystem has these concerns that cut across every module:

| Concern | Modules Affected | Current State |
|---|---|---|
| Protocol logging (opcodes, payloads) | stream_connection.py, streamer.py | Scattered log.debug() calls |
| Encryption/decryption tracing | rtp/crypto.py, voice_send.py | Manual logging in each method |
| Packet lifecycle (build -> encrypt -> send) | rtp/serialize.py, rtp/h264.py, rtp/crypto.py, voice_send.py | No end-to-end tracing |
| Timing measurements (frame pacing, send latency) | media/pacer.py, streamer.py | Inline time.monotonic() calls |
| Error propagation (WS sends, FFmpeg, DAVE) | stream_connection.py, streamer.py | Mixed fire-and-forget and await |
| State transitions (connection lifecycle) | stream_connection.py | Manual state tracking |
| SSRC/key validation | voice_send.py, stream_connection.py | Inline checks in start() |

With AOP, each of these becomes a single aspect that applies everywhere automatically.

### 1.2 What AOP Gives You That Decorators Alone Don't

Python decorators are limited AOP: they work on individual functions you explicitly decorate. True AOP (via `aspectlib.weave`) can:

- **Weave into existing code** without modifying source files: `aspectlib.weave(StreamConnection, aspects=log_aspect)`
- **Apply to entire classes/modules** with pointcut expressions: weave all methods matching a pattern
- **Capture and replay**: record all inputs/outputs of woven functions, replay them in tests
- **Monkey-patch third-party code**: instrument discord.py internals, PyAV calls, nacl operations

This is critical for debugging because you can instrument the running bot without restarting it.

---

## 2. Recommended Tool Stack

### 2.1 aspectlib (Core AOP)

```
pip install aspectlib
```

What it provides:
- `aspectlib.weave(target, aspects)` -- weave aspects into functions, classes, or modules
- `@aspectlib.aspect` -- define before/after/around advice
- `aspectlib.Test` -- record/mock for testing
- `aspectlib.Story` / `aspectlib.Replay` -- capture/replay framework

Example -- log every method call on StreamConnection:

```python
import aspectlib

@aspectlib.aspect
def log_calls(call):
    print(f">>> {call.func.__name__}({call.args}, {call.kwargs})")
    result = yield aspectlib.Proceed
    print(f"<<< {call.func.__name__} -> {result}")
    yield aspectlib.Return(result)

# Weave into existing class without modifying source
aspectlib.weave(StreamConnection, log_calls)
```

### 2.2 structlog (Structured Logging)

```
pip install structlog
```

What it provides:
- Structured key-value logging (not just strings)
- Context binding (bind ssrc, opcode, session_id to all subsequent logs)
- Processor chain (timestamp, level, caller info, JSON output)
- Integration with standard logging

Example:

```python
import structlog
log = structlog.get_logger()

log = log.bind(ssrc=2000, opcode=12)
log.info("video_frame_sent", packets=5, pts_ms=100.0, size_bytes=8500)
# Output: [info] video_frame_sent ssrc=2000 opcode=12 packets=5 pts_ms=100.0 size_bytes=8500
```

### 2.3 rich (Visual Debugging)

```
pip install rich
```

What it provides:
- Syntax-highlighted logging output
- Live tables for packet inspection
- Progress bars for frame pacing visualization
- Panel/tree layouts for connection state
- Traceback formatting

### 2.4 IPC Debug Channel

Use Unix domain sockets for runtime introspection:

```python
# debug_server.py
import asyncio
import json

class DebugServer:
    def __init__(self, streamer, path="/tmp/discord-debug.sock"):
        self._streamer = streamer
        self._path = path

    async def start(self):
        server = await asyncio.start_unix_server(self._handle, self._path)
        await server.serve_forever()

    async def handle(self, reader, writer):
        cmd = (await reader.readline()).decode().strip()
        if cmd == "status":
            writer.write(json.dumps({
                "streaming": self._streamer.is_streaming,
                "voice_connected": self._streamer._voice_client is not None,
            }).encode() + b"\n")
        elif cmd == "packets":
            # Return recent packet stats from instrumented senders
            ...
        elif cmd == "state":
            # Dump full connection state
            ...
        await writer.drain()
        writer.close()
```

Commands from terminal:
```bash
echo "status" | socat - UNIX-CONNECT:/tmp/discord-debug.sock
echo "packets" | socat - UNIX-CONNECT:/tmp/discord-debug.sock
echo "state" | socat - UNIX-CONNECT:/tmp/discord-debug.sock
```

---

## 3. AOP Application to Specific Subsystems

### 3.1 Protocol Logging Aspect

Replace scattered `log.debug()` calls with a single aspect that automatically logs all voice WebSocket messages:

```python
@aspectlib.aspect
def protocol_trace(call):
    """Trace all protocol method calls with opcode and payload."""
    op_name = call.func.__name__
    log.debug("protocol_call", method=op_name, args=call.args[:1])
    result = yield aspectlib.Proceed
    log.debug("protocol_return", method=op_name, result_type=type(result).__name__)
    yield aspectlib.Return(result)

# Apply to all methods in StreamConnection that handle opcodes
aspectlib.weave(StreamConnection, protocol_trace, methods=[
    'identify', 'resume', 'set_speaking', 'set_video_attributes',
    '_handle_json_message', '_handle_binary_message',
])
```

### 3.2 Packet Lifecycle Aspect

Trace a packet from construction to wire:

```python
@aspectlib.aspect
def packet_lifecycle(call):
    """Trace packet build -> encrypt -> send pipeline."""
    method = call.func.__name__
    if method == 'build_rtp_header':
        log.debug("rtp_header_built", seq=call.args[0], ts=call.args[1], ssrc=call.args[2])
    elif method == 'encrypt_rtp':
        log.debug("rtp_encrypted", payload_size=len(call.args[1]))
    elif method == 'send_frame':
        log.debug("frame_sending", pts_ms=call.args[1])
    result = yield aspectlib.Proceed
    if method == 'send_frame':
        log.debug("frame_sent", packets=result)
    yield aspectlib.Return(result)
```

### 3.3 Timing Aspect

Automatically measure execution time of any function:

```python
@aspectlib.aspect
def timing(call):
    """Measure and log execution time of wrapped functions."""
    start = time.perf_counter()
    result = yield aspectlib.Proceed
    elapsed_ms = (time.perf_counter() - start) * 1000
    if elapsed_ms > 5.0:  # only log slow calls
        log.warning("slow_call", method=call.func.__name__, elapsed_ms=elapsed_ms)
    yield aspectlib.Return(result)

# Apply to all media pipeline methods
aspectlib.weave([FFmpegProcess, Demuxer, VideoSender, AudioSender], timing)
```

### 3.4 Error Propagation Aspect

Catch and enrich errors across the pipeline:

```python
@aspectlib.aspect
def error_enrich(call):
    """Enrich errors with context before propagation."""
    try:
        result = yield aspectlib.Proceed
        yield aspectlib.Return(result)
    except Exception as e:
        log.error("pipeline_error",
            method=call.func.__name__,
            error_type=type(e).__name__,
            error_msg=str(e),
            # Add context: connection state, last opcode, SSRCs
        )
        raise
```

### 3.5 State Transition Aspect

Track connection state changes:

```python
@aspectlib.aspect
def state_tracker(call):
    """Track and log all state transitions."""
    old_state = dict(self.__dict__)  # snapshot before
    result = yield aspectlib.Proceed
    new_state = dict(self.__dict__)
    changes = {k: (old_state.get(k), new_state[k]) for k in new_state if old_state.get(k) != new_state[k]}
    if changes:
        log.info("state_change", method=call.func.__name__, changes=changes)
    yield aspectlib.Return(result)
```

---

## 4. Enhanced TDD with AOP

### 4.1 Capture-Replay Testing

aspectlib's capture-replay framework records real protocol interactions and replays them in tests:

```python
# Step 1: Record real session
import aspectlib

story = aspectlib.Story()
aspectlib.weave(StreamConnection, story.recorder)

# ... run real Go Live session ...
# All StreamConnection method calls are now recorded

story.save("test_stories/go_live_session.json")

# Step 2: Replay in tests
def test_go_live_session():
    story = aspectlib.Story.load("test_stories/go_live_session.json")
    with aspectlib.Replay(story):
        # All StreamConnection calls return recorded responses
        streamer = VideoStreamer(mock_client)
        await streamer.start_go_live()
        # Verifies the sequence matches the recording
```

### 4.2 Contract Testing with Aspects

Assert invariants on every function call without modifying the functions:

```python
@aspectlib.aspect
def rtp_contract(call):
    """Verify RTP packet invariants on every build."""
    result = yield aspectlib.Proceed
    if call.func.__name__ == 'build_rtp_header':
        header = result
        assert header[0] >> 6 == 2, "RTP version must be 2"
        seq = struct.unpack('>H', header[2:4])[0]
        assert 0 <= seq <= 65535, "Sequence out of range"
    yield aspectlib.Return(result)

aspectlib.weave(rtp.serialize, rtp_contract)
```

### 4.3 Property-Based Testing with Instrumentation

Combine hypothesis (property-based testing) with AOP instrumentation:

```python
from hypothesis import given, strategies as st

@given(
    seq=st.integers(min_value=0, max_value=65535),
    ts=st.integers(min_value=0, max_value=2**32 - 1),
    ssrc=st.integers(min_value=0, max_value=2**32 - 1),
)
def test_rtp_roundtrip(seq, ts, ssrc):
    header = build_rtp_header(seq, ts, ssrc, payload_type=101)
    parsed_seq, parsed_ts, parsed_ssrc = struct.unpack('>xxHII', header)
    assert parsed_seq == seq & 0xFFFF
    assert parsed_ts == ts & 0xFFFFFFFF
    assert parsed_ssrc == ssrc & 0xFFFFFFFF
```

---

## 5. Development Workflow

### 5.1 Environment Setup

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Core dependencies
pip install discord.py-self[voice] av websockets pynacl

# Development dependencies
pip install aspectlib structlog rich
pip install pytest pytest-asyncio hypothesis
pip install black isort pyright

# Optional: IPC tools
pip install socat  # or use built-in socat/nc
```

### 5.2 Project Structure for Dev Workflow

```
discord_video_stream/
    __init__.py
    streamer.py
    stream_connection.py
    voice_send.py
    rtp/          (serialize.py, h264.py, crypto.py)
    media/        (ffmpeg.py, demux.py, pacer.py)
    protocol/     (types.py, vui.py)
    compat/       (voice_recv.py)
    tests/
dev/
    aspects.py          # All AOP aspects defined here
    debug_server.py     # IPC debug channel
    capture_replay.py   # Capture/replay utilities
    fixtures/           # Recorded protocol sessions
    hypothesis_strategies.py  # Property-based test strategies
```

### 5.3 Debug Session Workflow

1. Start the bot with debug server:
```python
# bot.py
from dev.aspects import apply_debug_aspects
from dev.debug_server import DebugServer

apply_debug_aspects()  # Weave all debug aspects into the codebase
debug = DebugServer(streamer, path="/tmp/discord-debug.sock")
asyncio.ensure_future(debug.start())
```

2. From another terminal, inspect state:
```bash
echo "status" | socat - UNIX-CONNECT:/tmp/discord-debug.sock
echo "state" | socat - UNIX-CONNECT:/tmp/discord-debug.sock
echo "packets 100" | socat - UNIX-CONNECT:/tmp/discord-debug.sock  # last 100 packets
```

3. Structured logs appear in the bot's terminal with full context:
```
[13:10:05] [info] video_frame_sent ssrc=2000 opcode=12 packets=5 pts_ms=100.0 size_bytes=8500
[13:10:05] [debug] rtp_encrypted payload_size=1280 nonce_counter=42
[13:10:05] [warning] slow_call method=demux elapsed_ms=12.5
```

### 5.4 TDD Workflow

1. Write the test first (contract/property-based)
2. Write minimal implementation
3. Run tests with AOP aspects active (they catch invariant violations at runtime)
4. If a bug is found, capture a replay from a real session
5. Add the replay as a regression test
6. Fix the code, verify the replay still passes

### 5.5 Bug Investigation Workflow

1. Reproduce the bug with the debug server running
2. Check structured logs for the error sequence
3. Use `aspectlib.weave(suspected_module, debug_trace)` to instrument the suspect
4. If needed, capture a replay of the failing session
5. Write a test that replays the captured session
6. Fix the bug, verify the replay passes
7. Keep the replay as a permanent regression test

---

## 6. Specific AOP Wins for This Project

### 6.1 Replacing Scattered log.debug() Calls

Currently, `stream_connection.py` has ~30 manual `log.debug()` calls. With AOP, you define one aspect and apply it to all protocol methods. Adding logging to a new method requires zero code changes.

### 6.2 Packet Inspection Without Code Changes

During debugging, you can temporarily weave a packet inspector:

```python
aspectlib.weave(VideoSender, packet_inspector)
# ... debug the issue ...
aspectlib.weave(VideoSender, aspectlib.Rollback)  # remove all aspects
```

### 6.3 Third-Party Instrumentation

You can instrument discord.py internals, PyAV calls, and nacl operations without modifying their source:

```python
aspectlib.weave(discord.VoiceClient, log_calls)  # trace all VoiceClient method calls
aspectlib.weave(nacl.secret.Aead, timing)  # measure encryption latency
```

### 6.4 Test Isolation

With `aspectlib.Test`, you can mock any module for a specific test without permanent monkey-patching:

```python
def test_without_dave():
    with aspectlib.Test(davey, 'DaveSession') as mock:
        mock.encrypt.return_value = b'encrypted'
        # test code here
    # davey is automatically restored
```

---

## 7. Decision Matrix

| Approach | Logging | Timing | Packet Trace | State Track | Testing | IPC Debug |
|---|---|---|---|---|---|---|
| Manual decorators | OK | OK | Tedious | Tedious | OK | N/A |
| aspectlib weave | Excellent | Excellent | Excellent | Excellent | Excellent | Excellent |
| sys.monitoring (3.12+) | Low-level | Excellent | Low-level | Low-level | N/A | N/A |
| structlog alone | Excellent | N/A | Manual | Manual | N/A | N/A |

**Recommendation**: Use `aspectlib` as the core AOP layer, `structlog` for structured output, `rich` for visual formatting. Together they cover all concerns.

---

## 8. Risks, Mitigations, and Known Quirks

### aspectlib 2.0.0 Quirks

- **Rollback on `__main__` functions**: `aspectlib.weave(func, aspectlib.Rollback)` fails when the function is defined in `__main__` because aspectlib tries to find the function in the module namespace. This does NOT affect functions in proper packages. Workaround: weave/rollback on classes or modules, not standalone functions.

- **Class weave and `self` binding**: When weaving a class with `@aspectlib.Aspect(bind=True)`, the `cutpoint` is the unbound function. Calling `cutpoint(*args, **kwargs)` requires passing `self` explicitly. This is handled correctly in our aspects (we only read `cutpoint.__name__`, never call it).

- **Weave state persistence**: `aspectlib.weave(target, aspectlib.Rollback)` must be called explicitly to undo a weave. If a test weaves and doesn't rollback, subsequent tests see the woven version. Our `remove_debug_aspects()` handles this.

- **`@aspectlib.Aspect` vs `@aspectlib.aspect`**: Version 2.0.0 uses `@aspectlib.Aspect` (capital A) and `@aspectlib.Aspect(bind=True)` for access to the cutpoint function. The lowercase `@aspectlib.aspect` does not exist in 2.0.0.

### General Risks

| Risk | Mitigation |
|---|---|
| aspectlib weave is slow for hot paths | Only weave in dev mode; production uses static decorators |
| aspectlib is old (last release 2016) | Still works on Python 3.12+; fork if needed |
| IPC debug socket security | Unix socket with permissions; only accessible locally |
| Over-instrumentation makes logs noisy | Configurable log levels per aspect; filter by module |
| Capture/replay files grow large | Limit recording duration; compress old recordings |

---

## 9. Action Items

1. Install dependencies: `pip install aspectlib structlog rich hypothesis`
2. Create `dev/aspects.py` with core aspects (logging, timing, packet trace, state track)
3. Create `dev/debug_server.py` with IPC Unix socket server
4. Create `dev/capture_replay.py` with session recording utilities
5. Update test infrastructure to support aspect-enhanced TDD
6. Document the workflow in a DEVELOPMENT.md
