# discord-video-stream-python -- Final Plan

## Scope

Port `@dank074/discord-video-stream` to Python. Integrate with discord.py's existing voice infrastructure (VoiceClient, DAVE via davey, UDP socket). Send H.264 video + Opus audio via Go Live in guild voice channels.

## Key Insight

discord.py v2.7+ already handles 60% of the protocol layer:
- Voice WebSocket connection, handshake, heartbeat, resume, DAVE/MLS key exchange
- UDP socket, IP discovery, secret key, SSRC assignment, encryption mode
- DAVE session (`davey.DaveSession`) with full MLS opcode flow

We build only the send side: RTP construction, packetization, transport encryption (encrypt), DAVE frame encryption (video), FFmpeg pipeline, frame pacing, stream lifecycle.

---

## Phase 1: RTP Packet Construction + Transport Encryption

**Goal:** Construct valid RTP packets and encrypt them for UDP transport.

### Task 1.1: `rtp/serialize.py` -- RTP Packet Builder

Build RTP packets from components. Reference voice-recv's `rtp.py` for header format.

```python
# Header format: struct.Struct('>xxHII') = skip 2 bytes, then:
#   sequence (H, 16-bit), timestamp (I, 32-bit), ssrc (I, 32-bit)
# Total header: 12 bytes

def build_rtp_header(
    sequence: int,
    timestamp: int,
    ssrc: int,
    payload_type: int,
    marker: bool = False,
    extension: bool = False,
) -> bytes:
    """Build 12-byte RTP header."""

def build_rtp_packet(
    header: bytes,
    payload: bytes,
    extension_data: bytes | None = None,
) -> bytes:
    """Build complete RTP packet: header + [extension header + ext data] + payload."""

def build_rtcp_sr(
    ssrc: int,
    ntp_timestamp: int,
    rtp_timestamp: int,
    packet_count: int,
    octet_count: int,
) -> bytes:
    """Build RTCP Sender Report for sync."""
```

Invariants:
- Version = 2 (bits 7-6 of first byte)
- Padding = 0
- Extension bit set if header extensions present
- Marker bit set on last packet of a frame (H.264) or on each Opus packet
- Sequence: 16-bit, wraps at 65536
- Timestamp: 32-bit, wraps at 2^32
- Clock rate: 90000 for video, 48000 for audio

### Task 1.2: `rtp/crypto.py` -- Transport Encryption (Encrypt Side)

Mirror the decrypt side from voice-recv's `reader.py` `PacketDecryptor`.

```python
class TransportEncryptor:
    """Encrypts RTP packets for UDP transport."""

    def __init__(self, secret_key: bytes, mode: str):
        # mode: "aead_xchacha20_poly1305_rtpsize" or "aead_aes256_gcm_rtpsize"
        self._aead = nacl.secret.Aead(secret_key)  # for xchacha20
        # or AESGCM(secret_key) for aes256
        self._nonce_counter = 0

    def encrypt_rtp(self, header: bytes, payload: bytes) -> bytes:
        """
        AEAD encrypt payload with header as AAD.
        Returns: [encrypted_payload][4-byte nonce_counter BE]
        """
        nonce = self._make_nonce()
        encrypted = self._aead.encrypt(payload, header, nonce)
        return encrypted + struct.pack('>I', self._nonce_counter - 1)

    def _make_nonce(self) -> bytes:
        nonce = struct.pack('>I', self._nonce_counter)
        self._nonce_counter = (self._nonce_counter + 1) & 0xFFFFFFFF
        return nonce + b'\x00' * 20  # pad to 24 bytes for xchacha20
```

Invariants:
- AAD = RTP header bytes (the 12-byte header, not including extensions)
- Nonce = 4-byte incremental counter + 20 zero bytes (for xchacha20) or 8 zero bytes (for aes256, total 12)
- Output = ciphertext + 4-byte nonce counter appended
- Counter wraps at 2^32
- Each packet gets a unique incrementing nonce

### Task 1.3: `rtp/tests/` -- Unit Tests

- Test header serialization matches expected bytes
- Test encrypt/decrypt round-trip
- Test sequence/timestamp wrap-around
- Test marker bit on last fragment

**Test: build a known RTP packet, serialize it, compare to expected bytes.**

---

## Phase 2: H.264 Packetization

**Goal:** Convert H.264 Annex-B frames into RTP packet payloads.

### Task 2.1: `rtp/h264.py` -- NALU Processing + Packetization

