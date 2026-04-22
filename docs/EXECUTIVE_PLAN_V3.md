# Executive Plan V3: Discord Video Stream Python Port — Continuation & Debug

## Date: 2026-04-23
## Agent: New session, full codebase analysis completed

---

## 1. Complete Codebase Analysis Summary

### 1.1 What Exists (Phases 1-6, all completed by previous agent)

The `video-stream-port` branch of `Yui-Koi/discord-ext-voice-recv` contains a **complete** implementation:

| Phase | Files | Lines | Status |
|-------|-------|-------|--------|
| Phase 1: RTP + Crypto | `rtp/serialize.py`, `rtp/crypto.py`, `rtp/h264.py` | 526 | ✅ Complete |
| Phase 2: FFmpeg + Demux | `media/ffmpeg.py`, `media/demux.py` | 569 | ✅ Complete |
| Phase 3: Frame Pacing | `media/pacer.py` | 204 | ✅ Complete |
| Phase 4: Protocol + VUI | `protocol/types.py`, `protocol/vui.py` | 751 | ✅ Complete |
| Phase 5: Stream Connection | `stream_connection.py`, `voice_send.py`, `streamer.py` | ~1,400 | ✅ Complete |
| Phase 6: Integration | `compat/voice_recv.py`, `__init__.py` | ~130 | ✅ Complete |
| Tests | 9 test files | ~2,500 | ✅ 205 tests passing |

**Total codebase: ~8,283 lines across 24 Python files.**

### 1.2 Validation Against Reference Repos

All components have been cross-referenced against:
- **Node.js reference** (`Discord-RE/Discord-video-stream`): Codec PTs, SPS VUI, frame pacing, DAVE flow, Go Live lifecycle
- **discord-ext-voice-recv** (Yui-Koi, dm-voice branch): RTP format, decrypt→encrypt mirror, gateway hooks
- **discord.py / dpy-self**: VoiceClient internals, DAVE session API, UDP socket, encryption modes
- **davey 0.1.0**: DaveSession.encrypt(media_type, codec, packet) API confirmed working

### 1.3 What I Verified (Fresh Pass)

I performed a complete line-by-line analysis of all 24 source files and all documentation. Key findings:

1. **All Phase 1-4 code is correct** — RTP serialization, transport encryption, H.264 packetization, FFmpeg command construction, NUT demuxing, frame pacing, protocol types, SPS VUI rewriter all match the reference implementation.

2. **Phase 5 code is architecturally correct** but contains a **critical bug** that causes error 2012.

3. **Phase 6 code is correct** — compat module properly extends VoiceRecvClient.

---

## 2. ROOT CAUSE OF ERROR 2012 — CRITICAL BUG FOUND

### 2.1 The Bug

**Video RTP packets are sent to the WRONG endpoint.**

In `streamer.py`, the `send_udp` callback (lines 434-448):

```python
def send_udp(packet: bytes) -> None:
    if self._voice_client is not None and hasattr(self._voice_client, '_connection'):
        conn = self._voice_client._connection
        if hasattr(conn, 'socket') and conn.socket is not None:
            conn.socket.sendto(
                packet,
                (conn.endpoint_ip, conn.voice_port),  # ← BUG: main voice endpoint
            )
```

This sends video packets to the **main voice connection's endpoint** (`conn.endpoint_ip`, `conn.voice_port`). But the Go Live stream has its own separate voice server with its own IP and port.

### 2.2 Why This Causes Error 2012

Discord Error 2012 is a **video streaming timeout** — the video connection failed to initialize within the allowed time. The flow:

1. Bot joins voice channel → voice connection established to voice server A (IP_A:port_A)
2. Bot sends STREAM_CREATE → Discord creates a stream and sends STREAM_SERVER_UPDATE with a **different** endpoint (IP_B:port_B)
3. Bot connects stream WebSocket to IP_B → handshake completes, gets stream SSRCs and secret key
4. Bot starts sending video RTP packets → **but sends them to IP_A:port_A** (wrong server!)
5. Stream server at IP_B never receives any video data
6. Discord client times out waiting for video → **Error 2012: Stream failed to start**

### 2.3 The Fix

The `send_udp` callback must send to the **stream connection's** endpoint, not the main voice connection's endpoint. The stream connection's `_ready_params` object contains `ip` and `port` from the stream server's READY opcode.

**Fix location:** `streamer.py`, `play()` method, the `send_udp` closure.

**Fix approach:**
```python
def send_udp(packet: bytes) -> None:
    # Use stream connection's endpoint, NOT the main voice connection's
    stream_ready = self._stream_conn.ready_params
    if stream_ready is None:
        log.warning('Stream not ready, cannot send')
        return
    if self._voice_client is not None and hasattr(self._voice_client, '_connection'):
        conn = self._voice_client._connection
        if hasattr(conn, 'socket') and conn.socket is not None:
            conn.socket.sendto(
                packet,
                (stream_ready.ip, stream_ready.port),  # ← CORRECT: stream endpoint
            )
```

