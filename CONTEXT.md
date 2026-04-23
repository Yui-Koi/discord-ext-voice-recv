# Context: Exhaustive Protocol Research and Domain Knowledge

This document captures all protocol research, domain-specific knowledge, reference analysis, and open research areas discovered during the development of the Python video stream port. It serves as the canonical reference for protocol details, implementation constraints, and architectural decisions.

---

## 1. Voice WebSocket Protocol

### 1.1 Connection Flow

The voice WebSocket connection follows this sequence:

1. Client connects to `wss://{endpoint}/?v=9` (v9 recommended, v8 also works)
2. Server sends HELLO (opcode 8) with `heartbeat_interval` (typically 41250ms)
3. Client sends IDENTIFY (opcode 0) with credentials
4. Server sends READY (opcode 2) with SSRCs, IP/port, encryption modes, streams
5. Client sends SELECT_PROTOCOL (opcode 1) with transport choice and codecs
6. Server sends SELECT_PROTOCOL_ACK (opcode 4) with secret key and DAVE version
7. DAVE key exchange (if `dave_protocol_version > 0`)
8. Client sends VIDEO (opcode 12) to enable video
9. Client sends SPEAKING (opcode 5) with appropriate mode
10. Media flows

### 1.2 Version Differences

| Version | Changes |
|---------|---------|
| v9 (recommended) | Added `channel_id` to IDENTIFY (opcode 0) and RESUME (opcode 7) |
| v8 | Added buffered resuming |
| v7 | Added Channel Options Update (opcode 17) |
| v6 | Added Voice Backend Version (opcode 16) |
| v5 | Added Media Sink Wants (opcode 15), video support |
| v4 | Changed speaking status from boolean to bitmask |
| v3 | Added video, consolidated HELLO payload |

The Python port uses v9. The Node.js reference uses v8. Both work; v9 is preferred per Discord docs.

### 1.3 SELECT_PROTOCOL: UDP vs WebRTC

Two transport protocols are available:

**UDP** (used by Python port):
- `data.address` and `data.port` can be randomized for send-only connections
- Response contains `secret_key` (32 bytes), `mode` (encryption mode string), `dave_protocol_version`
- RTP packets are sent directly over UDP with AEAD transport encryption

**WebRTC** (used by Node.js reference):
- `data` contains an SDP offer string
- Response contains an SDP answer string with ICE credentials, DTLS fingerprint, candidates
- Uses DTLS-SRTP for encryption (handled by WebRTC library)

The Python port uses UDP because it avoids the WebRTC dependency chain. The Node.js reference uses WebRTC because `node-datachannel` provides built-in RTP packetization.

### 1.4 Codec Payload Types

The Node.js reference uses hardcoded payload types. The Discord docs say clients can specify their own PTs, but the working reference uses fixed values:

- Opus: PT 120, clock rate 48000
- H.264: PT 101, RTX PT 102, clock rate 90000
- H.265: PT 103, RTX PT 104, clock rate 90000
- VP8: PT 105, RTX PT 106, clock rate 90000
- VP9: PT 107, RTX PT 108, clock rate 90000
- AV1: PT 109, RTX PT 110, clock rate 90000

No payload type should be set to 96 (reserved for probe packets).

### 1.5 Transport Encryption

Supported modes (in order of preference):

1. `aead_aes256_gcm_rtpsize` -- Preferred when hardware AES-NI is available
2. `aead_xchacha20_poly1305_rtpsize` -- Required, always available

Both use the same wire format: `[12-byte RTP header (plaintext/AAD)][encrypted payload][4-byte nonce counter]`

The nonce is a 24-byte value: 4-byte BE counter + 20 zero bytes (XChaCha20) or 8 zero bytes (AES-256-GCM, total 12 bytes). The 4-byte nonce counter is appended to the encrypted payload.

The AAD (Additional Authenticated Data) is the 12-byte RTP header. For `rtpsize` variants, the AAD includes CSRCs and the extension preamble if present.

Counter wraps at 2^32. Each packet gets a unique incrementing nonce.

### 1.6 Speaking Flags

Speaking is a bitmask since voice gateway v4:

- 0: Not speaking
- 1: Normal speaking (voice activity)
- 2: Priority speaker / Soundshare (used for Go Live)
- 4: Priority speaker (some sources)

Go Live uses mode 2. Camera streams use mode 1.

---

## 2. Go Live Protocol

### 2.1 Stream Lifecycle

Go Live streams have a separate lifecycle from the main voice connection:

1. Main voice connection is established (voice WS + UDP socket)
2. Gateway sends STREAM_CREATE (opcode 18): `{type, guild_id, channel_id, preferred_region: null}`
3. Gateway responds with STREAM_CREATE event: `{stream_key, rtc_server_id}`
4. Gateway responds with STREAM_SERVER_UPDATE event: `{stream_key, endpoint, token}`
5. Stream voice WebSocket connects to `wss://{endpoint}/?v=9`
6. Stream WS handshake: IDENTIFY -> READY -> SELECT_PROTOCOL -> SESSION_DESCRIPTION
7. DAVE key exchange for stream
8. VIDEO opcode sent with stream SSRCs
9. SPEAKING opcode sent with mode=2
10. Media flows through stream connection
11. STREAM_DELETE (opcode 19) to end stream