```python
def split_nalu(frame: bytes) -> list[bytes]:
    """Split Annex-B frame into NALUs by start codes (0x000001, 0x00000001)."""

def get_nalu_type(nalu: bytes) -> int:
    """Return nalu[0] & 0x1F for H.264."""

class H264Packetizer:
    MAX_PAYLOAD = 1300  # leave room for RTP header

    def packetize_frame(self, frame: bytes, timestamp: int, ssrc: int, pt: int) -> list[bytes]:
        """
        Input: complete H.264 frame in Annex-B format
        Output: list of complete RTP packets (header + payload)
        """
        nalus = split_nalu(frame)
        packets = []
        for nalu in nalus:
            if len(nalu) <= self.MAX_PAYLOAD:
                # Single NAL unit packet
                packets.append(self._single_nalu(nalu, timestamp, ssrc, pt))
            else:
                # FU-A fragmentation
                packets.extend(self._fu_a(nalu, timestamp, ssrc, pt))
        # Set marker on last packet
        if packets:
            packets[-1] = set_marker(packets[-1], True)
        return packets

    def _fu_a(self, nalu: bytes, ts: int, ssrc: int, pt: int) -> list[bytes]:
        """
        Fragment NALU using FU-A.
        FU indicator: (nalu[0] & 0x60) | 28  (FU-A type)
        FU header: start(1) | end(1) | reserved(1) | type(5)
        """
        f_nri = nalu[0] & 0x60
        nal_type = nalu[0] & 0x1F
        fu_indicator = f_nri | 28  # FU-A

        payload = nalu[1:]  # strip NAL header
        max_frag = self.MAX_PAYLOAD - 2  # FU indicator + FU header

        fragments = []
        offset = 0
        while offset < len(payload):
            chunk = payload[offset:offset + max_frag]
            is_start = offset == 0
            is_end = offset + max_frag >= len(payload)

            fu_header = nal_type
            if is_start:
                fu_header |= 0x80  # start bit
            if is_end:
                fu_header |= 0x40  # end bit

            rtp_payload = bytes([fu_indicator, fu_header]) + chunk
            fragments.append(rtp_payload)
            offset += max_frag

        return fragments
```

Invariants:
- NALU type 7 (SPS) and 8 (PPS) should be sent with every keyframe
- NALU type 5 (IDR) is a keyframe -- SPS+PPS must precede it
- Marker bit = 1 on the last RTP packet of an access unit (frame)
- FU-A indicator byte: `(nalu[0] & 0x60) | 28`
- FU-A header: bit 7 = start, bit 6 = end, bits 4-0 = original NAL type
- Emulation prevention: 0x000003 sequences in NALU data must be preserved

### Task 2.2: `rtp/tests/test_h264.py`

- Test single NALU packetization for small NALUs
- Test FU-A fragmentation for large NALUs (verify start/end bits)
- Test marker bit on last fragment
- Test SPS/PPS/IDR ordering

---

## Phase 3: FFmpeg Pipeline + Demuxing

**Goal:** Transcode input to H.264 + Opus in NUT format, extract frames.

### Task 3.1: `media/ffmpeg.py` -- FFmpeg Subprocess

```python
@dataclass
class StreamOptions:
    url: str
    width: int = -2          # -2 = maintain aspect ratio
    height: int = -2
    frame_rate: int | None = None
    bitrate_video: int = 5000    # kbps
    bitrate_video_max: int = 7000
    bitrate_audio: int = 128
    include_audio: bool = True
    hwaccel: bool = False
    custom_input_options: list[str] = field(default_factory=list)
    custom_flags: list[str] = field(default_factory=list)

class FFmpegProcess:
    """Manages FFmpeg subprocess with NUT pipe output."""

    def __init__(self, options: StreamOptions):
        self._cmd = self._build_command(options)
        self._process: subprocess.Popen | None = None

    def _build_command(self, opts: StreamOptions) -> list[str]:
        cmd = ['ffmpeg', '-y', '-loglevel', 'verbose', '-nostats']
        if opts.hwaccel:
            cmd += ['-hwaccel', 'auto']
        cmd += opts.custom_input_options
        cmd += ['-i', opts.url]

        # Video: H.264, no B-frames, forced keyframes every 1s
        cmd += [
            '-map', '0:v',
            '-vcodec', 'libx264',
            '-preset', 'superfast',
            '-tune', 'film',
            '-forced-idr', '1',
            '-bf', '0',
            '-pix_fmt', 'yuv420p',
            '-force_key_frames', 'expr:gte(t,n_forced*1)',
            '-b:v', f'{opts.bitrate_video}k',
            '-maxrate:v', f'{opts.bitrate_video_max}k',
            '-bufsize:v', f'{opts.bitrate_video // 2}k',
            '-vf', f'scale={opts.width}:{opts.height}',
        ]
        if opts.frame_rate:
            cmd += ['-r', str(opts.frame_rate)]

        # Audio: Opus
        if opts.include_audio:
            cmd += [
                '-map', '0:a:0?',
                '-ac', '2',
                '-ar', '48000',
                '-acodec', 'libopus',
                '-b:a', f'{opts.bitrate_audio}k',
            ]

        cmd += opts.custom_flags
        cmd += ['-f', 'nut', 'pipe:1']
        return cmd

    def start(self) -> asyncio.subprocess.Process:
        """Start FFmpeg, return process with stdout pipe."""

    def stop(self):
        """Terminate FFmpeg gracefully."""
```