**Alternative approach (preferred):** Pass the stream connection's endpoint into the `VideoSender` at `start()` time and let it manage its own sending, rather than routing through the streamer's callback.

### 2.4 Secondary Issue: The Stream's UDP Socket

The stream connection's READY payload provides an IP and port. The Node.js reference uses WebRTC (which handles its own transport), but our UDP path needs to send to the stream server's address.

**Important question:** Does the stream server share the same UDP socket as the main voice connection, or does it need a separate socket?

From the Node.js reference analysis:
- The stream connection extends `BaseMediaConnection` which has its own `_webRtcWrapper` with its own `PeerConnection`
- This means the Node.js lib uses a **separate** WebRTC connection (and therefore separate UDP transport) for the stream

For our UDP approach:
- We can use the **same** UDP socket (it's already bound to a local port)
- But we must send to the **stream server's** IP:port, not the main voice server's IP:port
- The stream server's IP:port comes from `_ready_params.ip` and `_ready_params.port`

**However**, there's a subtlety: the main voice connection's UDP socket was created with IP discovery (sending a specific packet to discover our external IP). The stream server may need its own IP discovery, or it may accept packets from the same source.

Looking at the Discord docs: "For send-only connections, the address and port in SELECT_PROTOCOL data can be randomized." This means we don't need IP discovery for the stream — we just need to send to the stream server's address.

**Decision:** Use the same UDP socket, send to stream's IP:port. This is the simplest approach and should work because:
1. UDP is connectionless — same socket can send to different destinations
2. The stream server identifies us by our SSRC (in the RTP packets), not by our source IP:port
3. The SELECT_PROTOCOL we send includes our (randomized) address/port for receiving, which we don't need

---

## 3. Additional Issues Found

### 3.1 `_send_json` Uses `asyncio.ensure_future` (Fire-and-Forget)

In `stream_connection.py`, `_send_json` and `_send_binary` use `asyncio.ensure_future(self._ws.send(...))`. This means sends are fire-and-forget — errors are silently swallowed. While this is fine for the initial implementation, it could cause issues if the WebSocket is in a closing state.

**Severity:** Low — the WebSocket state check before sending mitigates most issues.

### 3.2 `identify()` Called Before Receive Loop Starts

In `connect()`, `identify()` is called synchronously before the receive loop task is created. The `identify()` method calls `_send_json` which uses `asyncio.ensure_future`. This means the IDENTIFY message is queued but may not be sent before the receive loop starts processing messages.

**Severity:** Low — `ensure_future` will send the message on the next event loop iteration, and the receive loop also runs on the event loop. Since we `await self._ready_event.wait()`, the IDENTIFY will be sent before we time out.

### 3.3 Voice Gateway Version

The code uses `v=8` for the stream WebSocket URL. The Discord docs recommend `v=9` which adds `channel_id` to IDENTIFY. Using v8 should still work but v9 is recommended.

**Severity:** Low — v8 works, v9 is a nice-to-have.

### 3.4 SELECT_PROTOCOL Encryption Mode Preference

In `_send_select_protocol`, the code uses `SUPPORTED_ENCRYPTION_MODES[1]` which is `aead_xchacha20_poly1305_rtpsize`. The Node.js reference uses the same. However, the array is ordered with AES-256-GCM first (index 0) and XChaCha20 second (index 1). Discord prefers AES-256-GCM when available (hardware acceleration).

**Severity:** Low — both modes work. XChaCha20 is actually more portable since it doesn't require AES-NI.

### 3.5 RTCP Sender Reports Not Periodic

The `send_rtcp_sender_report()` method exists in `VideoSender` but is never called periodically. RTCP SRs are important for A/V sync on the receiver side.

**Severity:** Medium — streams will work without RTCP SRs but A/V sync may drift.

### 3.6 No RTX Retransmission

The Node.js reference uses `RtcpNackResponder` to handle NACK requests (retransmit lost packets). Our implementation doesn't handle incoming RTCP NACKs.

**Severity:** Low for MVP — Go Live is one-way, and packet loss just means brief quality degradation.

### 3.7 `_connected_users` Not Populated Initially

The `StreamConnection._connected_users` set is populated via CLIENTS_CONNECT events. But the initial proposals may reference users who connected before us. The `process_proposals` call passes `list(self._connected_users)` as `expected_user_ids`, which may be empty initially.

**Severity:** Medium — this could cause DAVE key exchange to fail if the server expects specific user IDs in the proposals response. Need to verify if `expected_user_ids` is required or optional in davey.

---

## 4. Executive Plan: Fix Error 2012 + Validation

### Phase 7A: Fix UDP Endpoint Bug (CRITICAL — Error 2012 Fix)

**What:** Fix the `send_udp` callback in `streamer.py` to send to the stream server's endpoint instead of the main voice connection's endpoint.

**Files to modify:**
- `streamer.py` — Fix `send_udp` closure in `play()` method
- `voice_send.py` — Optionally refactor to accept stream endpoint at `start()` time

**Approach:**
1. In `VideoSender.start()`, accept the stream connection's IP and port
2. Store them as `_target_ip` and `_target_port`
3. In `_send_udp()`, use these instead of relying on the callback
4. The callback still needs the socket from the main voice connection
5. Refactor: `set_send_callback(callback, ip, port)` or pass at `start()` time

**Validation:**
- Unit test: mock socket, verify `sendto` is called with stream's IP:port
- Integration test: connect to Discord, start Go Live, verify no error 2012

### Phase 7B: Add Stream Endpoint to VideoSender

**What:** Refactor `VideoSender` to store and use the stream connection's endpoint.

**Changes to `voice_send.py`:**
```python
def start(self, target_ip: str, target_port: int) -> None:
    """Initialize with stream server endpoint."""
    ...
    self._target_ip = target_ip
    self._target_port = target_port

def _send_udp(self, packet: bytes) -> None:
    """Send to stream server endpoint."""
    if self._send_callback:
        self._send_callback(packet, self._target_ip, self._target_port)
```

**Changes to `streamer.py`:**
```python
# In play():
stream_ready = self._stream_conn.ready_params
self._video_sender.start(
    target_ip=stream_ready.ip,
    target_port=stream_ready.port,
)

# send_udp callback:
def send_udp(packet: bytes, ip: str, port: int) -> None:
    conn = self._voice_client._connection
    conn.socket.sendto(packet, (ip, port))
```

### Phase 7C: Validation Pass

**What:** Run all existing tests, then add targeted tests for the endpoint fix.

**Tests to add:**
1. `test_sender_uses_stream_endpoint` — Verify sendto is called with stream's IP:port
2. `test_sender_not_using_main_voice_endpoint` — Verify it does NOT use main voice endpoint
3. `test_stream_ready_params_ip_port` — Verify READY params extraction

---

## 5. Longer-Term Improvements (Post Error-2012 Fix)

### 5.1 Periodic RTCP Sender Reports
Add a background task that sends RTCP SRs every ~5 seconds for A/V sync.

### 5.2 Voice Gateway v9
Update `wss://{endpoint}/?v=8` to `?v=9` and add `channel_id` to IDENTIFY.

### 5.3 Proper Error Handling
Replace `asyncio.ensure_future` with proper error-propagating patterns in `_send_json`/`_send_binary`.

### 5.4 NUT Pipe Latency Benchmark
Test PyAV NUT demuxing latency. If >5ms/frame, implement raw Annex-B fallback.

### 5.5 Multiple Simulcast Streams
Investigate sending multiple quality levels (rid "50" + rid "100").

---

## 6. Priority Order

```
┌─────────────────────────────────────────────────┐
│ PRIORITY 1: Fix send_udp endpoint bug           │ ← Error 2012 fix
│ PRIORITY 2: Run existing test suite             │ ← Verify no regressions
│ PRIORITY 3: Add endpoint-specific tests         │ ← Prevent recurrence
│ PRIORITY 4: Integration test with Discord       │ ← Verify end-to-end
│ PRIORITY 5: RTCP Sender Reports (periodic)      │ ← A/V sync quality
│ PRIORITY 6: Voice Gateway v9                    │ ← Future-proofing
└─────────────────────────────────────────────────┘
```

---

## 7. File Impact Summary

| File | Action | Change |
|------|--------|--------|
| `streamer.py` | MODIFY | Fix `send_udp` to use stream endpoint |
| `voice_send.py` | MODIFY | Accept target IP/port at `start()` time |
| `tests/test_video_sender.py` | ADD | Endpoint-specific tests |
| `tests/test_streamer.py` | ADD | Integration endpoint tests |

Estimated change: ~30-50 lines modified, ~50 lines of new tests.

---

## 8. Risk Assessment

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| Stream endpoint fix doesn't resolve 2012 | High | Low | Add packet logging, verify with Wireshark |
| Stream server needs separate UDP socket | Medium | Low | Test with same socket first; if fails, create new socket |
| DAVE session for stream differs from voice | Medium | Medium | Already handled — separate DaveSession in stream_connection.py |
| PyAV NUT latency too high | Medium | Low | Fallback to raw Annex-B (already documented) |
| davey version 0.1.0 vs 0.1.5 discrepancy | Low | Low | API confirmed identical, only version string differs |
