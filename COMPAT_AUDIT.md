# Compatibility Audit & Workspace Notes

## dpy-self Deep Transitive Dependency Audit

### Core Voice Dependencies

| Dependency | discord.py 2.7.1 | dpy-self 2.1.0 | Status |
|---|---|---|---|
| pynacl (nacl) | Imported in voice_client.py | Imported in voice_client.py (try/except) | Compatible |
| davey | Imported in voice_state.py (try/except) | Imported in voice_state.py (try/except) | Compatible |
| davey encrypt() API | `encrypt(media_type, codec, packet)` | Same (same davey 0.1.5) | Compatible |

### WebSocket Infrastructure

| Component | discord.py 2.7.1 | dpy-self 2.1.0 | Impact |
|---|---|---|---|
| Main WS | aiohttp | curl_cffi.requests.AsyncWebSocket | Different internals, same external API |
| Voice WS | aiohttp | curl_cffi.requests.AsyncWebSocket | Different internals, same external API |
| Binary WS frames | aiohttp | curl_cffi.const.CurlWsFlag.BINARY | Different transport layer |

**Key finding:** dpy-self replaces aiohttp with curl_cffi for ALL WebSocket connections (main gateway + voice). This is an internal implementation detail and does NOT affect the external API surface. The VoiceClient, VoiceConnectionState, and DiscordVoiceWebSocket classes have identical method signatures.

**Impact on our Go Live WS:** We need a SEPARATE WebSocket connection to the stream voice server. Options:
1. `websockets` library (pure Python, independent of both aiohttp and curl_cffi) -- RECOMMENDED
2. `curl_cffi` directly -- works but ties us to dpy-self's internals
3. `aiohttp` -- would need to be installed separately, may conflict with dpy-self

**Decision:** Use `websockets` library. It is independent, async-native, and avoids any coupling to dpy-self's transport layer.

### Voice-recv Compatibility

| Check | discord.py 2.7.1 | dpy-self 2.1.0 |
|---|---|---|
| `from discord.ext.voice_recv import VoiceRecvClient` | OK | OK |
| `from discord.ext.voice_recv.rtp import RTPPacket` | OK | OK |
| `from discord.ext.voice_recv.reader import PacketDecryptor` | OK | OK |
| `from discord.ext.voice_recv.gateway import hook` | OK | OK |
| `VoiceRecvClient.create_connection_state()` | Works | Works (same VoiceConnectionState API) |
| `SpeakingTimer._lookup_member()` | Uses guild.get_member() | Uses guild.get_member() or client.get_user() (dm-voice fix) |

voice-recv dm-voice branch imports successfully with dpy-self 2.1.0. All modules load without errors.

### DAVE Session API (davey 0.1.5)

Verified against pyi typings at `/usr/local/lib/python3.12/dist-packages/davey/__init__.pyi`:

```python
# The method signature we need for video encryption:
DaveSession.encrypt(media_type: MediaType, codec: Codec, packet: bytes) -> bytes

# Where:
MediaType.video = 1
Codec.h264 = 4
Codec.h265 = 5
Codec.vp8 = 2
Codec.vp9 = 3
Codec.av1 = 6
Codec.opus = 1
```

This is the SAME davey library used by both discord.py 2.7.1 and dpy-self 2.1.0. No version conflicts.

### Summary of Compatibility

| Component | Status | Notes |
|---|---|---|
| VoiceClient API | Identical | Same attributes, methods, encryption |
| VoiceConnectionState API | Identical | Same socket, secret_key, DAVE session |
| DAVE (davey) | Identical | Same version 0.1.5, same API |
| RTP packet format | Identical | Same header struct, same encryption modes |
| voice-recv extension | Compatible | dm-voice branch works with both |
| Gateway WS | Different internals | curl_cffi vs aiohttp, same external API |
| Voice WS | Different internals | curl_cffi vs aiohttp, same external API |

**Conclusion:** Our video send library can target both discord.py and dpy-self with no code changes, as long as we:
1. Do NOT depend on aiohttp (use websockets for Go Live WS)
2. Do NOT depend on internal WebSocket transport details
3. Use discord.VoiceClient as our base class (works in both)

---

## Node.js Reference Implementation Summary

### Files We Need to Port (in priority order)

