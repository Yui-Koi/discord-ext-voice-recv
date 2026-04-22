# Executive Plan: Discord Video Stream Python Port

## Project Goal

Port `@dank074/discord-video-stream` (Node.js, 4319 lines) to Python, building on top of discord.py/dpy-self's existing voice infrastructure and Yui-Koi's discord-ext-voice-recv (dm-voice branch). The deliverable is a Python package capable of sending H.264 video + Opus audio via Discord Go Live in both guild and DM/group voice channels.

---

## Current State Assessment

### What We Have

| Asset | Status | Lines | Role |
|-------|--------|-------|------|
| discord-ext-voice-recv (dm-voice branch) | ✅ Cloned, analyzed | 3078 | Receive-only voice extension |
| Node.js discord-video-stream | ✅ Fully documented in CONTEXT.md | 4319 | Reference implementation to port |
| Protocol knowledge | ✅ Exhaustive (CONTEXT.md, OBSERVATIONS.md) | N/A | Voice WS v8/v9, DAVE, RTP, H.264 |
| discord.py / dpy-self | ✅ Both installed and validated | N/A | Connection, DAVE, UDP socket, audio send |

### Validation Results (Passed ✅)

| Check | Result |
|-------|--------|
| discord.py 2.7.1 has VoiceClient.dave_session | ✅ Confirmed |
| discord.py 2.7.1 has VoiceClient._get_voice_packet + encrypt methods | ✅ Confirmed |
| discord.py 2.7.1 has VoiceConnectionState with socket/secret_key/endpoint_ip | ✅ Confirmed |
| discord.py 2.7.1 has VoiceConnectionState.add_socket_listener/remove_socket_listener | ✅ Confirmed |
| dpy-self 2.1.0 has identical VoiceClient/VoiceConnectionState structure | ✅ Confirmed |
| dpy-self 2.1.0 uses davey for DAVE (same API) | ✅ Confirmed |
| davey 0.1.5 has DaveSession.encrypt(media_type, codec, packet) for video | ✅ Confirmed via pyi typings |
| davey 0.1.5 has Codec.h264 (value 4) and MediaType.video (value 1) | ✅ Confirmed |
| discord-ext-voice-recv DM voice fixes (gateway.py) | ✅ All guild.get_member calls have client.get_user fallback |
| discord-ext-voice-recv PacketDecryptor aead_xchacha20_poly1305_rtpsize | ✅ Matches discord.py's encrypt implementation |

### One Minor Discrepancy Found (Non-Blocking)

The discord-ext-voice-recv README says "discord.py" but setup.py requires `discord.py-self[voice]`. This is a documentation inconsistency only — the code works with both since their APIs are structurally identical for our purposes.

---

## Why This Works (Key Architectural Insight)

discord.py/dpy-self already handles ~60% of what's needed:

```
HANDLED BY DISCORD.PY/DPI-SELF:          WE BUILD:
┌─────────────────────────────┐          ┌──────────────────────────┐
│ Gateway connection          │          │ RTP packet construction  │
│ Voice WS handshake          │          │ H.264 NALU packetization │
│ DAVE/MLS key exchange       │          │ Transport encryption     │
│ (via davey)                 │          │ (encrypt direction)      │
│ UDP socket                  │          │ DAVE video encryption    │
│ Secret key storage          │          │ FFmpeg pipeline          │
│ SSRC assignment             │          │ NUT demuxing             │
│ Heartbeat / reconnect       │          │ Frame pacing + A/V sync  │
│ Audio send path             │          │ Go Live stream lifecycle │
│ Socket reader               │          │ VIDEO opcode (send)      │
└─────────────────────────────┘          │ SPS VUI rewriter         │
                                         └──────────────────────────┘
```

---

## Implementation Phases

### Phase 1: RTP Foundation + Transport Crypto (~400 lines)

**What:** Build the packet construction and encryption layer.