### Task 3.2: `media/demux.py` -- NUT Demuxing

Two approaches, implement primary first, fallback if needed:

**Primary: PyAV**
```python
async def demux_nut(pipe: BinaryIO) -> tuple[VideoStream, AudioStream]:
    """Demux NUT from FFmpeg stdout pipe using PyAV."""
    container = av.open(pipe, format='nut', buffer_size=8192)
    # Extract video/audio stream info
    # Yield packets as async iterators
```

**Fallback: Raw Annex-B**
If PyAV latency is problematic, bypass container entirely:
```python
# FFmpeg command changes to:
# Video: -f h264 pipe:3  (raw Annex-B)
# Audio: -f data -c:a copy pipe:4  (raw Opus)
# Two separate subprocess pipes, simpler to parse
```

### Task 3.3: `media/tests/`

- Test FFmpeg command construction (verify flags)
- Test NUT demuxing from a test file
- Test frame extraction (video + audio packets have correct PTS)

---

## Phase 4: Frame Pacing + A/V Sync

**Goal:** Send frames at the correct timing, maintain lip sync.

### Task 4.1: `media/pacer.py`

Port `BaseMediaStream` timing from Node.js:

```python
class FramePacer:
    """Controls frame send timing with A/V sync."""

    def __init__(self, clock_rate: int):
        self._clock_rate = clock_rate
        self._start_time: float | None = None
        self._start_pts: float | None = None
        self._sync_partner: FramePacer | None = None
        self._sync_tolerance_ms = 20.0
        self._no_sleep = False

    async def pace(self, pts_ms: float, frametime_ms: float):
        """Sleep if needed to maintain correct timing."""
        if self._no_sleep:
            return

        now = time.monotonic() * 1000
        self._start_time = self._start_time or now
        self._start_pts = self._start_pts or pts_ms

        # How long we should have been running
        expected_elapsed = pts_ms - self._start_pts + frametime_ms
        actual_elapsed = now - self._start_time
        sleep_ms = expected_elapsed - actual_elapsed

        if self._sync_partner and self._is_behind(pts_ms):
            # Skip sleep to catch up
            self._reset_timing()
            return

        if self._sync_partner and self._is_ahead(pts_ms):
            # Wait for partner
            while self._is_ahead(pts_ms):
                await asyncio.sleep(frametime_ms / 1000)
            self._reset_timing()
            return

        if sleep_ms > 0:
            await asyncio.sleep(sleep_ms / 1000)
```

Invariants:
- PTS is in milliseconds, converted from codec time base
- Video PTS and audio PTS are compared for sync
- Tolerance of 20ms: if video is within 20ms of audio, no correction
- If video is ahead: wait (sleep in loop checking partner)
- If video is behind: skip sleep, reset timing compensation
- `no_sleep` mode: send frames as fast as possible (for initial burst)

---

## Phase 5: Stream Lifecycle + Integration

**Goal:** Join voice, start Go Live, send video, stop.

### Task 5.1: `protocol/types.py` -- Protocol Data Structures

```python
# Codec config (matches discord-video-stream Node.js lib)
OPUS_CODEC = {"name": "opus", "type": "audio", "priority": 1000, "payload_type": 120}
H264_CODEC = {"name": "H264", "type": "video", "priority": 1000,
              "payload_type": 101, "rtx_payload_type": 102, "encode": True, "decode": True}

SIMULCAST_STREAMS = [{"type": "screen", "rid": "100", "quality": 100}]

def generate_stream_key(type: str, guild_id: str | None, channel_id: str, user_id: str) -> str:
    if type == "guild":
        return f"guild:{guild_id}:{channel_id}:{user_id}"
    return f"call:{channel_id}:{user_id}"

def parse_stream_key(key: str) -> dict: ...
```

### Task 5.2: `streamer.py` -- Main API

