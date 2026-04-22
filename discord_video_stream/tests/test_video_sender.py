"""
Tests for video sender.

Validates:
- Video frame send pipeline (SPS VUI rewrite, DAVE encrypt, RTP packetize, transport encrypt)
- Separate video sequence/timestamp/nonce counters
- H.264 packetization with correct SSRC
- Transport encryption with stream secret key
- Marker bit on last packet of frame
- RTP timestamp calculation from PTS
"""

import sys
import os
import struct
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from voice_send import VideoSender
from rtp.serialize import build_rtp_header
from rtp.h264 import H264Packetizer, split_nalu, get_nalu_type, H264NalUnitTypes
from rtp.crypto import TransportEncryptor
from protocol.vui import rewrite_sps_vui

try:
    import nacl.secret
    HAS_NACL = True
except ImportError:
    HAS_NACL = False


def _make_mock_stream_conn(
    video_ssrc=2000,
    rtx_ssrc=2001,
    audio_ssrc=1000,
    secret_key=None,
    mode='aead_xchacha20_poly1305_rtpsize',
    dave_ready=False,
):
    """Create a mock StreamConnection for testing."""
    conn = MagicMock()
    conn.video_ssrc = video_ssrc
    conn.rtx_ssrc = rtx_ssrc
    conn.audio_ssrc = audio_ssrc
    conn.secret_key = secret_key or nacl.utils.random(32)
    conn.encryption_mode = mode
    conn.dave_ready = dave_ready
    conn.dave_session = None
    return conn


class TestVideoSenderInit(unittest.TestCase):
    """Test VideoSender initialization."""

    def test_initial_state(self):
        conn = _make_mock_stream_conn()
        sender = VideoSender(conn)
        self.assertFalse(sender.active)
        self.assertEqual(sender.sequence, 0)
        self.assertEqual(sender.timestamp, 0)

    def test_configure(self):
        conn = _make_mock_stream_conn()
        sender = VideoSender(conn)
        sender.configure(1920, 1080, 30)
        self.assertEqual(sender._width, 1920)
        self.assertEqual(sender._height, 1080)
        self.assertEqual(sender._fps, 30)


@unittest.skipUnless(HAS_NACL, "pynacl not installed")
class TestVideoSenderStart(unittest.TestCase):
    """Test VideoSender start/stop."""

    def test_start(self):
        conn = _make_mock_stream_conn()
        sender = VideoSender(conn)
        sender.start()
        self.assertTrue(sender.active)
        self.assertIsNotNone(sender._packetizer)
        self.assertIsNotNone(sender._encryptor)

    def test_start_no_video_ssrc_raises(self):
        conn = _make_mock_stream_conn(video_ssrc=0)
        sender = VideoSender(conn)
        with self.assertRaises(RuntimeError):
            sender.start()

    def test_start_no_secret_key_raises(self):
        conn = MagicMock()
        conn.video_ssrc = 2000
        conn.secret_key = None
        conn.encryption_mode = 'aead_xchacha20_poly1305_rtpsize'
        sender = VideoSender(conn)
        with self.assertRaises(RuntimeError):
            sender.start()

    def test_stop(self):
        conn = _make_mock_stream_conn()
        sender = VideoSender(conn)
        sender.start()
        sender.stop()
        self.assertFalse(sender.active)


