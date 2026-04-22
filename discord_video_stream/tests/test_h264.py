"""
Tests for H.264 NALU splitting and RTP packetization.
"""

import sys
import os
import struct
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from rtp.h264 import split_nalu, get_nalu_type, H264Packetizer, H264NalUnitTypes


class TestNALUSplitting(unittest.TestCase):
    """Test Annex-B frame splitting into NALUs."""

    def test_single_nalu_3byte(self):
        """Single NALU with 3-byte start code."""
        frame = b'\x00\x00\x01\x65\x88\x84\x00'
        nalus = split_nalu(frame)
        self.assertEqual(len(nalus), 1)
        self.assertEqual(nalus[0], b'\x65\x88\x84\x00')

    def test_single_nalu_4byte(self):
        """Single NALU with 4-byte start code."""
        frame = b'\x00\x00\x00\x01\x65\x88\x84\x00'
        nalus = split_nalu(frame)
        self.assertEqual(len(nalus), 1)
        self.assertEqual(nalus[0], b'\x65\x88\x84\x00')

    def test_two_nalus_3byte_then_4byte(self):
        """Two NALUs: first with 3-byte start code, second with 4-byte."""
        frame = b'\x00\x00\x01\x67\x42\x00\x00\x00\x01\x68\xce'
        # NALU 1: \x67\x42 (2 bytes, the \x00 at pos 5 is start of the 4-byte code)
        # NALU 2: \x68\xce (2 bytes)
        nalus = split_nalu(frame)
        self.assertEqual(len(nalus), 2)
        self.assertEqual(nalus[0], b'\x67\x42')
        self.assertEqual(nalus[1], b'\x68\xce')

    def test_two_nalus_4byte(self):
        """Two NALUs separated by 4-byte start codes."""
        frame = b'\x00\x00\x00\x01\x67\x42\x00\x00\x00\x00\x01\x68\xce'
        nalus = split_nalu(frame)
        self.assertEqual(len(nalus), 2)
        self.assertEqual(nalus[0], b'\x67\x42\x00')
        self.assertEqual(nalus[1], b'\x68\xce')

    def test_sps_pps_idr_keyframe(self):
        """Typical keyframe: SPS + PPS + IDR."""
        frame = (
            b'\x00\x00\x00\x01\x67' + b'\x42' * 10  # SPS (type 7)
            + b'\x00\x00\x00\x01\x68' + b'\xce' * 5   # PPS (type 8)
            + b'\x00\x00\x00\x01\x65' + b'\x88' * 20  # IDR (type 5)
        )
        nalus = split_nalu(frame)
        self.assertEqual(len(nalus), 3)
        self.assertEqual(get_nalu_type(nalus[0]), H264NalUnitTypes.SPS)
        self.assertEqual(get_nalu_type(nalus[1]), H264NalUnitTypes.PPS)
        self.assertEqual(get_nalu_type(nalus[2]), H264NalUnitTypes.IDR)

    def test_empty_frame(self):
        """Empty frame should return empty list."""
        self.assertEqual(split_nalu(b''), [])

    def test_no_start_code(self):
        """Frame without start code should be treated as single NALU."""
        frame = b'\x65\x88\x84'
        nalus = split_nalu(frame)
        self.assertEqual(len(nalus), 1)
        self.assertEqual(nalus[0], frame)


class TestNALUType(unittest.TestCase):
    """Test NALU type extraction."""

    def test_idr_type(self):
        self.assertEqual(get_nalu_type(b'\x65\x00'), 5)

    def test_sps_type(self):
        self.assertEqual(get_nalu_type(b'\x67\x00'), 7)

    def test_pps_type(self):
        self.assertEqual(get_nalu_type(b'\x68\x00'), 8)

    def test_empty_nalu(self):
        self.assertEqual(get_nalu_type(b''), -1)