```python
class VideoStreamer:
    """Main entry point for Discord video streaming."""

    def __init__(self, client: discord.Client):
        self._client = client
        self._voice_client: discord.VoiceClient | None = None
        self._stream_ws: websockets.WebSocketClientProtocol | None = None
        self._stream_ssrc: int = 0
        self._stream_rtx_ssrc: int = 0
        self._video_nonce = 0  # separate from audio nonce

    async def join_voice(self, guild_id: int, channel_id: int) -> None:
        """Join a voice channel using discord.py's VoiceClient."""
        guild = self._client.get_guild(guild_id)
        channel = guild.get_channel(channel_id)
        self._voice_client = await channel.connect()

    async def start_go_live(self) -> None:
        """Start a Go Live stream."""
        # 1. Send STREAM_CREATE via gateway
        # 2. Wait for STREAM_CREATE + STREAM_SERVER_UPDATE events
        # 3. Connect to stream voice server (separate WS)
        # 4. Identify, get stream SSRCs
        # 5. Select protocol (UDP, with our codecs)
        # 6. Get session description (secret key for stream)
        # 7. Signal VIDEO opcode with stream SSRCs

    async def play(self, url: str, options: StreamOptions | None = None) -> None:
        """Start streaming a URL via Go Live."""
        # 1. Start FFmpeg
        # 2. Demux video + audio
        # 3. Create packetizers (H.264 + Opus)
        # 4. Create transport encryptor (with stream secret key)
        # 5. Create frame pacer
        # 6. Loop: demux frame -> pace -> packetize -> DAVE encrypt -> transport encrypt -> UDP send
        # 7. Handle cancellation

    def stop(self) -> None: ...
    def leave(self) -> None: ...
```

### Task 5.3: `stream_connection.py` -- Go Live Connection

```python
class StreamConnection:
    """Manages Go Live voice WebSocket + media sending."""

    def __init__(self, streamer: VideoStreamer, guild_id: str, channel_id: str, user_id: str):
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.stream_key = generate_stream_key("guild", guild_id, channel_id, user_id)

    async def connect(self, endpoint: str, token: str, session_id: str):
        """Connect to stream voice server."""
        # Connect WS to wss://{endpoint}/?v=9
        # IDENTIFY with server_id=guild_id, user_id, session_id, token, video=true
        # Wait for READY -> get stream SSRC, video SSRC, RTX SSRC
        # SELECT_PROTOCOL (UDP, codecs, random address/port)
        # Wait for SESSION_DESCRIPTION -> get secret_key, mode, dave_protocol_version
        # DAVE key exchange if dave_protocol_version > 0

    async def send_video_attributes(self, width: int, height: int, fps: int):
        """Send VIDEO opcode (12) with our stream SSRCs."""

    async def send_speaking(self, speaking: bool):
        """Send SPEAKING opcode (5) with mode=2 (priority/soundshare)."""
```

Invariants for Go Live:
- Stream serverId = guild_id for guild, channel_id for DM
- daveChannelId = BigInt(serverId) - 1
- Speaking mode = 2 (not 1 which is camera)
- Separate WS from main voice connection
- Separate SSRCs (from READY streams array)
- Separate secret key (from stream SESSION_DESCRIPTION)
- Separate DAVE session (may be different protocol version)
- Media goes over the SAME UDP socket but with different SSRCs and keys

### Task 5.4: DAVE Frame Encryption for Video

```python
class VideoEncryptor:
    """Encrypts video frames using DAVE."""

    def __init__(self, dave_session, ssrc: int, codec: str):
        self._session = dave_session
        self._ssrc = ssrc
        # Assign SSRC to codec in DAVE session
        # dave_session assigns ssrc -> codec mapping

    def encrypt(self, frame: bytes) -> bytes:
        """Encrypt a complete video frame using DAVE."""
        if not self._session or not self._session.ready:
            return frame  # passthrough mode
        return self._session.encrypt(
            media_type="video",
            ssrc=self._ssrc,
            frame=frame
        )
```

Invariants:
- DAVE encryption happens on the COMPLETE frame BEFORE RTP packetization
- The encrypted frame is then split across RTP packets
- Order: encode -> DAVE encrypt -> RTP packetize -> transport encrypt -> send
- Passthrough mode: when dave_protocol_version=0 or during transitions, frames pass unencrypted
- The dave_session is shared with discord.py's audio sending

### Task 5.5: `protocol/vui.py` -- SPS VUI Rewriter

Port from TypeScript. ~200 lines of Exp-Golomb bitstream manipulation.

