# Context Document: Discord Video Stream Python Port

## Exhaustive Research Log and Domain Knowledge

This document captures all exploration, findings, inferences, resources, and open research areas discovered during analysis of the discord-video-stream Node.js library, the discord-ext-voice-recv Python library, and the surrounding Discord voice/video protocol ecosystem. It is intended as a reference for any agent continuing this work.

---

## 1. Repositories Analyzed

### 1.1 @dank074/discord-video-stream (Node.js, v6.0.0)

Repository: https://github.com/Discord-RE/Discord-video-stream
Cloned to: /root/.openclaw/workspace/discord-video-stream
50 files, 32 TypeScript source files, 4319 lines in src/

This is the only mature, working implementation of video streaming for Discord via a selfbot. It is a fork/evolution of mrjvs/Discord-video-experiment which was the original proof-of-concept from the CascadeBot team. The original PoC demonstrated that VP8 video could be sent in voice channels but Discord patched the client to no longer render it. The current library uses WebRTC (via node-datachannel) instead of raw UDP, supports Go Live and camera streams, and implements DAVE/E2EE via @snazzah/davey.

### 1.2 discord-ext-voice-recv (Python, Yui-Koi fork, dm-voice branch)

Repository: https://github.com/Yui-Koi/discord-ext-voice-recv
Cloned to: /root/.openclaw/workspace/discord-ext-voice-recv
25 files, 19 Python source files, 3403 lines

This is a receive-only extension for discord.py. The dm-voice branch (compared to main) adds approximately 135 lines across 13 files, consisting entirely of DM/group voice context fixes: replacing guild-dependent lookups (guild.me.id, guild.get_member) with fallbacks to user.id and client.get_user for non-guild contexts. The changes are cosmetic and do not alter the architecture.

### 1.3 discord.py (VoiceClient, VoiceConnectionState)

Analyzed via GitHub raw source at https://github.com/Rapptz/discord.py/master/discord/
Key files: voice_client.py, voice_state.py, gateway.py (voice WebSocket)