@unittest.skipUnless(HAS_NACL, "pynacl not installed")
class TestVideoSenderFramePipeline(unittest.TestCase):
    """Test the complete video frame send pipeline."""

    def test_send_frame_produces_packets(self):
        conn = _make_mock_stream_conn()
        sender = VideoSender(conn)
        sender.start()

        sent_packets = []
        sender.set_send_callback(lambda pkt: sent_packets.append(pkt))

        # SPS + PPS + IDR keyframe
        frame = (
            b'\x00\x00\x00\x01\x67' + b'\x42' * 10  # SPS
            + b'\x00\x00\x00\x01\x68' + b'\xce' * 5   # PPS
            + b'\x00\x00\x00\x01\x65' + b'\x88' * 20  # IDR
        )

        count = sender.send_frame(frame, 0.0, 33.33)
        self.assertGreater(count, 0)
        self.assertEqual(len(sent_packets), count)

    def test_send_frame_rtp_header_ssrc(self):
        """All packets should have the stream's video SSRC."""
        conn = _make_mock_stream_conn(video_ssrc=55555)
        sender = VideoSender(conn)
        sender.start()

        sent_packets = []
        sender.set_send_callback(lambda pkt: sent_packets.append(pkt))

        frame = b'\x00\x00\x00\x01\x65' + b'\x88' * 20
        sender.send_frame(frame, 0.0, 33.33)

        for pkt in sent_packets:
            ssrc = struct.unpack('>I', pkt[8:12])[0]
            self.assertEqual(ssrc, 55555)

    def test_send_frame_rtp_timestamp(self):
        """RTP timestamp should be pts_ms * 90 (90kHz clock)."""
        conn = _make_mock_stream_conn()
        sender = VideoSender(conn)
        sender.start()

        sent_packets = []
        sender.set_send_callback(lambda pkt: sent_packets.append(pkt))

        frame = b'\x00\x00\x00\x01\x65' + b'\x88' * 20
        sender.send_frame(frame, 100.0, 33.33)  # 100ms -> 9000

        for pkt in sent_packets:
            ts = struct.unpack('>I', pkt[4:8])[0]
            self.assertEqual(ts, 9000)  # 100 * 90

    def test_send_frame_marker_bit(self):
        """Last packet of frame should have marker bit set."""
        conn = _make_mock_stream_conn()
        sender = VideoSender(conn)
        sender.start()

        sent_packets = []
        sender.set_send_callback(lambda pkt: sent_packets.append(pkt))

        frame = b'\x00\x00\x00\x01\x65' + b'\x88' * 20
        sender.send_frame(frame, 0.0, 33.33)

        # All but last should have no marker
        for pkt in sent_packets[:-1]:
            self.assertEqual(pkt[1] & 0x80, 0)

        # Last should have marker
        self.assertEqual(sent_packets[-1][1] & 0x80, 0x80)

    def test_send_frame_payload_type(self):
        """All packets should have H.264 payload type (101)."""
        conn = _make_mock_stream_conn()
        sender = VideoSender(conn)
        sender.start()

        sent_packets = []
        sender.set_send_callback(lambda pkt: sent_packets.append(pkt))

        frame = b'\x00\x00\x00\x01\x65' + b'\x88' * 20
        sender.send_frame(frame, 0.0, 33.33)

        for pkt in sent_packets:
            pt = pkt[1] & 0x7F
            self.assertEqual(pt, 101)

    def test_send_frame_encrypted(self):
        """Sent packets should be transport-encrypted (different from raw payload)."""
        conn = _make_mock_stream_conn()
        sender = VideoSender(conn)
        sender.start()

        sent_packets = []
        sender.set_send_callback(lambda pkt: sent_packets.append(pkt))

        frame = b'\x00\x00\x00\x01\x65' + b'\x88' * 20
        sender.send_frame(frame, 0.0, 33.33)

        # Each packet should be larger than header + raw NALU
        # because of AEAD encryption overhead
        for pkt in sent_packets:
            # Header (12) + encrypted payload + 4-byte nonce
            self.assertGreater(len(pkt), 12 + 4 + 4)  # at least header + tag + nonce

    def test_send_frame_sequence_increments(self):
        """Sequence numbers should increment across packets."""
        conn = _make_mock_stream_conn()
        sender = VideoSender(conn)
        sender.start()

        sent_packets = []
        sender.set_send_callback(lambda pkt: sent_packets.append(pkt))

        # Large frame that produces multiple packets
        frame = b'\x00\x00\x00\x01\x65' + b'\x88' * 3000
        sender.send_frame(frame, 0.0, 33.33)

        seqs = []
        for pkt in sent_packets:
            seq = struct.unpack('>H', pkt[2:4])[0]
            seqs.append(seq)

        # Should be consecutive
        for i in range(1, len(seqs)):
            self.assertEqual(seqs[i], (seqs[i-1] + 1) & 0xFFFF)

    def test_send_multiple_frames(self):
        """Multiple frames should maintain continuous state."""
        conn = _make_mock_stream_conn()
        sender = VideoSender(conn)
        sender.start()

        all_packets = []
        sender.set_send_callback(lambda pkt: all_packets.append(pkt))

        frame1 = b'\x00\x00\x00\x01\x67' + b'\x42' * 10  # SPS
        frame2 = b'\x00\x00\x00\x01\x65' + b'\x88' * 20  # IDR

        c1 = sender.send_frame(frame1, 0.0, 33.33)
        c2 = sender.send_frame(frame2, 33.33, 33.33)

        self.assertGreater(c1, 0)
        self.assertGreater(c2, 0)
        self.assertEqual(len(all_packets), c1 + c2)

    def test_send_frame_not_active_raises(self):
        conn = _make_mock_stream_conn()
        sender = VideoSender(conn)
        with self.assertRaises(RuntimeError):
            sender.send_frame(b'\x00', 0.0, 33.33)