### 2.2 Stream Key Format

Stream keys identify Go Live streams:

- Guild: `guild:{guild_id}:{channel_id}:{user_id}`
- Call/DM: `call:{channel_id}:{user_id}`

### 2.3 Separate Connection Parameters

The stream connection has its own:

- Voice WebSocket (separate URL, separate heartbeat)
- SSRCs (from stream READY: audio_ssrc, video_ssrc, rtx_ssrc)
- Secret key (from stream SESSION_DESCRIPTION)
- DAVE session (separate `daveChannelId = serverId - 1`)
- Encryption mode (may differ from main voice)

The UDP socket is shared (same local socket, different destination IP:port).

### 2.4 daveChannelId Computation

For the stream connection: `daveChannelId = BigInt(serverId) - 1`
For the main voice connection: `daveChannelId = channelId`

Where `serverId` = `guildId` for guild channels, `channelId` for DM/call.

---

## 3. DAVE Protocol

### 3.1 Protocol Overview

DAVE (Discord Audio & Video End-to-End Encryption) uses MLS (Messaging Layer Security) for group key exchange. Protocol version 1.1.

Key properties:
- Frame-level encryption: complete encoded frames are encrypted BEFORE RTP packetization
- Per-sender keys: each sender has a unique ratcheted symmetric key from the MLS group
- External sender: the voice server acts as an MLS external sender for member management
- Forward secrecy: when a member leaves, new keys are generated

### 3.2 MLS Group Establishment

1. Client sends MLS_KEY_PACKAGE (binary opcode 26) with serialized key package
2. Server sends MLS_EXTERNAL_SENDER (binary opcode 25) with external sender data
3. Server sends MLS_PROPOSALS (binary opcode 27) with Add proposals
4. Client processes proposals, generates commit + optional welcome
5. Client sends MLS_COMMIT_WELCOME (binary opcode 28)
6. Server picks "winning" commit, broadcasts MLS_ANNOUNCE_COMMIT_TRANSITION (binary opcode 29)
7. New members receive MLS_WELCOME (binary opcode 30)
8. All clients send DAVE_TRANSITION_READY (opcode 23)
9. Server sends DAVE_EXECUTE_TRANSITION (opcode 22)
10. Frames are now encrypted with new key ratchet

### 3.3 Protocol Transitions

- DAVE_PREPARE_TRANSITION (opcode 21): Server announces version change
- DAVE_EXECUTE_TRANSITION (opcode 22): Clients switch to new version
- DAVE_TRANSITION_READY (opcode 23): Client reports readiness
- DAVE_PREPARE_EPOCH (opcode 24): MLS epoch change
- MLS_INVALID_COMMIT_WELCOME (opcode 31): Client reports unprocessable commit

Transition ID 0 executes immediately. Non-zero transition IDs wait for all clients.

### 3.4 Passthrough Mode

When `dave_protocol_version` is 0 (no DAVE) or during transitions, frames pass through unencrypted. `dave_session.set_passthrough_mode(True, timeout_frames)` enables this.

### 3.5 Frame Encryption Order

For video:
1. FFmpeg outputs complete H.264 frame (Annex-B)
2. SPS VUI rewrite (if SPS NALU found)
3. DAVE encrypt: `dave_session.encrypt(MediaType.video, Codec.h264, frame)`
4. RTP packetize: NALU split -> FU-A fragmentation
5. Transport encrypt: AEAD encrypt each RTP packet
6. UDP send

For audio:
1. FFmpeg outputs Opus frame
2. DAVE encrypt: `dave_session.encrypt_opus(frame)`
3. Build RTP packet (single packet, no fragmentation)
4. Transport encrypt
5. UDP send

---

## 4. H.264 RTP Packetization

### 4.1 NAL Unit Types

- Type 0: Unspecified
- Type 1: Coded slice of non-IDR picture (P-frame)
- Type 2-4: Coded slice partition A/B/C
- Type 5: Coded slice of IDR picture (keyframe)
- Type 6: Supplemental Enhancement Information (SEI)
- Type 7: Sequence Parameter Set (SPS)
- Type 8: Picture Parameter Set (PPS)
- Type 9: Access Unit Delimiter (AUD)

### 4.2 Single NAL Unit Packet

When a NALU fits in one RTP packet (<= 1300 bytes payload):
- RTP header (12 bytes) + NALU data (no start code prefix)

### 4.3 FU-A Fragmentation

When a NALU exceeds the MTU:

FU indicator byte: `(nalu[0] & 0x60) | 28`
- Bits 7-6: NRI from original NAL header
- Bits 5-0: Type 28 (FU-A)

FU header byte:
- Bit 7: Start bit (1 for first fragment)
- Bit 6: End bit (1 for last fragment)
- Bits 4-0: Original NAL unit type

Maximum fragment payload: 1298 bytes (1300 - 2 for FU indicator and header).

### 4.4 Marker Bit

The marker bit MUST be set on the last RTP packet of each access unit (frame). For multi-NALU frames, only the very last packet of the entire frame gets the marker bit.