class TestH264Packetizer(unittest.TestCase):
    """Test H.264 RTP packetization."""

    def test_single_nalu_packet(self):
        """Small NALU should produce a single RTP packet."""
        pkt = H264Packetizer(ssrc=12345, payload_type=101)
        pkt.set_timestamp(90000)

        # SPS NALU (small) - 6 bytes after the start code
        frame = b'\x00\x00\x00\x01\x67\x42\x00\x1e\x96\x54'
        packets = pkt.packetize_frame(frame)

        self.assertEqual(len(packets), 1)
        self.assertEqual(len(packets[0]), 12 + 6)  # header + NALU (without start code)

        # Verify RTP header: marker=1 (only packet, also last)
        self.assertEqual(packets[0][1] & 0x80, 0x80)  # marker set

        # Verify payload type
        self.assertEqual(packets[0][1] & 0x7F, 101)

    def test_fu_a_fragmentation(self):
        """Large NALU should be fragmented using FU-A."""
        pkt = H264Packetizer(ssrc=12345, payload_type=101)
        pkt.set_timestamp(90000)

        # Create a large IDR NALU (> 1300 bytes payload)
        nalu_payload = b'\x88' * 3000  # IDR NALU body
        nalu = bytes([0x65]) + nalu_payload  # IDR NALU header
        frame = b'\x00\x00\x00\x01' + nalu

        packets = pkt.packetize_frame(frame)

        # 3001 bytes / 1298 per fragment = 3 fragments
        self.assertGreater(len(packets), 1)

        # First packet: FU-A indicator + FU header + chunk
        rtp_payload = bytes(packets[0][12:])  # skip RTP header
        fu_indicator = rtp_payload[0]
        fu_header = rtp_payload[1]

        # FU indicator should be (nalu[0] & 0x60) | 28
        self.assertEqual(fu_indicator, (0x65 & 0x60) | 28)

        # First fragment: start bit set
        self.assertEqual(fu_header & 0x80, 0x80)

        # Original NAL type in lower 5 bits
        self.assertEqual(fu_header & 0x1F, 5)  # IDR

        # Last packet: marker bit set, end bit set
        last_payload = bytes(packets[-1][12:])
        last_fu_header = last_payload[1]
        self.assertEqual(last_fu_header & 0x40, 0x40)  # end bit
        self.assertEqual(packets[-1][1] & 0x80, 0x80)  # RTP marker

    def test_sequence_numbers(self):
        """Sequence numbers should increment across packets."""
        pkt = H264Packetizer(ssrc=12345, payload_type=101)
        pkt.set_timestamp(90000)

        nalu = bytes([0x65]) + b'\x88' * 3000
        frame = b'\x00\x00\x00\x01' + nalu
        packets = pkt.packetize_frame(frame)

        # Extract sequence numbers
        seqs = []
        for p in packets:
            seq = struct.unpack('>H', p[2:4])[0]
            seqs.append(seq)

        # Should be consecutive
        for i in range(1, len(seqs)):
            self.assertEqual(seqs[i], (seqs[i-1] + 1) & 0xFFFF)

    def test_timestamp_same_for_frame(self):
        """All packets in a frame should have the same timestamp."""
        pkt = H264Packetizer(ssrc=12345, payload_type=101)
        pkt.set_timestamp(90000)

        nalu = bytes([0x65]) + b'\x88' * 3000
        frame = b'\x00\x00\x00\x01' + nalu
        packets = pkt.packetize_frame(frame)

        timestamps = set()
        for p in packets:
            ts = struct.unpack('>I', p[4:8])[0]
            timestamps.add(ts)

        self.assertEqual(len(timestamps), 1)
        self.assertEqual(timestamps.pop(), 90000)

    def test_ssrc_in_header(self):
        """SSRC should be in all packets."""
        pkt = H264Packetizer(ssrc=99999, payload_type=101)
        frame = b'\x00\x00\x00\x01\x67\x42\x00'
        packets = pkt.packetize_frame(frame)

        for p in packets:
            ssrc = struct.unpack('>I', p[8:12])[0]
            self.assertEqual(ssrc, 99999)

    def test_marker_only_on_last_packet(self):
        """Marker bit should only be set on the last packet of a frame."""
        pkt = H264Packetizer(ssrc=12345, payload_type=101)
        pkt.set_timestamp(90000)

        # Multi-NALU frame: SPS + PPS + IDR (all small)
        frame = (
            b'\x00\x00\x00\x01\x67' + b'\x42' * 10
            + b'\x00\x00\x00\x01\x68' + b'\xce' * 5
            + b'\x00\x00\x00\x01\x65' + b'\x88' * 20
        )
        packets = pkt.packetize_frame(frame)
        self.assertEqual(len(packets), 3)

        # First two: no marker
        self.assertEqual(packets[0][1] & 0x80, 0)
        self.assertEqual(packets[1][1] & 0x80, 0)

        # Last: marker set
        self.assertEqual(packets[2][1] & 0x80, 0x80)

    def test_voice_recv_compatible_header(self):
        """Generated packets should parseable by voice-recv's RTPPacket."""
        pkt = H264Packetizer(ssrc=12345, payload_type=101)
        pkt.set_timestamp(90000)

        frame = b'\x00\x00\x00\x01\x65\x88\x84\x00'
        packets = pkt.packetize_frame(frame)

        hstruct = struct.Struct('>xxHII')
        for p in packets:
            seq, ts, ssrc = hstruct.unpack_from(p)
            self.assertEqual(ssrc, 12345)
            self.assertEqual(ts, 90000)
            self.assertGreaterEqual(seq, 0)
            self.assertLessEqual(seq, 65535)


if __name__ == '__main__':
    unittest.main()
