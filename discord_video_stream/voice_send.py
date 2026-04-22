"""
Voice send extension for Discord video streaming.

Extends discord.VoiceClient to add video send capability. Maintains
separate video sequence number, timestamp, and nonce counters
independent from the audio path managed by discord.py.

The send pipeline for video:
1. DAVE encrypt (dave_session.encrypt(MediaType.video, Codec.h264, frame))
2. RTP packetize (H.264 NALU split, FU-A fragmentation)
3. Transport encrypt (AEAD with stream secret key)
4. UDP sendto (via VoiceClient's socket)

Reference: discord.py voice_client.py _get_voice_packet
           nodejs-reference/src/client/voice/WebRtcWrapper.ts sendVideoFrame
"""

from __future__ import annotations

import logging
import struct
from typing import TYPE_CHECKING

try:
    from .rtp.serialize import build_rtp_header, build_rtp_packet, set_marker
    from .rtp.h264 import H264Packetizer, split_nalu, get_nalu_type, H264NalUnitTypes
    from .rtp.crypto import TransportEncryptor
    from .protocol.vui import rewrite_sps_vui
except ImportError:
    from rtp.serialize import build_rtp_header, build_rtp_packet, set_marker
    from rtp.h264 import H264Packetizer, split_nalu, get_nalu_type, H264NalUnitTypes
    from rtp.crypto import TransportEncryptor
    from protocol.vui import rewrite_sps_vui

if TYPE_CHECKING:
    from typing import Optional
    try:
        from .stream_connection import StreamConnection
    except ImportError:
        from stream_connection import StreamConnection

__all__ = [
    'VideoSender',
]

log = logging.getLogger(__name__)