Key changes to SPS:
- Force `bitstream_restriction_flag = 1`
- Force `max_num_reorder_frames = 0`
- Set `max_dec_frame_buffering = max_num_ref_frames`
- Strip `video_signal_type` (set present flag to 0)

This runs on every frame that contains an SPS NALU.

---

## Phase 6: Integration Testing

### Test Order (each builds on previous)

**Test 1: RTP + Crypto Unit Tests**
- Build RTP packets, serialize, verify format
- Encrypt/decrypt round-trip with pynacl
- H.264 FU-A fragmentation correctness

**Test 2: FFmpeg Pipeline**
- Run FFmpeg on a test file, verify NUT output
- Demux and extract video/audio frames
- Verify PTS values are correct

**Test 3: Frame Pacing**
- Feed frames to pacer, verify timing
- Test A/V sync with known PTS values

**Test 4: Voice Connection (without sending)**
- Join voice channel using discord.py
- Verify VoiceClient has socket, secret_key, ssrc, mode
- Verify DAVE session is established

**Test 5: Go Live Connection**
- Start Go Live, connect to stream server
- Verify READY + SESSION_DESCRIPTION received
- Verify stream SSRCs and secret key

**Test 6: Send RTP to Discord**
- Send a single H.264 keyframe as RTP over UDP
- Verify no errors from voice server
- Use Wireshark to inspect packet structure

**Test 7: Full Pipeline**
- Join voice, start Go Live, play a test video
- Viewer in Discord sees video
- Check A/V sync over 30+ seconds
- Test cancellation/cleanup

**Test 8: DAVE Verification**
- Verify frames are encrypted (payload is not raw H.264)
- Test with multiple participants (MLS group with 2+ members)
- Test protocol transitions (member join/leave)

---

## File Map (Final)

```
discord_video_stream/
    __init__.py                  # 20 lines
    streamer.py                  # 250 lines -- Main API
    stream_connection.py         # 200 lines -- Go Live WS + media
    rtp/
        __init__.py              # 10 lines
        serialize.py             # 120 lines -- RTP/RTCP packet builder
        h264.py                  # 200 lines -- NALU + FU-A/STAP-A
        crypto.py                # 80 lines -- Transport encryption (encrypt)
    media/
        __init__.py              # 10 lines
        ffmpeg.py                # 150 lines -- FFmpeg subprocess
        demux.py                 # 180 lines -- NUT demuxing (PyAV)
        pacer.py                 # 150 lines -- Frame pacing + A/V sync
    protocol/
        __init__.py              # 5 lines
        types.py                 # 100 lines -- Codec config, stream key, etc.
        vui.py                   # 200 lines -- SPS VUI rewriter
    tests/
        test_rtp.py
        test_h264.py
        test_crypto.py
        test_ffmpeg.py
        test_pacer.py
TOTAL: ~1700 lines (excluding tests)
```

---

## Dependencies (Final)

| Package | Purpose | Source |
|---|---|---|
| `discord.py[voice]` >=2.7 | Gateway, voice WS, DAVE (via davey), UDP socket | pip |
| `websockets` >=12.0 | Go Live voice WS (separate from discord.py's WS) | pip |
| `PyAV` >=12.0 | NUT demuxing from FFmpeg pipe | pip |
| `pynacl` >=1.5 | Transport encryption (AEAD) | pip (already required by discord.py) |
| FFmpeg | Video/audio transcoding | system |

Note: `davey` is bundled with discord.py[voice] -- it is the DAVE dependency discord.py requires. We do NOT need `dave.py` by DisnakeDev.

---

## What Changed from Earlier Plans

| Item | Old Plan | Final Plan | Reason |
|---|---|---|---|
| Voice WS | Build from scratch | discord.py handles | discord.py VoiceClient already does this |
| DAVE | Use dave.py | Use davey (via discord.py) | discord.py v2.7 requires davey |
| Gateway events | Build from scratch | discord.py handles | VoiceStateUpdate, VoiceServerUpdate already dispatched |
| UDP socket | Create new | discord.py provides | VoiceClient.socket is the shared UDP socket |
| Secret key | Manage ourselves | discord.py provides | VoiceClient.secret_key |
| SSRC | Manage ourselves | discord.py provides | VoiceClient.ssrc (audio), READY payload (video) |
| Heartbeat | Build from scratch | discord.py handles | VoiceConnectionState manages heartbeat |
| Reconnection | Build from scratch | discord.py handles | VoiceConnectionState manages resume |
| Line count | 2650 | 1700 | 35% reduction from reusing discord.py |
| WebRTC bridge | Needed | Not needed | UDP path exists |
| dave.py dependency | Required | Not needed | discord.py uses davey |