### 4.5 Clock Rates

- Video: 90 kHz (90000 ticks per second)
- Audio (Opus): 48 kHz (48000 ticks per second)

PTS conversion: `rtp_timestamp = int(pts_ms * clock_rate / 1000) & 0xFFFFFFFF`

---

## 5. SPS VUI Rewriting

### 5.1 Purpose

Discord's WebRTC decoder (based on Chromium's) requires `bitstream_restriction` to be present in the H.264 SPS. Without it, the decoder may buffer frames incorrectly or reject the stream.

### 5.2 Changes Applied

- Force `bitstream_restriction_flag = 1`
- Force `max_num_reorder_frames = 0` (no B-frame reordering)
- Set `max_dec_frame_buffering = max_num_ref_frames`
- Strip `video_signal_type` information (set present flag to 0)

### 5.3 When It Runs

Only for H.264 video. On every frame that contains an SPS NALU (type 7). Other NALUs pass through unchanged.

### 5.4 Derivation

Ported from WebRTC's C++ `sps_vui_rewriter.cc` (via the TypeScript port in the Node.js reference). The bitstream reader/writer handles Exp-Golomb coding and emulation prevention byte skipping.

---

## 6. FFmpeg Pipeline

### 6.1 Critical Flags

- `-bf 0`: No B-frames (essential for low latency)
- `-preset superfast`: NOT ultrafast (causes bitrate spikes)
- `-tune film`: Optimizes for live-action content
- `-forced-idr 1`: Every keyframe is IDR (decoder recovery)
- `-force_key_frames expr:gte(t,n_forced*1)`: Keyframe every 1 second
- `-pix_fmt yuv420p`: Only 4:2:0 chroma supported
- `-bufsize:v {bitrate/2}k`: VBV buffer at half average bitrate
- `-f nut pipe:1`: NUT container to stdout

### 6.2 Input Handling

URLs with `lavfi:` prefix are converted to `-f lavfi -i <filter>` format. FFmpeg does not accept `lavfi:` as a URL protocol.

### 6.3 Audio Configuration

- Codec: libopus
- Channels: 2 (stereo)
- Sample rate: 48000 Hz
- Bitrate: configurable (default 128 kbps)

### 6.4 Bitstream Filters (Node.js only)

The Node.js reference applies bitstream filters for H.264 input:
- `h264_mp4toannexb`: AVCC to Annex-B conversion
- `h264_metadata` with `aud: "remove"`: Strip AUD NALUs
- `dump_extra`: Ensure SPS/PPS extradata

The Python port does not need these because FFmpeg's `-f nut` output already provides Annex-B format.

---

## 7. NUT Container

### 7.1 Why NUT

NUT was chosen over Matroska for:
- Lower framing overhead (reduced initial buffering delay)
- No seek table or index (streaming use case)
- Simple codec ID signaling
- Designed for real-time pipe reading

### 7.2 PyAV Integration

PyAV reads NUT from a synchronous file-like object. The Python port duplicates the asyncio pipe fd and sets it to blocking mode via `os.dup()` + `fcntl.fcntl()`. This blocks the event loop during PyAV reads, mitigated by `asyncio.sleep(0)` every 8 frames.

### 7.3 Keyframe Detection

PyAV's `packet.is_keyframe` is the primary keyframe indicator. A fallback scans for IDR NALUs (type 5) in the Annex-B data using `_contains_idr_nalu()`.

---

## 8. Open Research Areas

### 8.1 Raw Annex-B Pipe Alternative

Instead of NUT + PyAV, FFmpeg could output raw H.264 Annex-B to `-f h264 pipe:3` and raw Opus to `-f data -c:a copy pipe:4`. This would eliminate PyAV as a dependency and avoid the synchronous API issue. The challenge is delimiting Opus frames without container framing (requires TOC byte parsing).

### 8.2 AES-256-GCM vs XChaCha20 Performance

The Discord docs recommend AES-256-GCM when available (hardware acceleration). pynacl provides XChaCha20-Poly1305 via `secret.Aead`. Python's `cryptography` library provides AES-256-GCM. The performance difference in Python has not been benchmarked for typical RTP packet sizes (500-1300 bytes).

### 8.3 Multiple Simulcast Streams

Discord supports multiple simulcast quality levels. The reference implementation sends only one stream (rid "100", quality 100). Sending multiple streams (e.g., rid "50" + rid "100") may provide adaptive quality for viewers with limited bandwidth, but increases encoding and sending overhead.

### 8.4 Stage Channel Specifics

Stage channels are exempt from DAVE requirements. They also require `setSuppressed(false)` before the bot can speak. The interaction between Go Live and stage channels has not been fully investigated.

### 8.5 Stream Preview

The stream preview feature decodes keyframes, resizes to 1024x576, converts to JPEG, and uploads via REST API. This is relatively expensive and disabled by default in both Node.js and Python implementations.

### 8.6 Selfbot Detection Vectors

The technical detection vectors for selfbot video streaming are not fully understood. Using identical codec PTs, speaking modes, and timing as the Node.js reference (which is proven working) is the safest approach.