class VideoSender:
    """Manages video frame sending for a Go Live stream.

    This is NOT a VoiceClient subclass. It operates alongside the main
    VoiceClient, using the stream connection's SSRCs and secret key
    for the video RTP stream.

    The video RTP stream is completely independent from the audio RTP
    stream: different SSRC, different sequence numbers, different
    timestamps, different nonce counter.

    Usage:
        sender = VideoSender(stream_connection)
        sender.configure(width=1920, height=1080, fps=30)
        sender.start()
        # In the send loop:
        sender.send_frame(h264_frame, pts_ms, frametime_ms)
        sender.stop()
    """

    # Maximum RTP payload size (MTU - IP/UDP/RTP headers)
    MAX_PAYLOAD = 1300

    def __init__(self, stream_conn: StreamConnection) -> None:
        self._stream_conn = stream_conn

        # Separate video RTP state (independent from audio)
        self._sequence: int = 0
        self._timestamp: int = 0
        self._nonce_counter: int = 0

        # H.264 packetizer
        self._packetizer: Optional[H264Packetizer] = None

        # Transport encryptor (uses stream connection's secret key)
        self._encryptor: Optional[TransportEncryptor] = None

        # SPS VUI rewriter state
        self._rewrite_sps: bool = True

        # Video attributes
        self._width: int = 0
        self._height: int = 0
        self._fps: int = 0

        # Stream server endpoint (from READY opcode)
        self._target_ip: str = ''
        self._target_port: int = 0

        # Packet/octet counters for RTCP Sender Reports
        self._packet_count: int = 0
        self._octet_count: int = 0

        # Running state
        self._active: bool = False

        # Send callback: callable(packet, ip, port)
        self._send_callback = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def timestamp(self) -> int:
        return self._timestamp

    def configure(self, width: int, height: int, fps: int) -> None:
        """Set video stream attributes."""
        self._width = width
        self._height = height
        self._fps = fps

    def start(self) -> None:
        """Initialize the video sender with stream connection parameters.

        Requires stream_connection to have completed the handshake
        (READY and SELECT_PROTOCOL_ACK received).

        The stream server endpoint (IP and port) is read from the
        stream connection's READY params and stored for UDP sending.
        This is the CRITICAL fix for error 2012: video packets must
        go to the stream server's endpoint, NOT the main voice
        connection's endpoint.
        """
        video_ssrc = self._stream_conn.video_ssrc
        if video_ssrc == 0:
            raise RuntimeError('Stream connection not ready (no video SSRC)')

        secret_key = self._stream_conn.secret_key
        if secret_key is None or len(secret_key) == 0:
            raise RuntimeError('Stream connection not ready (no secret key)')

        mode = self._stream_conn.encryption_mode
        if mode is None:
            raise RuntimeError('Stream connection not ready (no encryption mode)')

        # Extract stream server endpoint from READY params
        ready = self._stream_conn.ready_params
        if ready is None:
            raise RuntimeError('Stream connection not ready (no READY params)')
        self._target_ip = ready.ip
        self._target_port = ready.port

        # Create H.264 packetizer with stream's video SSRC
        self._packetizer = H264Packetizer(
            ssrc=video_ssrc,
            payload_type=101,  # H.264 PT from CodecPayloadType
        )

        # Create transport encryptor with stream's secret key
        self._encryptor = TransportEncryptor(secret_key, mode)

        self._sequence = 0
        self._timestamp = 0
        self._nonce_counter = 0
        self._packet_count = 0
        self._octet_count = 0
        self._active = True

        log.info(
            'VideoSender started: ssrc=%s mode=%s target=%s:%s',
            video_ssrc, mode, self._target_ip, self._target_port,
        )

    def stop(self) -> None:
        """Stop the video sender."""
        self._active = False
        log.info('VideoSender stopped')

    def send_frame(self, frame: bytes, pts_ms: float, frametime_ms: float) -> int:
        """Send a single H.264 video frame.

        Complete pipeline:
        1. SPS VUI rewrite (if SPS NALU present)
        2. DAVE encrypt (if session ready)
        3. RTP packetize (H.264 NALU split + FU-A)
        4. Transport encrypt (AEAD)
        5. UDP send

        Parameters
        ----------
        frame : bytes
            Complete H.264 frame in Annex-B format.
        pts_ms : float
            Presentation timestamp in milliseconds.
        frametime_ms : float
            Frame duration in milliseconds.

        Returns
        -------
        int
            Number of RTP packets sent.
        """
        if not self._active:
            raise RuntimeError('VideoSender not active')

        if self._packetizer is None or self._encryptor is None:
            raise RuntimeError('VideoSender not started')

        # Step 1: SPS VUI rewrite
        if self._rewrite_sps:
            frame = self._rewrite_frame_sps(frame)

        # Step 2: DAVE encrypt
        frame = self._dave_encrypt(frame)

        # Step 3: Convert pts_ms to RTP timestamp (90kHz clock)
        rtp_timestamp = int(pts_ms * 90) & 0xFFFFFFFF
        self._packetizer.set_timestamp(rtp_timestamp)
        self._timestamp = rtp_timestamp

        # Step 4: RTP packetize
        packets = self._packetizer.packetize_frame(frame)

        # Step 5: Transport encrypt and send each packet
        video_ssrc = self._stream_conn.video_ssrc
        for pkt in packets:
            header = bytes(pkt[:12])
            payload = bytes(pkt[12:])
            encrypted = self._encryptor.encrypt_rtp(header, payload)
            wire_packet = header + encrypted
            self._send_udp(wire_packet)
            self._packet_count += 1
            self._octet_count += len(payload)

        return len(packets)

    def _rewrite_frame_sps(self, frame: bytes) -> bytes:
        """Rewrite SPS NALUs in a frame for Discord compatibility.

        Scans for SPS NALUs (type 7) and rewrites their VUI section
        to force bitstream_restriction and max_num_reorder_frames=0.

        If rewriting fails (e.g., malformed SPS), the original NALU is
        preserved unchanged.
        """
        nalus = split_nalu(frame)
        modified = False
        new_nalus = []

        for nalu in nalus:
            if get_nalu_type(nalu) == H264NalUnitTypes.SPS:
                try:
                    rewritten = rewrite_sps_vui(nalu)
                    if rewritten != nalu:
                        modified = True
                        new_nalus.append(rewritten)
                    else:
                        new_nalus.append(nalu)
                except (ValueError, IndexError):
                    # Malformed SPS, preserve as-is
                    new_nalus.append(nalu)
            else:
                new_nalus.append(nalu)

        if not modified:
            return frame

        # Reassemble with start codes
        parts = []
        for nalu in new_nalus:
            parts.append(b'\x00\x00\x01')
            parts.append(nalu)
        return b''.join(parts)

    def _dave_encrypt(self, frame: bytes) -> bytes:
        """Encrypt a video frame using DAVE.

        If the DAVE session is not ready, the frame passes through
        unencrypted (passthrough mode).

        The encrypt call operates on the COMPLETE frame BEFORE RTP
        packetization. This is the correct ordering per the Node.js
        reference (WebRtcWrapper.ts sendVideoFrame).
        """
        dave_session = self._stream_conn.dave_session
        if dave_session is None or not self._stream_conn.dave_ready:
            return frame

        try:
            import davey
            encrypted = dave_session.encrypt(
                davey.MediaType.video,
                davey.Codec.h264,
                frame,
            )
            return encrypted
        except Exception as e:
            log.warning('DAVE encrypt failed, using passthrough: %s', e)
            return frame

    def _send_udp(self, packet: bytes) -> None:
        """Send a packet over UDP to the stream server's endpoint.

        Uses the stream connection's READY params IP:port — NOT the
        main voice connection's endpoint. This is the critical
        distinction that fixes error 2012.

        The UDP socket itself comes from the main voice connection
        (discord.py's VoiceClient._connection.socket), but we send
        to the stream server's address.
        """
        if self._send_callback is None:
            log.warning('No send callback configured')
            return
        try:
            self._send_callback(packet, self._target_ip, self._target_port)
        except Exception as e:
            log.warning('UDP send error: %s', e)

    def set_send_callback(self, callback) -> None:
        """Set the callback for sending UDP packets.

        The callback signature is: callback(packet, ip, port)
        - packet: complete wire packet (header + encrypted payload + nonce)
        - ip: stream server IP (from READY params)
        - port: stream server port (from READY params)
        """
        self._send_callback = callback

    def send_rtcp_sender_report(self) -> None:
        """Send an RTCP Sender Report for synchronization.

        This is sent periodically to allow receivers to synchronize
        audio and video streams.
        """
        try:
            from .rtp.serialize import build_rtcp_sr
        except ImportError:
            from rtp.serialize import build_rtcp_sr
        import time

        if self._packetizer is None:
            return

        # NTP timestamp (seconds since 1900-01-01)
        ntp_epoch = -2208988800  # offset from Unix epoch to NTP epoch
        ntp_now = time.time() - ntp_epoch
        ntp_timestamp = int(ntp_now * (2**32))

        sr = build_rtcp_sr(
            ssrc=self._stream_conn.video_ssrc,
            ntp_timestamp=ntp_timestamp,
            rtp_timestamp=self._timestamp,
            packet_count=self._packet_count,
            octet_count=self._octet_count,
        )
        self._send_udp(bytes(sr))

    def reset_sequence(self) -> None:
        """Reset the video sequence number (e.g., after reconnect)."""
        self._sequence = 0

    def encrypt_frame_for_test(self, frame: bytes) -> bytes:
        """Encrypt a frame using DAVE (for testing only)."""
        return self._dave_encrypt(frame)
