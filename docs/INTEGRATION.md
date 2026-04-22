# discord-ext-voice-recv Integration Analysis

## What Exists and What We Can Reuse

### 1. discord.py's VoiceClient (already handles connection lifecycle)

discord.py's `VoiceClient` and `VoiceConnectionState` already handle:
- Gateway VOICE_STATE_UPDATE / VOICE_SERVER_UPDATE collection
- Voice WebSocket connection, IDENTIFY, READY, SELECT_PROTOCOL, SESSION_DESCRIPTION
- Heartbeat management
- UDP socket creation and IP discovery
- Secret key storage from SESSION_DESCRIPTION
- DAVE session management (`davey.DaveSession` integration)
- DAVE opcode handling (MLS_EXTERNAL_SENDER, MLS_PROPOSALS, MLS_ANNOUNCE_COMMIT_TRANSITION, MLS_WELCOME, DAVE_PREPARE_TRANSITION, DAVE_EXECUTE_TRANSITION, DAVE_PREPARE_EPOCH)
- Socket reader thread for receiving UDP packets
- Reconnection and resume logic

**Key insight:** discord.py v2.7+ already requires `davey` (the Node.js DAVE library's Python counterpart by Rapptz, NOT `dave.py` by DisnakeDev). The `VoiceConnectionState` has full DAVE integration including `dave_session`, `dave_protocol_version`, `dave_pending_transitions`, `dave_downgraded`, and the entire MLS opcode flow.

**We do NOT need to reimplement the voice WebSocket protocol or DAVE.** discord.py handles all of this when you call `channel.connect()`. What we need to build is the SENDING side.

### 2. discord-ext-voice-recv (already handles receiving)

The receive library provides:
- `VoiceRecvClient` extending `discord.VoiceClient`
- `AudioReader` with `PacketDecryptor` (4 encryption modes)
- `PacketRouter` with jitter buffering
- `RTPPacket` / `RTCPPacket` parsing
- SSRC <-> user ID mapping
- Speaking detection
- Event dispatching
- DM voice support via `patches.py`

### 3. What We Need to Build (send side only)

The gap is entirely on the send side. Here is what discord.py and voice-recv provide vs what we need:

| Component | Provided By | We Need |
|---|---|---|
| Gateway connection | discord.py | Nothing |
| Voice WS handshake | discord.py | Nothing (extend VoiceClient) |
| DAVE key exchange | discord.py (davey) | Nothing |
| UDP socket | discord.py (`self.socket`) | Nothing (use existing) |
| Secret key | discord.py (`self.secret_key`) | Nothing (use existing) |
| SSRC assignment | discord.py (`self.ssrc`) | Nothing (use existing) |
| Transport encryption | discord.py provides modes | **Need encrypt function** (recv only has decrypt) |
| RTP packet construction | voice-recv has parse only | **Need build** |
| H.264 NALU processing | Not present | **Need build** |
| Opus frame handling | discord.py encoder exists | **Need frame extraction from FFmpeg** |
| Frame pacing | Not present | **Need build** |
| FFmpeg subprocess | Not present (sinks use it for receive) | **Need build** (for transcoding) |
| Video attributes opcode | Not present | **Need build** (send VIDEO opcode 12) |
| Stream lifecycle | Not present | **Need build** (STREAM_CREATE/DELETE) |
| Speaking opcode | discord.py has it | **Use existing** |
| Go Live connection | Not present | **Need build** (separate StreamConnection) |

### 4. Specific Reusable Components

#### From discord-ext-voice-recv `rtp.py`:
- `RTPPacket` class -- We can reference its header format for SERIALIZATION (reverse direction)
- `_hstruct = struct.Struct('>xxHII')` -- Header packing: skip 2 bytes, then sequence(H), timestamp(I), ssrc(I)
- `is_rtcp()` -- Reuse as-is
- `decode()`, `decode_rtp()`, `decode_rtcp()` -- Keep for receive, but we need the inverse
- Extension header parsing (`_parse_bede_header`, `update_ext_headers`) -- Reference for building extension headers
- `OPUS_SILENCE = b'\xf8\xff\xfe'` -- Reuse for silence detection

#### From discord-ext-voice-recv `reader.py`:
- `PacketDecryptor` class -- The encrypt side mirrors this. Same modes, same nonce format, just encrypt instead of decrypt
- For `aead_xchacha20_poly1305_rtpsize`: the decrypt uses `nacl.secret.Aead(key)`, we just call `.encrypt()` instead of `.decrypt()`
- Nonce format: 4-byte incremental counter appended to payload
- AAD: RTP header bytes

#### From discord-ext-voice-recv `voice_client.py`:
- `VoiceRecvClient` -- We create a parallel `VoiceSendClient` or extend `VoiceRecvClient`
- SSRC tracking (`_ssrc_to_id`, `_id_to_ssrc`) -- Reuse
- Gateway hook integration -- Extend
- `create_connection_state()` -- Override to use custom `VoiceConnectionState` subclass

#### From discord-ext-voice-recv `gateway.py`:
- `hook()` function -- The opcode dispatch logic. We extend this to handle our send-side events (VIDEO opcode with our own SSRCs, etc.)
- Opcode constants -- Reuse (already imported)

#### From discord-ext-voice-recv `utils.py`:
- `gap_wrapped()`, `add_wrapped()` -- Reuse for sequence/timestamp wrap-around
- `Bidict` -- Reuse for bidirectional SSRC mapping
- `LoopTimer` -- Could reuse for frame pacing

#### From discord-ext-voice-recv `types.py`:
- `VoiceVideoPayload`, `VideoStream`, `VideoResolution` -- Reuse for VIDEO opcode construction

### 5. Architecture Decision: Extend vs Separate

**Option A: Extend discord-ext-voice-recv (minimal changes)**

Create a new class `VoiceSendRecvClient(VoiceRecvClient)` that adds send capability:
- Adds `send_video()`, `send_audio()` methods
- Adds frame pacer, RTP packetizer, FFmpeg pipeline
- Adds Go Live stream management
- Shares SSRC tracking, gateway hooks, socket with recv side
- Integration point: the same UDP socket is used for both send and receive

Pros: Single client for bidirectional, shared state, minimal duplication
Cons: Tight coupling to voice-recv internals

**Option B: Separate package that hooks into discord.py (reference voice-recv)**

Create `discord-video-stream-python` as an independent package:
- Implements its own `VoiceStreamClient(VoiceClient)` (not extending VoiceRecvClient)
- Can optionally integrate with voice-recv if both are installed
- Shares the discord.py socket and DAVE session
- Reimplements the parts it needs (RTP construction, crypto, pacing)

Pros: Clean separation, no coupling to voice-recv internals
Cons: Some duplication (SSRC tracking, gateway hooks)

**Recommendation: Option B (separate package)** with an optional integration module for voice-recv.

Rationale:
- voice-recv is receive-only and our send side doesn't need most of its abstractions (sinks, routers, jitter buffers)
- The shared surface is small: SSRC tracking, gateway hooks, UDP socket, DAVE session
- discord.py's `VoiceClient` already provides the socket, secret key, DAVE session, and WS connection
- We only need to add: RTP construction, H.264 packetization, frame pacing, FFmpeg, transport encryption (encrypt side)
- If voice-recv is installed, we can optionally expose a unified client

### 6. What discord.py's VoiceClient Actually Gives Us

From the source code analysis, `VoiceClient` provides:

```python
# Connection state (from VoiceConnectionState)
self.socket              # UDP socket, already connected to voice server
self.secret_key          # 32-byte key from SESSION_DESCRIPTION
self.ssrc                # Our assigned SSRC
self.mode                # Encryption mode string (e.g., "aead_xchacha20_poly1305_rtpsize")
self.session_id          # Session ID
self.token               # Voice token
self.endpoint            # Voice server endpoint
self.ws                  # Voice WebSocket (DiscordVoiceWebSocket)
self._connection         # VoiceConnectionState (has DAVE session, etc.)

# Methods
self.checked_add(attr, value, limit)  # Wrapping integer addition
self.sequence            # RTP sequence number (managed by discord.py for audio send)
self.timestamp           # RTP timestamp (managed by discord.py for audio send)
self._incr_nonce         # Incremental nonce counter for transport encryption
```

**Critical observation:** discord.py already manages `self.sequence`, `self.timestamp`, and `self._incr_nonce` for audio sending. For video, we need our OWN sequence number, timestamp, and nonce counter because video uses a different SSRC and different RTP stream.

**Critical observation:** `self.socket` is the shared UDP socket. Both send and receive use it. For video sending, we just call `self.socket.sendto(encrypted_packet, (endpoint_ip, voice_port))`.

**Critical observation:** discord.py's `VoiceConnectionState` already handles the DAVE opcode flow including MLS key exchange, transitions, and key ratchet management. We access the DAVE session via `self._connection.dave_session` and use it to encrypt our video frames.

### 7. The Encryption Side (what we add)

discord.py uses `davey` (NOT `dave.py` by DisnakeDev). It wraps `davey.DaveSession`. The encrypt side:

```python
# discord.py already does this for audio. We need the equivalent for video.
# From VoiceConnectionState:
self.dave_session.encrypt_opus(frame)  # For audio
self.dave_session.encrypt(media_type, codec, frame)  # For video (generic)
```

For transport encryption:
```python
# From discord.py's voice sending code:
nacl.secret.Aead(secret_key).encrypt(
    plaintext=rtp_payload,
    aad=rtp_header,
    nonce=nonce  # 24 bytes: 4-byte counter + 20 zero padding
)
# Output: encrypted_payload + 4_byte_nonce_counter
```

The `self._incr_nonce` counter is already managed by discord.py for audio. For video, we need our own counter.

### 8. Revised File Structure

```
discord_video_stream/
    __init__.py              # Exports
    streamer.py              # Main API: join_voice, create_stream, play_stream
    voice_send.py            # VoiceStreamClient(VoiceClient) -- our send-side client
    stream_connection.py     # Go Live stream connection management
    rtp/
        __init__.py
        h264.py              # H.264 NALU splitting, FU-A/STAP-A packetization
        opus.py              # Opus frame extraction
        crypto.py            # Transport encryption (encrypt side of nacl AEAD)
    media/
        __init__.py
        ffmpeg.py            # FFmpeg subprocess, command construction
        demuxer.py           # NUT/Annex-B parsing (PyAV or raw)
        pacer.py             # Frame pacing, A-V sync
    protocol/
        __init__.py
        opcodes.py           # Gateway opcodes for stream lifecycle
        types.py             # Stream key, video attributes, codec config
        vui.py               # H.264 SPS VUI rewriter
    compat/
        __init__.py
        voice_recv.py        # Optional integration with discord-ext-voice-recv
```

Estimated new code: ~2000 lines (down from 2650 because we reuse discord.py's connection/DAVE)

### 9. What We Do NOT Need to Build

- Voice WebSocket connection -- discord.py handles it
- DAVE/MLS key exchange -- discord.py handles it (via davey)
- UDP socket creation -- discord.py provides `self.socket`
- IP discovery -- discord.py handles it (or we randomize for send-only)
- Heartbeat -- discord.py handles it
- Reconnection/resume -- discord.py handles it
- Gateway VOICE_STATE_UPDATE/VOICE_SERVER_UPDATE -- discord.py handles it
- Secret key management -- discord.py provides it
- SSRC assignment -- discord.py provides it from READY payload

### 10. What We DO Need to Build

- RTP packet construction (header + payload serialization)
- H.264 NALU splitting and FU-A/STAP-A packetization
- Opus frame extraction from FFmpeg output
- Transport encryption (encrypt direction of AEAD)
- DAVE frame encryption (call `dave_session.encrypt()` for video frames)
- FFmpeg subprocess management
- NUT demuxing or raw Annex-B parsing
- Frame pacing with A/V sync
- VIDEO opcode sending (video attributes, our SSRCs)
- STREAM_CREATE/DELETE gateway opcodes for Go Live
- Go Live StreamConnection (separate voice WS, separate SSRCs, separate DAVE channel)
- SPS VUI rewriting (port from TypeScript)

### 11. Integration Points with voice-recv

If voice-recv is installed alongside our package:
- Both share the same `self.socket` via discord.py's VoiceClient
- Both share the same DAVE session
- Both share the same SSRC <-> user mapping
- voice-recv's `VoiceRecvClient` handles receive, our client handles send
- They can coexist: extend `VoiceRecvClient` to add send capability, or use both independently

The `compat/voice_recv.py` module would provide:
- `VoiceSendRecvClient(VoiceRecvClient)` that adds send methods
- Shared SSRC tracking
- Unified lifecycle management