1. **CodecPayloadType.ts** (40 lines) -> `protocol/types.py` -- Codec config constants, fully documented
2. **VoiceOpCodes.ts** (48 lines) -> `protocol/types.py` -- Already defined in voice-recv's gateway.py
3. **StreamConnection.ts** (18 lines) -> `stream_connection.py` -- Go Live connection subclass
4. **BaseMediaConnection.ts** (396 lines) -> `stream_connection.py` -- Voice WS, DAVE, protocols, heartbeat
5. **WebRtcWrapper.ts** (186 lines) -> NOT NEEDED (we use UDP instead of WebRTC)
6. **Streamer.ts** (259 lines) -> `streamer.py` -- Main API, gateway opcodes
7. **BaseMediaStream.ts** (198 lines) -> `media/pacer.py` -- Frame pacing algorithm
8. **SPSVUIRewriter.ts** (222 lines) -> `protocol/vui.py` -- SPS bitstream manipulation
9. **AnnexBBitstreamReaderWriter.ts** (259 lines) -> `protocol/vui.py` -- Exp-Golomb reader/writer
10. **AnnexBHelper.ts** (part of processing/) -> `rtp/h264.py` -- NALU splitting
11. **LibavDemuxer.ts** (291 lines) -> `media/demux.py` -- Frame extraction
12. **VideoStream.ts / AudioStream.ts** (35 lines) -> `media/pacer.py` -- Stream wrappers

### Key Architectural Differences (Node.js vs Python)

| Aspect | Node.js | Python (our approach) |
|---|---|---|
| Transport | WebRTC (node-datachannel) | UDP (discord.py's socket) |
| RTP packetization | node-datachannel H264RtpPacketizer | We implement manually (FU-A/STAP-A) |
| DAVE session | Separate per connection | Shared via discord.py's VoiceConnectionState |
| Frame pacing | BaseMediaStream writable stream | asyncio FramePacer |
| FFmpeg | FFmpeg subprocess + node-av demuxer | FFmpeg subprocess + PyAV demuxer |
| WebSocket | Browser WebSocket API | websockets library |
| Gateway | discord.js selfbot raw events | discord.py gateway dispatch |

### What the Node.js Lib Confirms

1. **Go Live daveChannelId = BigInt(serverId) - 1** -- Confirmed in StreamConnection.ts line 18-20
2. **Speaking mode = 2 for Go Live** -- Confirmed in StreamConnection.ts `setSpeaking()` (speaking ? 2 : 0)
3. **STREAM_CREATE gateway opcode = 18** -- Confirmed in GatewayOpCodes.ts
4. **STREAM_DELETE gateway opcode = 19** -- Confirmed in GatewayOpCodes.ts
5. **STREAM_SET_PAUSED gateway opcode = 22** -- Confirmed in GatewayOpCodes.ts
6. **Codec payload types** -- Confirmed: H264 PT 101, RTX 102, Opus PT 120
7. **SPS VUI rewriting** -- Full implementation confirmed, matches CONTEXT.md
8. **Frame pacing algorithm** -- Full implementation confirmed in BaseMediaStream.ts
9. **PacingHandler rate = 25 Mbps, burst = 1** -- Confirmed in WebRtcWrapper.ts
10. **DAVE encrypt before RTP packetize** -- Confirmed in WebRtcWrapper.ts sendVideoFrame()

---

## Project Repos

- discord-ext-voice-recv (dm-voice branch): `/root/.openclaw/workspace/discord-ext-voice-recv`
- discord-video-stream (Node.js reference): `/root/.openclaw/workspace/discord-video-stream`
- EXECUTIVE_PLAN.md: `/root/.openclaw/workspace/EXECUTIVE_PLAN.md`
- This file: `/root/.openclaw/workspace/COMPAT_AUDIT.md`

## Installed Python Packages

| Package | Version | Purpose |
|---|---|---|
| dpy-self | 2.1.0 | Base library (replaces discord.py 2.7.1) |
| davey | 0.1.5 | DAVE/E2EE protocol |
| pynacl | 1.5.0 | Transport encryption (AEAD) |
| aiohttp | 3.13.5 | (installed, but dpy-self uses curl_cffi) |
| PyAV | NOT INSTALLED | Will install for NUT demuxing |
| websockets | NOT INSTALLED | Will install for Go Live WS |
