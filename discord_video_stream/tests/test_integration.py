"""
End-to-end integration test for the send pipeline.

Simulates: frame -> packetize -> transport encrypt -> wire format
Validates the complete flow without Discord connection.
"""

import sys
import os
import struct
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from rtp.serialize import build_rtp_header, build_rtp_packet
from rtp.h264 import H264Packetizer, split_nalu, get_nalu_type, H264NalUnitTypes
from rtp.crypto import TransportEncryptor

try:
    import nacl.secret
    HAS_NACL = True
except ImportError:
    HAS_NACL = False


@unittest.skipUnless(HAS_NACL, "pynacl not installed")
class TestFullPipeline(unittest.TestCase):
    """Test the complete send pipeline end-to-end."""

    def test_keyframe_pipeline(self):
        """Simulate sending a keyframe (SPS+PPS+IDR) through the full pipeline."""
        secret_key = nacl.utils.random(32)
        encryptor = TransportEncryptor(secret_key, 'aead_xchacha20_poly1305_rtpsize')
        packetizer = H264Packetizer(ssrc=55555, payload_type=101)
        packetizer.set_timestamp(90000)

        # Build a synthetic keyframe
        sps = b'\x67' + b'\x42' * 20   # SPS NALU
        pps = b'\x68' + b'\xce' * 10   # PPS NALU
        idr = b'\x65' + b'\x88' * 50   # IDR NALU (small, single packet)
        frame = (
            b'\x00\x00\x00\x01' + sps
            + b'\x00\x00\x00\x01' + pps
            + b'\x00\x00\x00\x01' + idr
        )

        # Step 1: Packetize
        packets = packetizer.packetize_frame(frame)
        self.assertEqual(len(packets), 3)  # 3 small NALUs = 3 packets

        # Step 2: Encrypt each packet
        wire_packets = []
        for pkt in packets:
            header = bytes(pkt[:12])
            payload = bytes(pkt[12:])
            encrypted = encryptor.encrypt_rtp(header, payload)
            wire_packets.append(header + encrypted)

        # Step 3: Verify wire packets
        for i, wp in enumerate(wire_packets):
            # Each wire packet should be header + encrypted_payload + nonce_counter
            self.assertGreater(len(wp), 12)
            # Header should be parseable
            seq, ts, ssrc = struct.unpack('>xxHII', wp[:12])
            self.assertEqual(ssrc, 55555)
            self.assertEqual(ts, 90000)

        # Step 4: Decrypt each wire packet and verify NALU types
        for i, wp in enumerate(wire_packets):
            header = bytes(wp[:12])
            encrypted_data = wp[12:]

            nonce_counter_bytes = encrypted_data[-4:]
            ciphertext = encrypted_data[:-4]

            nonce = bytearray(24)
            nonce[:4] = nonce_counter_bytes

            box = nacl.secret.Aead(secret_key)
            decrypted = box.decrypt(bytes(ciphertext), header, bytes(nonce))

            # Verify NALU type
            nalu_type = get_nalu_type(decrypted)
            if i == 0:
                self.assertEqual(nalu_type, H264NalUnitTypes.SPS)
            elif i == 1:
                self.assertEqual(nalu_type, H264NalUnitTypes.PPS)
            elif i == 2:
                self.assertEqual(nalu_type, H264NalUnitTypes.IDR)

    def test_large_frame_fua_pipeline(self):
        """Test FU-A fragmentation through encryption."""
        secret_key = nacl.utils.random(32)
        encryptor = TransportEncryptor(secret_key, 'aead_xchacha20_poly1305_rtpsize')
        packetizer = H264Packetizer(ssrc=12345, payload_type=101)
        packetizer.set_timestamp(270000)

        # Large IDR frame that needs FU-A
        idr_nalu = bytes([0x65]) + b'\x88' * 5000
        frame = b'\x00\x00\x00\x01' + idr_nalu

        packets = packetizer.packetize_frame(frame)
        self.assertGreater(len(packets), 1)  # definitely fragmented

        # Encrypt and verify all packets
        for pkt in packets:
            header = bytes(pkt[:12])
            payload = bytes(pkt[12:])
            encrypted = encryptor.encrypt_rtp(header, payload)
            self.assertGreater(len(encrypted), len(payload))  # encrypted is larger

        # Only last packet should have marker bit
        for pkt in packets[:-1]:
            self.assertEqual(pkt[1] & 0x80, 0)
        self.assertEqual(packets[-1][1] & 0x80, 0x80)

    def test_multiple_frames_sequence_increments(self):
        """Multiple frames should maintain continuous sequence numbers."""
        packetizer = H264Packetizer(ssrc=12345, payload_type=101)

        frame1 = b'\x00\x00\x00\x01\x67\x42\x00'  # SPS
        frame2 = b'\x00\x00\x00\x01\x65\x88\x84'  # IDR

        pkt1 = packetizer.packetize_frame(frame1)
        pkt2 = packetizer.packetize_frame(frame2)

        seq1 = struct.unpack('>H', pkt1[0][2:4])[0]
        seq2 = struct.unpack('>H', pkt2[0][2:4])[0]
        self.assertEqual(seq2, (seq1 + 1) & 0xFFFF)

    def test_voice_recv_roundtrip_compatibility(self):
        """Generated packets should be parseable by voice-recv's RTPPacket format."""
        packetizer = H264Packetizer(ssrc=99999, payload_type=101)
        packetizer.set_timestamp(180000)

        frame = b'\x00\x00\x00\x01\x65' + b'\xff' * 3000  # large IDR
        packets = packetizer.packetize_frame(frame)

        hstruct = struct.Struct('>xxHII')
        for pkt in packets:
            # Parse using voice-recv's exact struct
            seq, ts, ssrc = hstruct.unpack_from(pkt)
            self.assertEqual(ssrc, 99999)
            self.assertEqual(ts, 180000)
            self.assertGreaterEqual(seq, 0)
            self.assertLessEqual(seq, 65535)

            # Verify version bits (should be 2)
            self.assertEqual(pkt[0] >> 6, 2)

            # Verify payload type
            self.assertEqual(pkt[1] & 0x7F, 101)


if __name__ == '__main__':
    unittest.main()