| File | Lines | Purpose |
|------|-------|---------|
| `rtp/serialize.py` | 120 | RTP header builder, RTCP SR builder |
| `rtp/crypto.py` | 80 | Transport encryption (AEAD encrypt side, mirrors reader.py's PacketDecryptor) |
| `rtp/h264.py` | 200 | NALU splitting, FU-A/STAP-A fragmentation, marker bit logic |

**Key references:**
- `discord/ext/voice_recv/rtp.py` → header format `_hstruct = struct.Struct('>xxHII')`
- `discord/ext/voice_recv/reader.py` → `PacketDecryptor._decrypt_rtp_aead_xchacha20_poly1305_rtpsize` (reverse for encrypt)
- `discord.py voice_client.py` → `_encrypt_aead_xchacha20_poly1305_rtpsize` (ready-made encrypt implementation to copy)

**Dependencies:** pynacl (already required by discord.py)

**Validation gate:** Unit tests for RTP serialization round-trip and encrypt/decrypt round-trip.

---

### Phase 2: FFmpeg Pipeline + Demuxing (~330 lines)

**What:** Transcode input media and extract frames for RTP packetization.

| File | Lines | Purpose |
|------|-------|---------|
| `media/ffmpeg.py` | 150 | FFmpeg subprocess, command construction with critical flags |
| `media/demux.py` | 180 | NUT demuxing (PyAV primary, raw Annex-B fallback) |

**Critical FFmpeg flags** (validated from Node.js reference):
- `-bf 0` — No B-frames (essential for low latency)
- `-preset superfast` — NOT ultrafast (causes bitrate spikes)
- `-forced-idr 1` — Every keyframe is IDR (SFU recovery)
- `-force_key_frames expr:gte(t,n_forced*1)` — 1s keyframe interval
- `-pix_fmt yuv420p` — Only 4:2:0 chroma Discord supports

**Dependencies:** PyAV, system FFmpeg

**Validation gate:** FFmpeg produces NUT pipe, PyAV demuxes to correct PTS values.

---

### Phase 3: Frame Pacing + A/V Sync (~150 lines)

**What:** Send frames at correct timing, maintain lip sync.

| File | Lines | Purpose |
|------|-------|---------|
| `media/pacer.py` | 150 | Frame pacing with A/V sync |

**Algorithm** (ported from Node.js BaseMediaStream):
1. First frame: record start_time (wall clock) + start_pts
2. Each frame: `sleep = pts - start_pts + frametime - (now - start_time)`
3. Sync tolerance: 20ms
4. Video behind → skip sleep; Video ahead → wait in loop

**Dependencies:** asyncio

**Validation gate:** Feed known PTS values, verify timing correctness.

---

### Phase 4: Protocol Types + SPS VUI (~300 lines)

**What:** Wire protocol structures and H.264 SPS manipulation.

| File | Lines | Purpose |
|------|-------|---------|
| `protocol/types.py` | 100 | Codec config, stream key generation/parsing, VIDEO opcode payload |
| `protocol/vui.py` | 200 | SPS VUI rewriter (port from TypeScript, originally from WebRTC C++) |

**SPS VUI changes:** Force `bitstream_restriction_flag=1`, `max_num_reorder_frames=0`, strip `video_signal_type`.

**Dependencies:** None (pure Python bitstream manipulation)

**Validation gate:** Rewrite known SPS NALU, verify output bitstream.

---

### Phase 5: Stream Connection + Go Live (~450 lines)

**What:** Manage the Go Live voice WebSocket and media sending.

| File | Lines | Purpose |
|------|-------|---------|
| `stream_connection.py` | 200 | Go Live WS: CONNECT → READY → SELECT_PROTOCOL → SESSION_DESCRIPTION → DAVE |
| `voice_send.py` | 150 | VoiceStreamClient extending discord.VoiceClient (or standalone) |
| `streamer.py` | 250 | Main API: join_voice, start_go_live, play, stop |

**Key invariants for Go Live:**
- Separate voice WebSocket from main voice connection
- Separate SSRCs (from stream server's READY)
- Separate secret key (from stream server's SESSION_DESCRIPTION)
- Separate DAVE session (may differ from main voice)
- Speaking mode = 2 (priority/soundshare, not 1)
- serverId = guild_id (guild) or channel_id (DM)
- daveChannelId = BigInt(serverId) - 1

**Dependencies:** websockets (Go Live WS), discord.py/dpy-self

**Validation gate:** Connect to stream server, receive READY + SESSION_DESCRIPTION.

---

### Phase 6: Integration + Full Pipeline (~200 lines)

**What:** Wire everything together, send real video to Discord.

| File | Lines | Purpose |
|------|-------|---------|
| `__init__.py` | 20 | Package exports |
| `compat/voice_recv.py` | 80 | Optional integration with discord-ext-voice-recv |
| `tests/test_integration.py` | 100 | End-to-end test: join → Go Live → play video → viewer sees it |

**Full pipeline flow:**
```
FFmpeg → NUT demux → H.264 frame
                      ↓
              SPS VUI rewrite (if SPS found)
                      ↓
              DAVE encrypt (dave_session.encrypt(video, h264, frame))
                      ↓
              RTP packetize (NALU split → FU-A)
                      ↓
              Transport encrypt (AEAD encrypt with stream secret key)
                      ↓
              UDP sendto (stream socket)
```

**Dependencies:** All of the above

**Validation gate:** Viewer in Discord sees video from Go Live.

---

## File Map (Final)

```
discord_video_stream/
    __init__.py                  # ~20 lines
    streamer.py                  # ~250 lines — Main API
    voice_send.py                # ~150 lines — Send-side VoiceClient extension
    stream_connection.py         # ~200 lines — Go Live WS + media
    rtp/
        __init__.py              # ~10 lines
        serialize.py             # ~120 lines — RTP/RTCP packet builder
        h264.py                  # ~200 lines — NALU + FU-A/STAP-A
        crypto.py                # ~80 lines  — Transport encryption (encrypt)
    media/
        __init__.py              # ~10 lines
        ffmpeg.py                # ~150 lines — FFmpeg subprocess
        demux.py                 # ~180 lines — NUT demuxing (PyAV)
        pacer.py                 # ~150 lines — Frame pacing + A/V sync
    protocol/
        __init__.py              # ~5 lines
        types.py                 # ~100 lines — Codec config, stream key
        vui.py                   # ~200 lines — SPS VUI rewriter
    compat/
        __init__.py              # ~5 lines
        voice_recv.py            # ~80 lines  — Optional voice-recv integration
    tests/
        test_rtp.py
        test_h264.py
        test_crypto.py
        test_ffmpeg.py
        test_pacer.py
        test_integration.py
TOTAL: ~1700 lines (excluding tests)
```

---

## Dependencies

| Package | Purpose | Source |
|---------|---------|--------|
| `discord.py-self[voice]` >=2.1 | Gateway, voice WS, DAVE (davey), UDP socket | pip |
| `websockets` >=12.0 | Go Live voice WS (separate from discord.py's WS) | pip |
| `PyAV` >=12.0 | NUT demuxing from FFmpeg pipe | pip |
| `pynacl` >=1.5 | Transport encryption (AEAD) | pip (already required by dpy-self) |
| FFmpeg | Video/audio transcoding | system |

**NOT needed:**
- `dave.py` by DisnakeDev (dpy-self uses davey instead)
- WebRTC library (we use UDP path directly)
- Separate DAVE implementation (davey handles everything)

---

## Risk Register

| Risk | Severity | Mitigation |
|------|----------|------------|
| PyAV NUT pipe latency | Medium | Raw Annex-B fallback (two separate pipes, -f h264 + -f data) |
| DAVE channel ID mapping for DM Go Live | Low | daveChannelId = BigInt(serverId) - 1 validated in Node.js source |
| Stream connection shares UDP socket | Low | Both go through `self.socket.sendto()` — discord.py handles this |
| SPS VUI rewriter complexity | Low | Well-documented port from C++/TS, pure bitstream manipulation |
| Discord detecting selfbot patterns | Unknown | Use same codec PTs, speaking modes, and timing as Node.js (proven working) |

---

## Execution Order

```
Phase 1 (RTP + Crypto) ──→ Phase 2 (FFmpeg) ──→ Phase 3 (Pacing) ──→ Phase 5 (Stream Connection) ──→ Phase 6 (Integration)
                                                   │                        ↑
                                                   └── Phase 4 (Protocol) ─┘
```

Phases 1-4 can be partially parallelized. Phase 5 requires Phases 1+4. Phase 6 requires everything.

**Estimated total effort:** ~1700 lines of new code + ~300 lines of tests = ~2000 lines.

---

## What discord-ext-voice-recv (dm-voice branch) Gives Us

1. **Architecture pattern** — AudioSink ↔ AudioSource duality teaches us how to structure VideoSource/Sink
2. **RTP header format** — `_hstruct = struct.Struct('>xxHII')` reused for serialization
3. **PacketDecryptor** — Reverse-engineered to create PacketEncryptor (same AEAD modes)
4. **Gateway hook pattern** — We extend the same hook function to handle VIDEO opcode send
5. **SSRC tracking** — Bidirectional SSRC↔user mapping in VoiceRecvClient
6. **DM voice fixes** — `vc.guild.get_member(uid) if vc.guild else vc.client.get_user(uid)` pattern
7. **Utility functions** — `gap_wrapped()`, `add_wrapped()`, `Bidict` for our own needs
8. **Type definitions** — `VoiceVideoPayload`, `VideoStream` TypedDicts for building VIDEO opcode payloads

---

## Next Steps (Per Phase)

1. **Start Phase 1 immediately** — RTP serialization + transport crypto. Pure Python, no external dependencies beyond pynacl. Can be unit tested in isolation.
2. **Phase 2 in parallel** — FFmpeg command construction + NUT demuxing. Requires FFmpeg installed.
3. **Phase 4 in parallel** — SPS VUI rewriter. Pure bitstream math, independent.
4. **Phase 3 after Phase 2** — Frame pacer needs real frame timestamps from demuxer.
5. **Phase 5 after Phases 1+4** — Stream connection needs RTP (to send) and protocol types (to talk to voice server).
6. **Phase 6 last** — Integration testing with real Discord servers.