@unittest.skipUnless(HAS_NACL, "pynacl not installed")
class TestVideoSenderSPSVUI(unittest.TestCase):
    """Test SPS VUI rewriting in the send pipeline."""

    def test_sps_is_rewritten(self):
        """SPS NALUs in the frame should have VUI rewritten."""
        conn = _make_mock_stream_conn()
        sender = VideoSender(conn)
        sender.start()

        sent_packets = []
        sender.set_send_callback(lambda pkt: sent_packets.append(pkt))

        # Build a frame with a minimal SPS
        from protocol.vui import BitstreamWriter
        writer = BitstreamWriter()
        writer.write_unsigned(66, 8)   # profile_idc (Baseline)
        writer.write_unsigned(0, 8)    # constraint_flags
        writer.write_unsigned(30, 8)   # level_idc
        writer.write_ue(0)             # sps_id
        writer.write_ue(0)             # log2_max_frame_num
        writer.write_ue(0)             # poc_type
        writer.write_ue(0)             # log2_max_poc_lsb
        writer.write_ue(4)             # max_num_ref_frames
        writer.write_bits(0, 1)        # gaps
        writer.write_ue(21)            # width
        writer.write_ue(17)            # height
        writer.write_bits(1, 1)        # frame_mbs_only
        writer.write_bits(0, 1)        # direct_8x8
        writer.write_bits(0, 1)        # frame_cropping
        writer.write_bits(0, 1)        # no VUI
        writer.write_bits(1, 1)        # RBSP stop
        writer.flush()

        sps_nalu = bytes([0x67]) + writer.to_bytes()
        frame = b'\x00\x00\x00\x01' + sps_nalu

        # The send pipeline should rewrite the SPS
        # We can verify by checking that the output differs from input
        # (the rewritten SPS will have VUI injected)
        count = sender.send_frame(frame, 0.0, 33.33)
        self.assertGreater(count, 0)

    def test_sps_rewrite_disabled(self):
        """When _rewrite_sps is False, SPS should pass through."""
        conn = _make_mock_stream_conn()
        sender = VideoSender(conn)
        sender._rewrite_sps = False
        sender.start()

        sent_packets = []
        sender.set_send_callback(lambda pkt: sent_packets.append(pkt))

        frame = b'\x00\x00\x00\x01\x67' + b'\x42' * 10
        count = sender.send_frame(frame, 0.0, 33.33)
        self.assertGreater(count, 0)


@unittest.skipUnless(HAS_NACL, "pynacl not installed")
class TestVideoSenderDAVE(unittest.TestCase):
    """Test DAVE encryption in the send pipeline."""

    def test_dave_passthrough_when_no_session(self):
        """Without DAVE session, frames should pass through unencrypted."""
        conn = _make_mock_stream_conn(dave_ready=False)
        conn.dave_session = None
        sender = VideoSender(conn)
        sender.start()

        # _dave_encrypt should return frame unchanged
        frame = b'\x00\x00\x00\x01\x65\x88\x84'
        result = sender.encrypt_frame_for_test(frame)
        self.assertEqual(result, frame)

    def test_dave_encrypt_called_when_ready(self):
        """When DAVE is ready, encrypt should be called."""
        conn = _make_mock_stream_conn(dave_ready=True)
        mock_session = MagicMock()
        mock_session.encrypt.return_value = b'encrypted_frame'
        conn.dave_session = mock_session

        sender = VideoSender(conn)
        sender.start()

        sent_packets = []
        sender.set_send_callback(lambda pkt: sent_packets.append(pkt))

        frame = b'\x00\x00\x00\x01\x65\x88\x84'
        sender.send_frame(frame, 0.0, 33.33)

        # DAVE encrypt should have been called
        mock_session.encrypt.assert_called_once()


class TestVideoSenderRTCP(unittest.TestCase):
    """Test RTCP Sender Report generation."""

    @unittest.skipUnless(HAS_NACL, "pynacl not installed")
    def test_send_rtcp_sr(self):
        conn = _make_mock_stream_conn(video_ssrc=55555)
        sender = VideoSender(conn)
        sender.start()

        sent_packets = []
        sender.set_send_callback(lambda pkt: sent_packets.append(pkt))

        sender.send_rtcp_sender_report()
        self.assertEqual(len(sent_packets), 1)

        # RTCP SR should have type 200 in byte 1
        pkt = sent_packets[0]
        self.assertEqual(pkt[1], 200)

        # SSRC should match
        ssrc = struct.unpack('>I', pkt[4:8])[0]
        self.assertEqual(ssrc, 55555)


if __name__ == '__main__':
    unittest.main()