discord.py v2.7+ has complete DAVE integration. It depends on `davey` (Snazzah's Rust DAVE implementation compiled to Python via maturin, available on PyPI at version 0.1.5, uploaded March 29, 2026). The `VoiceClient.__init__` explicitly checks `if not has_dave: raise RuntimeError('davey library needed in order to use voice')`. This means discord.py 2.7+ REQUIRES davey for voice functionality.

### 1.4 Snazzah/davey (Rust DAVE implementation with Python bindings)

Repository: https://github.com/Snazzah/davey
PyPI: https://pypi.org/project/davey/ (version 0.1.5)
Python typings: davey-python/davey.pyi in the repository

This is NOT the same as DisnakeDev/dave.py. Davey is Snazzah's independent Rust implementation using OpenMLS. Dave.py wraps Discord's official libdave C++ library. Both implement the DAVE protocol but are different codebases. discord.py uses davey.

The DaveSession API (from davey.pyi typings):
- __init__(protocol_version, user_id, channel_id, key_pair?)
- reset() -- deletes group, clears storage
- reinit(protocol_version, user_id, channel_id, key_pair?) -- reset + reinit
- set_external_sender(external_sender_data: bytes)
- get_serialized_key_package() -> bytes -- single-use, regenerated each call
- process_proposals(operation_type: ProposalsOperationType, proposals: bytes, expected_user_ids?: List[int]) -> Optional[CommitWelcome]
- process_welcome(welcome: bytes) -- raises on failure
- process_commit(commit: bytes) -- raises on failure
- encrypt(media_type: MediaType, codec: Codec, packet: bytes) -> bytes -- THE KEY METHOD for video
- encrypt_opus(packet: bytes) -> bytes -- convenience for audio
- ready: bool
- status: SessionStatus (inactive/pending/awaiting_response/active)
- voice_privacy_code: Optional[str]
- epoch: Optional[int]

The ProposalsOperationType enum has values append=0 and revoke=1. This differs from the Node.js davey which passes the operation type as a raw byte.

The MediaType enum has audio=0 and video=1.
The Codec enum has unknown=0, opus=1, vp8=2, vp9=3, h264=4, h265=5, av1=6.

### 1.5 Discord Voice Connection Documentation

Source: https://docs.discord.food/topics/voice-connections (unofficial but comprehensive)

This documentation confirms:
- Voice gateway v9 is now recommended (not v8). v9 adds channel_id to Opcode 0 Identify and Opcode 7 Resume.
- Two transport modes: UDP and WebRTC. WebRTC is described as the alternative for clients that cannot support UDP.
- For send-only connections, the address and port in UDP protocol data can be randomized.
- DAVE is mandatory since March 1, 2026. Clients without DAVE are disconnected with close code 4017.
- Stage channels are exempt from DAVE requirements.
- Binary WebSocket messages for DAVE opcodes use format: [2-byte sequence BE][1-byte opcode][variable payload] for server-to-client, and [1-byte opcode][variable payload] for client-to-server.
- Codec payload types are client-specified in the SELECT_PROTOCOL codecs array. The docs example shows AV1 at PT 101/102 and H264 at PT 103/104. However, the working Node.js library uses different fixed PTs.
- No payload type should be set to 96 (reserved for probe packets).
- The supported encryption modes in order of preference: aead_aes256_gcm_rtpsize (preferred), aead_xchacha20_poly1305_rtpsize (required), plus several deprecated modes.

### 1.6 Discord DAVE Protocol Whitepaper

Source: https://daveprotocol.com/
Protocol version: 1.1

Key details confirmed:
- DAVE operates at the frame level: the complete encoded frame is encrypted BEFORE RTP packetization.
- Each sender has a unique ratcheted symmetric key derived from the MLS group.
- The voice server acts as an external sender in the MLS group, sending Add/Remove proposals.
- Only proposals from the external sender should be processed.
- Key packages are single-use.
- Protocol transitions are coordinated by the gateway using transition IDs.
- The initial group (epoch=1) allows any pending member to produce the commit.
- If a commit or welcome is unprocessable, the client sends MLS_INVALID_COMMIT_WELCOME (opcode 31) and reinitializes.

---

## 2. Protocol Details

### 2.1 Voice WebSocket Opcodes (validated from source and docs)

Opcode 0 (IDENTIFY): server_id, user_id, session_id, token, video=bool, streams=[], max_dave_protocol_version=int. v9 adds channel_id field.

Opcode 1 (SELECT_PROTOCOL): protocol="udp" or "webrtc", data={address, port, mode} for UDP or data=sdp string for WebRTC, codecs=[{name, type, priority, payload_type, rtx_payload_type?, encode?, decode?}], rtc_connection_id=uuid4.

Opcode 2 (READY): ssrc=int, ip=string, port=int, modes=[string], experiments=[string], streams=[{type, ssrc, rtx_ssrc, rid, quality, active}].

Opcode 3 (HEARTBEAT): {t=timestamp_ms, seq_ack=last_sequence_number}.

Opcode 4 (SELECT_PROTOCOL_ACK / SESSION_DESCRIPTION): For UDP: audio_codec, video_codec, media_session_id, mode, secret_key=[int;32], dave_protocol_version. For WebRTC: sdp string plus dave_protocol_version.

Opcode 5 (SPEAKING): speaking=bitmask (0=none, 1=normal, 2=priority/soundshare), delay=int, ssrc=int. Since gateway v4, speaking is a bitmask not a boolean.

Opcode 8 (HELLO): heartbeat_interval=ms.

Opcode 12 (VIDEO): audio_ssrc=int, video_ssrc=int, rtx_ssrc=int, streams=[{type, rid, ssrc, active, quality, rtx_ssrc, max_bitrate, max_framerate, max_resolution: {type, width, height}}].

Opcode 14 (SESSION_UPDATE): Client sends codecs array to update supported codecs. Server responds with new audio_codec, video_codec, media_session_id, keyframe_interval.

DAVE opcodes 21-31: See protocol types above. Binary opcodes 25-30 use the binary WebSocket format.

### 2.2 Codec Payload Types

The Node.js library uses hardcoded payload types:
- Opus: PT 120, clock rate 48000
- H.264: PT 101, RTX PT 102, clock rate 90000
- H.265: PT 103, RTX PT 104, clock rate 90000
- VP8: PT 105, RTX PT 106, clock rate 90000
- VP9: PT 107, RTX PT 108, clock rate 90000
- AV1: PT 109, RTX PT 110, clock rate 90000

The Discord documentation shows a different example where the client specifies dynamic payload types. The discrepancy may indicate that Discord's server accepts client-specified PTs in the codecs array, or that the docs example is not representative of actual behavior. For the MVP, using the same fixed PTs as the Node.js library is the safe choice since those are known to work.

### 2.3 Transport Encryption

discord.py's _encrypt_aead_xchacha20_poly1305_rtpsize (from voice_client.py source):
- Creates nacl.secret.Aead(bytes(secret_key))
- Nonce: 24 bytes, first 4 bytes = incremental counter (BE uint32), remaining 20 bytes = zero
- Encrypts: box.encrypt(bytes(data), bytes(header), bytes(nonce))
- Output format: header + ciphertext + nonce[:4] (first 4 bytes of nonce, which is the counter)
- Counter wraps at 4294967295 (2^32 - 1)

The AAD (Additional Authenticated Data) is the RTP header bytes. This means the RTP header is sent in plaintext and authenticated but not encrypted. Only the payload is encrypted.

The full packet on the wire: [12-byte RTP header][encrypted payload][4-byte nonce counter].

For AES-256-GCM, the nonce is 12 bytes: 4-byte counter + 8 zero bytes.

### 2.4 DAVE Frame Encryption

discord.py calls dave_session.encrypt_opus(data) for audio. For video, the equivalent is dave_session.encrypt(media_type=MediaType.video, codec=Codec.h264, packet=frame_bytes).

The encrypt call operates on the COMPLETE encoded frame, not on individual RTP packets. After DAVE encryption, the encrypted frame is then split across RTP packets by the packetizer. This ordering is critical: DAVE encrypt first, then RTP packetize.

When the DAVE session is not ready (ready=False) or during passthrough transitions, frames pass through unencrypted.

### 2.5 Go Live vs Camera Stream

Camera stream:
- Uses the existing voice connection's WebSocket and WebRTC/UDP connection
- Sends self_video=true via VOICE_STATE_UPDATE gateway opcode
- Speaking mode = 1 (normal speaking)
- No separate stream server needed

Go Live:
- Requires separate STREAM_CREATE gateway opcode (opcode 18)
- Gateway responds with STREAM_CREATE event (stream_key, rtc_server_id)
- Gateway responds with STREAM_SERVER_UPDATE event (stream_key, endpoint, token)
- Connects to a SEPARATE voice WebSocket server
- Has its own SSRCs (from the stream server's READY payload)
- Has its own secret key (from the stream server's SESSION_DESCRIPTION)
- Has its own DAVE session (may be different protocol version)
- Speaking mode = 2 (priority/soundshare, not normal speaking)
- serverId: guild_id for guild channels, channel_id for DM calls
- daveChannelId: BigInt(serverId) - 1n as string

Stream key format: {type}:{guild_id?}:{channel_id}:{user_id}
For guild: "guild:41771983423143937:123456:789012"
For call: "call:123456:789012"

---

## 3. Implementation Observations

### 3.1 discord.py's VoiceClient Audio Send Path

From voice_client.py source, the _get_voice_packet method:
1. DAVE encrypt: dave_session.encrypt_opus(data) if dave session is ready, else raw data
2. Build RTP header: bytearray(12), header[0]=0x80 (version 2), header[1]=0x78 (PT 120 for opus), pack sequence (H) at offset 2, pack timestamp (I) at offset 4, pack ssrc (I) at offset 8
3. Transport encrypt: getattr(self, '_encrypt_' + self.mode)(header, packet)

For video, we replicate this but:
- header[1] = PT for video codec (e.g., 0x65 for H.264 PT 101)
- Set marker bit (bit 7 of header[1]) on last packet of frame
- Use video SSRC instead of audio SSRC
- Use separate sequence number and timestamp counters
- Use separate nonce counter for transport encryption
- DAVE encrypt with media_type=video, codec=h264 instead of encrypt_opus

### 3.2 H.264 RTP Packetization

NALU types relevant to packetization:
- Type 1: Coded slice of non-IDR picture (P-frame)
- Type 5: Coded slice of IDR picture (I-frame / keyframe)
- Type 7: Sequence Parameter Set (SPS)
- Type 8: Picture Parameter Set (PPS)
- Type 6: Supplemental Enhancement Information (SEI)

When a keyframe (NALU type 5) is present, the SPS (type 7) and PPS (type 8) must be sent with it, preceding the IDR NALU in the RTP stream.

FU-A fragmentation:
- FU indicator byte: (original_nal_header & 0x60) | 28. The 0x60 mask preserves the NRI bits. 28 is the FU-A NAL unit type.
- FU header byte: bit 7 = start bit, bit 6 = end bit, bits 4-0 = original NAL type.
- Maximum fragment payload: 1300 - 2 = 1298 bytes (subtracting FU indicator and FU header from MTU).

STAP-A aggregation:
- Used to bundle multiple small NALUs (SPS + PPS + small slice) into one RTP packet.
- STAP header: (nalu_header & 0xE0) | 24. The 0xE0 mask preserves NRI bits. 24 is the STAP-A type.
- Each NALU is prefixed with a 2-byte length (big-endian).
- Maximum 9 NALUs per STAP-A packet.

### 3.3 SPS VUI Rewriter

The SPSVUIRewriter (332 lines in TypeScript) modifies H.264 SPS NALUs to add or rewrite the Video Usability Information section. The critical changes:
- Forces bitstream_restriction_flag = 1
- Forces max_num_reorder_frames = 0 (no B-frame reordering)
- Sets max_dec_frame_buffering = max_num_ref_frames
- Strips video_signal_type information (sets present flag to 0)

The rewriter handles both cases: SPS with existing VUI (parses and modifies) and SPS without VUI (injects a new minimal VUI with just bitstream restriction).

The bitstream reader handles Exp-Golomb coding (unsigned and signed) and emulation prevention byte skipping (0x000003 sequences).

This is derived from WebRTC's C++ sps_vui_rewriter.cc at https://webrtc.googlesource.com/src/+/5f2c9278f35e47ff72eb191669d473b7400c9f3e/common_video/h264/sps_vui_rewriter.cc

### 3.4 FFmpeg Command Construction

The Node.js library builds FFmpeg commands with these specific flags:
- -bf 0: No B-frames. Critical for low latency. B-frames require future reference frames causing reordering delay.
- -force_key_frames expr:gte(t,n_forced*1): IDR keyframe every 1 second. Required by Discord's SFU for viewer join and error recovery.
- -pix_fmt yuv420p: Only 4:2:0 chroma subsampling is supported by Discord's decoder.
- -preset superfast: NOT ultrafast. Testing showed ultrafast causes bitrate spikes and stream stuttering.
- -tune film: Optimizes for live-action content. animation tuning is better for screen recordings but worse for camera.
- -forced-idr 1: Every keyframe is an IDR frame, ensuring decoder recovery from any point.
- -f nut: NUT container format for pipe output. Lower overhead than Matroska, simpler framing for real-time pipe reading.
- -bufsize:v {bitrate/2}k: VBV buffer size at half the average bitrate controls bitrate variability.

For H.264 input, the demuxer applies bitstream filters: h264_mp4toannexb (converts AVCC to Annex-B), h264_metadata with aud:remove (strips Access Unit Delimiters), dump_extra (ensures SPS/PPS extradata is present).

### 3.5 Frame Pacing Algorithm

The BaseMediaStream timing algorithm:
1. On first frame: record _start_time (wall clock via performance.now()) and _start_pts (presentation timestamp in ms)
2. For each frame: compute sleep = pts - start_pts + frametime - (now - start_time)
3. If sleep > 0: sleep for that duration
4. If sync is enabled and stream is behind partner (pts > partner_pts + tolerance): skip sleep, reset timing
5. If sync is enabled and stream is ahead (pts < partner_pts - tolerance): wait in loop sleeping frametime intervals until caught up
6. Sync tolerance: 20ms default

The frametime is calculated as (duration / timeBase.den) * timeBase.num * 1000, converting from codec time base to milliseconds.

The no_sleep mode bypasses all timing. Used during readrate_initial_burst where frames are sent as fast as possible for the first N seconds of PTS time, then normal pacing resumes.

### 3.6 Opus Frame Duration from TOC Byte

RFC 6716 section 3.1 defines the Opus TOC byte format. The configuration number (bits 7-3) determines frame size. The code field (bits 1-0) determines frame count: 0 = 1 frame, 1-2 = 2 frames, 3 = frame count in next byte.

Frame sizes by configuration: configs 0-3 are SILK narrowband (10, 20, 40, 60ms), configs 4-7 SILK medium-band (same sizes), configs 8-11 SILK wideband (same sizes), configs 12-13 hybrid super-wideband (10, 20ms), configs 14-15 hybrid fullband (10, 20ms), configs 16-19 CELT narrowband (2.5, 5, 10, 20ms), configs 20-23 CELT wideband (same), configs 24-27 CELT super-wideband (same), configs 28-31 CELT fullband (same).

Duration in samples = frame_size * frame_count. Duration in ms = samples / 48.

---

## 4. Architecture Decisions and Rationale

### 4.1 UDP Path over WebRTC

The Discord documentation confirms that UDP is the primary transport and WebRTC is the alternative. For send-only connections, the address and port can be randomized. This eliminates the entire WebRTC dependency chain (no node-datachannel, no aiortc, no ICE negotiation, no DTLS handshake, no SDP exchange for the transport layer). The media is sent as raw RTP over UDP with transport encryption.

The Node.js library uses WebRTC because it was developed before the UDP path was well understood for video sending, and because WebRTC provides built-in RTP packetization, pacing, and RTX handling. For Python, implementing RTP packetization directly is simpler than bridging to a WebRTC library.

### 4.2 discord.py Integration over Standalone

discord.py's VoiceClient already handles the voice WebSocket connection, DAVE/MLS key exchange, UDP socket creation, secret key storage, SSRC assignment, heartbeat, and reconnection. Building a standalone implementation would duplicate approximately 1500 lines of protocol handling. By extending VoiceClient, we only need to add the send-side components (RTP construction, packetization, encryption, frame pacing).

### 4.3 davey over dave.py

discord.py v2.7+ explicitly depends on davey (Snazzah's Rust implementation). Using dave.py (DisnakeDev's C++ bindings to libdave) would create a conflicting dependency. The DaveSession.encrypt(media_type, codec, packet) method in davey provides exactly the API needed for video frame encryption.

### 4.4 Separate Package over Extending voice-recv

voice-recv's abstractions (AudioSink, PacketRouter, JitterBuffer, SinkEventRouter) are receive-only. The send side needs different abstractions (RTP packetizer, frame pacer, FFmpeg pipeline). The shared surface (SSRC tracking, gateway hooks, UDP socket, DAVE session) is all provided by discord.py's VoiceClient. An optional compatibility module can provide a unified client when both packages are installed.

---

## 5. Open Research Areas

### 5.1 Go Live DAVE Channel ID Mapping

The Node.js library computes daveChannelId as BigInt(serverId) - 1n for stream connections. This needs verification: is this always the case, or does it depend on whether the stream is in a guild vs DM context? The protocol whitepaper does not document this mapping explicitly.

Research path: Test Go Live with DAVE enabled in both guild and DM contexts, verify the daveChannelId computation matches what the server expects.

### 5.2 UDP Path with DAVE

The Discord documentation describes DAVE in the context of WebRTC connections (using the Encoded Transform API). The interaction between DAVE frame encryption and the UDP transport path is not explicitly documented. Specifically: does DAVE-encrypted data go through the same UDP transport encryption (AEAD), or does DAVE replace transport encryption?

Inference from source code: DAVE and transport encryption are independent layers. DAVE encrypts the frame content (E2EE), transport encryption encrypts the RTP payload for transit to the SFU. Both layers apply. The Node.js library does both: DAVE encrypt the frame, then RTP packetize, then transport encrypt.

### 5.3 PyAV NUT Pipe Demuxing Latency

PyAV's av.open() with a pipe input and format="nut" has not been tested for real-time performance. The concern is that PyAV's internal buffering or the NUT demuxer's parsing might introduce latency that compounds over time.

Research path: Implement a simple test that pipes FFmpeg NUT output into PyAV and measures the time between FFmpeg producing a frame and PyAV yielding it. If latency exceeds 5ms per frame, fall back to raw Annex-B parsing with -f h264 pipe:3.

### 5.4 Stream Secret Key vs Voice Secret Key

When Go Live is active, there are two separate voice connections: the main voice connection (for audio/camera) and the stream connection (for Go Live). Each has its own secret key from its own SESSION_DESCRIPTION. The stream's secret key is used for encrypting the Go Live media.

Open question: Does the stream connection also have its own DAVE session, or does it share the main voice connection's DAVE session? The Node.js library creates a separate DAVE session for the stream connection (StreamConnection extends BaseMediaConnection which has its own _daveSession).

### 5.5 Multiple Simulcast Streams

The Node.js library sends a single simulcast stream (rid: "100", quality: 100). Discord's READY payload returns multiple streams with different rid values and SSRCs. The documentation suggests multiple quality levels are supported.

Research path: Investigate whether sending multiple simulcast streams (e.g., rid: "50" at lower quality and rid: "100" at full quality) provides any benefit, such as adaptive quality for viewers with limited bandwidth.

### 5.6 fixed_keyframe_interval Experiment

The READY payload includes an experiments array that may contain "fixed_keyframe_interval". The effect of this experiment is not documented. It may change the required keyframe interval or the way keyframes are signaled.

Research path: Connect to voice with and without this experiment enabled, compare the server's behavior regarding keyframe requests (PLI/FIR RTCP packets).

### 5.7 Voice Gateway v9 vs v8 Differences

The Node.js library uses v8. The documentation recommends v9 which adds channel_id to the Identify and Resume opcodes. The practical impact of this change is unclear: does v9 provide additional functionality, or is it merely a schema change for consistency?

Research path: Connect with v8 and v9, compare the READY and SESSION_DESCRIPTION payloads for any differences.

### 5.8 AES-256-GCM vs XChaCha20-Poly1305 Performance in Python

The documentation recommends AES-256-GCM when available (hardware acceleration). pynacl provides XChaCha20-Poly1305 via secret.Aead. Python's cryptography library provides AES-256-GCM via AESGCM. The performance difference in Python has not been benchmarked.

Research path: Benchmark both encryption modes in Python for typical RTP packet sizes (500-1300 bytes payload). If AES-GCM is significantly faster on hardware with AES-NI, prefer it. Otherwise default to XChaCha20 for simplicity (single dependency via pynacl).

### 5.9 Stage Channel Specifics

Stage channels are exempt from DAVE requirements. They also require setSuppressed(false) before the bot can speak. The interaction between Go Live and stage channels (max_stage_video_channel_users limit) has not been investigated.

Research path: Test Go Live in a stage channel, verify DAVE is not required, verify setSuppressed interaction.

### 5.10 Selfbot Detection Vectors

The Node.js library uses discord.js-selfbot-v13 which is a modified Discord.js client for user accounts. Discord's terms of service prohibit automation of user accounts. The technical detection vectors are not fully understood: does Discord detect non-standard client behavior in the voice protocol (e.g., specific SDP patterns, missing header extensions, codec negotiation patterns)?

Research path: Compare the RTP packets generated by the Node.js library against those generated by Discord's official client for the same stream content. Identify any differences in header extensions, padding, RTCP behavior, or timing patterns.

---

## 6. Resource Index

### Protocol Documentation
- Discord Voice Connections (unofficial comprehensive docs): https://docs.discord.food/topics/voice-connections
- DAVE Protocol Whitepaper: https://daveprotocol.com/
- DAVE Protocol Repository: https://github.com/discord/dave-protocol
- RFC 8285 (RTP Header Extensions): https://www.rfc-editor.org/rfc/rfc8285
- RFC 6716 (Opus Codec): https://www.rfc-editor.org/rfc/rfc6716
- RFC 3550 (RTP): https://www.rfc-editor.org/rfc/rfc3550

### Implementation References
- discord-video-stream (Node.js): https://github.com/Discord-RE/Discord-video-stream
- discord-ext-voice-recv (Python): https://github.com/Yui-Koi/discord-ext-voice-recv (dm-voice branch)
- discord.py VoiceClient: https://github.com/Rapptz/discord.py/blob/master/discord/voice_client.py
- discord.py VoiceConnectionState: https://github.com/Rapptz/discord.py/blob/master/discord/voice_state.py
- Snazzah/davey (Rust DAVE, Python bindings): https://github.com/Snazzah/davey
- DisnakeDev/dave.py (Python bindings to libdave): https://github.com/DisnakeDev/dave.py
- discord/libdave (official C++ DAVE): https://github.com/discord/libdave
- WebRTC SPS VUI Rewriter (original C++ source): https://webrtc.googlesource.com/src/+/5f2c9278f35e47ff72eb191669d473b7400c9f3e/common_video/h264/sps_vui_rewriter.cc
- aixxe.net Discord Video Bot writeup: https://aixxe.net/2021/04/discord-video-bot
- mrjvs/Discord-video-experiment (original PoC): https://github.com/mrjvs/Discord-video-experiment

### PyPI Packages
- davey: https://pypi.org/project/davey/ (version 0.1.5, Snazzah's Rust DAVE, used by discord.py)
- dave.py: https://pypi.org/project/dave.py/ (DisnakeDev's libdave bindings, alternative)
- discord.py: https://pypi.org/project/discord.py/
- discord-ext-voice-recv: https://pypi.org/project/discord-ext-voice-recv/
- PyAV: https://pypi.org/project/av/
- PyNaCl: https://pypi.org/project/PyNaCl/

### GitHub Issues
- DAVE implementation tracking: https://github.com/Discord-RE/Discord-video-stream/issues/102
- Go Live / Screenshare discussion: https://github.com/aiko-chan-ai/discord.js-selfbot-v13/issues/293
- discord-ext-voice-recv fork (DM voice): https://github.com/Yui-Koi/discord-ext-voice-recv (branch: dm-voice)

---

## 7. Known Quirks and Edge Cases

The SPS VUI rewriter exists because Discord's WebRTC decoder requires bitstream_restriction to be present in the SPS. Without it, the decoder may buffer frames incorrectly. The max_num_reorder_frames=0 setting is critical because it tells the decoder that no frame reordering occurs, matching the -bf 0 FFmpeg flag. If these are inconsistent (e.g., -bf 0 is set but max_num_reorder_frames is not 0), the decoder may still buffer frames expecting reordering.

The pacing handler rate of 25 Mbps with burst size 1 is extremely conservative. Each RTP packet is individually paced with no bursting allowed. For a 5 Mbps stream, this adds micro-latency per packet but prevents UDP buffer overflow from large keyframe bursts.

The NUT container was chosen over Matroska for lower framing overhead and simpler real-time pipe reading. NUT's header is minimal compared to Matroska's EBML header, reducing the initial buffering delay.

The Opus encoder uses -ac 2 (stereo) and -ar 48000 (48kHz). The LFE mix level is set to 1 for surround sound content. The volume filter volume@internal_lib=1.0 is used for real-time volume control via ZMQ (optional, only on Node.js).

The stream preview feature decodes keyframes only (checks AV_PKT_FLAG_KEY), resizes to 1024x576, converts to JPEG, and uploads via a REST API call. This is relatively expensive and disabled by default.

The readrate_initial_burst option is used for live inputs like Puppeteer browser captures. It disables frame pacing for the first N seconds of PTS time, filling the pipeline quickly before switching to normal real-time pacing.

The speaking mode difference between Go Live (mode=2, priority/soundshare) and camera (mode=1, normal) is significant. Getting this wrong means the stream will not register properly with Discord's UI.

The Go Live daveChannelId computation (BigInt(serverId) - 1) is an internal mapping not documented in the protocol whitepaper. The stream server derives its DAVE channel ID by subtracting 1 from the stream's RTC server ID.

For send-only connections, the UDP address and port in SELECT_PROTOCOL can be randomized because they are only used for receiving. This is explicitly stated in the Discord documentation and eliminates the need for IP discovery when only sending.

The binary WebSocket format for DAVE opcodes includes a 2-byte sequence number in server-to-client messages but NOT in client-to-server messages. Getting this asymmetry wrong causes parsing failures.

The voice gateway does not provide a default version. The client must explicitly pass ?v=N in the connection URL. Using an outdated version (e.g., v3) may result in missing features or deprecated behavior.
