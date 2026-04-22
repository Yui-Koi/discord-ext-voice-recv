"""
Tests for protocol types and SPS VUI rewriter.
"""

import sys
import os
import struct
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from protocol.types import (
    CodecConfig,
    CODEC_OPUS,
    CODEC_H264,
    generate_stream_key,
    parse_stream_key,
    build_video_payload,
    build_video_off_payload,
    VideoAttributes,
    ALL_CODECS,
)
from protocol.vui import (
    BitstreamReader,
    BitstreamWriter,
    rewrite_sps_vui,
)


class TestCodecConfig(unittest.TestCase):
    """Test codec configuration."""

    def test_opus_config(self):
        self.assertEqual(CODEC_OPUS.name, 'opus')
        self.assertEqual(CODEC_OPUS.payload_type, 120)
        self.assertEqual(CODEC_OPUS.clock_rate, 48000)
        self.assertIsNone(CODEC_OPUS.rtx_payload_type)

    def test_h264_config(self):
        self.assertEqual(CODEC_H264.name, 'H264')
        self.assertEqual(CODEC_H264.payload_type, 101)
        self.assertEqual(CODEC_H264.rtx_payload_type, 102)
        self.assertEqual(CODEC_H264.clock_rate, 90000)

    def test_to_dict(self):
        d = CODEC_H264.to_dict()
        self.assertEqual(d['name'], 'H264')
        self.assertEqual(d['payload_type'], 101)
        self.assertEqual(d['rtx_payload_type'], 102)
        self.assertTrue(d['encode'])
        self.assertTrue(d['decode'])

    def test_all_codecs_present(self):
        names = [c.name for c in ALL_CODECS]
        self.assertIn('opus', names)
        self.assertIn('H264', names)
        self.assertIn('H265', names)
        self.assertIn('VP8', names)
        self.assertIn('VP9', names)
        self.assertIn('AV1', names)

    def test_codec_payload_types(self):
        """Verify codec PTs match Node.js reference."""
        self.assertEqual(CODEC_OPUS.payload_type, 120)
        self.assertEqual(CODEC_H264.payload_type, 101)
        self.assertEqual(CODEC_H264.rtx_payload_type, 102)


class TestStreamKey(unittest.TestCase):
    """Test stream key generation and parsing."""

    def test_guild_key(self):
        key = generate_stream_key('guild', '123456', '789012', '999')
        self.assertEqual(key, 'guild:123456:789012:999')

    def test_call_key(self):
        key = generate_stream_key('call', None, '789012', '999')
        self.assertEqual(key, 'call:789012:999')

    def test_parse_guild_key(self):
        result = parse_stream_key('guild:41771983423143937:123456:789012')
        self.assertEqual(result['type'], 'guild')
        self.assertEqual(result['guild_id'], '41771983423143937')
        self.assertEqual(result['channel_id'], '123456')
        self.assertEqual(result['user_id'], '789012')

    def test_parse_call_key(self):
        result = parse_stream_key('call:123456:789012')
        self.assertEqual(result['type'], 'call')
        self.assertIsNone(result['guild_id'])
        self.assertEqual(result['channel_id'], '123456')
        self.assertEqual(result['user_id'], '789012')

    def test_invalid_key_type(self):
        with self.assertRaises(ValueError):
            parse_stream_key('invalid:123:456')

    def test_roundtrip(self):
        key = generate_stream_key('guild', '100', '200', '300')
        parsed = parse_stream_key(key)
        self.assertEqual(parsed['guild_id'], '100')
        self.assertEqual(parsed['channel_id'], '200')
        self.assertEqual(parsed['user_id'], '300')


class TestVideoPayload(unittest.TestCase):
    """Test VIDEO opcode payload construction."""

    def test_video_on_payload(self):
        attrs = VideoAttributes(width=1920, height=1080, fps=30)
        payload = build_video_payload(
            audio_ssrc=1000, video_ssrc=2000, rtx_ssrc=2001, attrs=attrs,
        )

        self.assertEqual(payload['audio_ssrc'], 1000)
        self.assertEqual(payload['video_ssrc'], 2000)
        self.assertEqual(payload['rtx_ssrc'], 2001)

        streams = payload['streams']
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]['rid'], '100')
        self.assertEqual(streams[0]['ssrc'], 2000)
        self.assertEqual(streams[0]['max_resolution']['width'], 1920)
        self.assertEqual(streams[0]['max_resolution']['height'], 1080)
        self.assertEqual(streams[0]['max_framerate'], 30)

    def test_video_off_payload(self):
        payload = build_video_off_payload(audio_ssrc=1000)
        self.assertEqual(payload['audio_ssrc'], 1000)
        self.assertEqual(payload['video_ssrc'], 0)
        self.assertEqual(payload['streams'], [])


class TestBitstreamReader(unittest.TestCase):
    """Test Exp-Golomb bitstream reader."""

    def test_read_bits(self):
        # 0b10110011 = 0xB3
        reader = BitstreamReader(bytes([0xB3]))
        self.assertEqual(reader.read_bits(4), 0b1011)
        self.assertEqual(reader.read_bits(4), 0b0011)

    def test_read_unsigned(self):
        reader = BitstreamReader(bytes([0xFF, 0x00]))
        self.assertEqual(reader.read_unsigned(8), 255)
        self.assertEqual(reader.read_unsigned(8), 0)

    def test_read_signed_positive(self):
        # 0b01100000 -> 3 bits = 0b011 = 3
        reader = BitstreamReader(bytes([0x60]))
        self.assertEqual(reader.read_signed(3), 3)

    def test_read_signed_negative(self):
        # 0b11000000 -> 3 bits = 0b110 = 6, which is -2 in 3-bit signed
        reader = BitstreamReader(bytes([0xC0]))
        self.assertEqual(reader.read_signed(3), -2)

    def test_read_ue(self):
        # Exp-Golomb ue(2): code_num=3=0b11 -> 0 11 -> byte: 0b01100000 = 0x60
        reader = BitstreamReader(bytes([0x60]))
        self.assertEqual(reader.read_ue(), 2)

    def test_read_ue_zero(self):
        # 0 -> leading0=0, value=1 -> bits: 1
        reader = BitstreamReader(bytes([0x80]))
        self.assertEqual(reader.read_ue(), 0)

    def test_read_se_positive(self):
        # se(1) = ue(1), se(2) = ue(3), se(3) = ue(5)
        # se(1): 0b10... -> reader at bit 0
        reader = BitstreamReader(bytes([0x80]))
        self.assertEqual(reader.read_se(), 0)  # ue(0) -> se(0)

    def test_emulation_prevention(self):
        """0x00 00 03 XX should skip the 03 byte."""
        # 0x00 00 03 FF -> should read as 0x00 00 FF
        reader = BitstreamReader(bytes([0x00, 0x00, 0x03, 0xFF]))
        # Read 3 bytes worth of bits
        b0 = reader.read_bits(8)
        b1 = reader.read_bits(8)
        b2 = reader.read_bits(8)
        self.assertEqual(b0, 0x00)
        self.assertEqual(b1, 0x00)
        self.assertEqual(b2, 0xFF)  # 03 was skipped


class TestBitstreamWriter(unittest.TestCase):
    """Test Exp-Golomb bitstream writer."""

    def test_write_bits(self):
        writer = BitstreamWriter()
        writer.write_bits(0b1011, 4)
        writer.write_bits(0b0011, 4)
        writer.flush()
        # Result may have trailing zeros from incomplete final byte
        data = writer.to_bytes()
        self.assertEqual(data[0], 0xB3)

    def test_write_ue(self):
        writer = BitstreamWriter()
        writer.write_ue(3)  # should produce bits: 011
        writer.flush()
        data = writer.to_bytes()
        # Read back
        reader = BitstreamReader(data)
        self.assertEqual(reader.read_ue(), 3)

    def test_write_se(self):
        writer = BitstreamWriter()
        writer.write_se(-2)  # se(-2) = ue(4)
        writer.flush()
        reader = BitstreamReader(writer.to_bytes())
        self.assertEqual(reader.read_se(), -2)

    def test_ue_roundtrip(self):
        """Write and read back various ue values."""
        for val in [0, 1, 3, 7, 15, 100, 255]:
            writer = BitstreamWriter()
            writer.write_ue(val)
            writer.flush()
            reader = BitstreamReader(writer.to_bytes())
            self.assertEqual(reader.read_ue(), val, f'Failed for {val}')

    def test_se_roundtrip(self):
        """Write and read back various se values."""
        for val in [-5, -2, -1, 0, 1, 2, 5]:
            writer = BitstreamWriter()
            writer.write_se(val)
            writer.flush()
            reader = BitstreamReader(writer.to_bytes())
            self.assertEqual(reader.read_se(), val, f'Failed for {val}')


class TestSPSVUIRewriter(unittest.TestCase):
    """Test SPS VUI rewriting."""

    def _make_sps(self, profile_idc=66, level_idc=30, extra_vui=False):
        """Create a minimal SPS NALU for testing.

        profile_idc=66 is Baseline (no high profile fields).
        Creates a minimal valid SPS with no VUI or optional VUI.
        """
        writer = BitstreamWriter()

        # profile_idc
        writer.write_unsigned(profile_idc, 8)
        # constraint_flags (all zero)
        writer.write_unsigned(0, 8)
        # level_idc
        writer.write_unsigned(level_idc, 8)
        # seq_parameter_set_id = 0
        writer.write_ue(0)
        # log2_max_frame_num_minus4 = 0
        writer.write_ue(0)
        # pic_order_cnt_type = 0
        writer.write_ue(0)
        # log2_max_pic_order_cnt_lsb_minus4 = 0
        writer.write_ue(0)
        # max_num_ref_frames = 4
        writer.write_ue(4)
        # gaps_in_frame_num_value_allowed_flag = 0
        writer.write_bits(0, 1)
        # pic_width_in_mbs_minus1 = 21 (352 pixels = 22 mbs)
        writer.write_ue(21)
        # pic_height_in_map_units_minus1 = 17 (288 pixels = 18 mbs)
        writer.write_ue(17)
        # frame_mbs_only_flag = 1
        writer.write_bits(1, 1)
        # direct_8x8_inference_flag = 0
        writer.write_bits(0, 1)
        # frame_cropping_flag = 0
        writer.write_bits(0, 1)
        # vui_parameters_present_flag
        writer.write_bits(1 if extra_vui else 0, 1)

        if extra_vui:
            # Minimal VUI: aspect_ratio=0, overscan=0, video_signal=0,
            # chroma_loc=0, timing=0, nal_hrd=0, vcl_hrd=0, pic_struct=0
            writer.write_bits(0, 1)  # aspect_ratio_info
            writer.write_bits(0, 1)  # overscan_info
            writer.write_bits(0, 1)  # video_signal_type
            writer.write_bits(0, 1)  # chroma_loc_info
            writer.write_bits(0, 1)  # timing_info
            writer.write_bits(0, 1)  # nal_hrd
            writer.write_bits(0, 1)  # vcl_hrd
            writer.write_bits(0, 1)  # pic_struct
            writer.write_bits(0, 1)  # bitstream_restriction

        # RBSP stop bit
        writer.write_bits(1, 1)
        writer.flush()

        nalu_data = writer.to_bytes()
        # Prepend NAL header byte (type 7 = SPS)
        return bytes([0x67]) + nalu_data

    def test_rewrites_sps_without_vui(self):
        """SPS without VUI should get VUI injected."""
        sps = self._make_sps(extra_vui=False)
        result = rewrite_sps_vui(sps)

        # NAL header preserved
        self.assertEqual(result[0], sps[0])

        # Result should be longer (VUI was injected)
        self.assertGreater(len(result), len(sps))

        # Parse the result to verify VUI was added
        reader = BitstreamReader(result[1:])
        reader.read_unsigned(8)   # profile_idc
        reader.read_unsigned(8)   # constraint_flags
        reader.read_unsigned(8)   # level_idc
        reader.read_ue()          # seq_parameter_set_id
        reader.read_ue()          # log2_max_frame_num_minus4
        reader.read_ue()          # pic_order_cnt_type
        # (skip rest of poc type 0 fields)
        reader.read_ue()          # log2_max_pic_order_cnt_lsb_minus4
        reader.read_ue()          # max_num_ref_frames
        reader.read_bits(1)       # gaps_in_frame_num_value_allowed_flag
        reader.read_ue()          # pic_width_in_mbs_minus1
        reader.read_ue()          # pic_height_in_map_units_minus1
        reader.read_bits(1)       # frame_mbs_only_flag
        reader.read_bits(1)       # direct_8x8_inference_flag
        frame_cropping = reader.read_bits(1)

        # Skip cropping params if present
        # (should not be present for our test SPS)

        # VUI should be present
        vui_present = reader.read_bits(1)
        self.assertEqual(vui_present, 1)

    def test_rewrites_sps_with_existing_vui(self):
        """SPS with existing VUI should have bitstream restriction rewritten."""
        sps = self._make_sps(extra_vui=True)
        result = rewrite_sps_vui(sps)

        # NAL header preserved
        self.assertEqual(result[0], sps[0])
        # Result should be different
        self.assertNotEqual(result, sps)

    def test_preserves_profile_fields(self):
        """SPS fields before VUI should be preserved."""
        sps = self._make_sps(profile_idc=66, level_idc=30)
        result = rewrite_sps_vui(sps)

        # Parse result and check profile/level
        reader = BitstreamReader(result[1:])
        profile = reader.read_unsigned(8)
        self.assertEqual(profile, 66)

        constraints = reader.read_unsigned(8)
        self.assertEqual(constraints, 0)

        level = reader.read_unsigned(8)
        self.assertEqual(level, 30)

    def test_empty_nalu_returns_empty(self):
        self.assertEqual(rewrite_sps_vui(b''), b'')

    def test_max_num_reorder_frames_is_zero(self):
        """The rewritten SPS must have max_num_reorder_frames = 0."""
        sps = self._make_sps()
        result = rewrite_sps_vui(sps)

        # We need to parse deep into the VUI to find max_num_reorder_frames
        # For the injected VUI case, the path is:
        # ... vui_present=1 -> aspect=0, overscan=0, video_signal=0,
        # chroma_loc=0, timing=0, nal_hrd=0, vcl_hrd=0, pic_struct=0,
        # bitstream_restriction=1 -> motion_vectors=1, max_bytes=2,
        # max_bits=1, log2_h=16, log2_v=16, max_reorder=0, max_buffering=4

        # Just verify the result is parseable and contains the restriction
        reader = BitstreamReader(result[1:])

        # Skip to VUI section
        reader.read_unsigned(8)   # profile
        reader.read_unsigned(8)   # constraints
        reader.read_unsigned(8)   # level
        reader.read_ue()          # sps_id
        reader.read_ue()          # log2_max_frame_num
        reader.read_ue()          # poc_type
        reader.read_ue()          # log2_max_poc_lsb
        reader.read_ue()          # max_num_ref_frames
        reader.read_bits(1)       # gaps
        reader.read_ue()          # width
        reader.read_ue()          # height
        reader.read_bits(1)       # frame_mbs_only
        reader.read_bits(1)       # direct_8x8
        reader.read_bits(1)       # frame_cropping

        vui = reader.read_bits(1)
        self.assertEqual(vui, 1)

        # Skip VUI to bitstream restriction
        reader.read_bits(1)  # aspect_ratio
        reader.read_bits(1)  # overscan
        reader.read_bits(1)  # video_signal
        reader.read_bits(1)  # chroma_loc
        reader.read_bits(1)  # timing
        reader.read_bits(1)  # nal_hrd
        reader.read_bits(1)  # vcl_hrd
        reader.read_bits(1)  # pic_struct

        bitstream_restriction = reader.read_bits(1)
        self.assertEqual(bitstream_restriction, 1)

        reader.read_bits(1)  # motion_vectors
        reader.read_ue()     # max_bytes_per_pic
        reader.read_ue()     # max_bits_per_mb
        reader.read_ue()     # log2_max_mv_h
        reader.read_ue()     # log2_max_mv_v

        max_reorder = reader.read_ue()
        self.assertEqual(max_reorder, 0)

        # max_dec_frame_buffering should equal max_num_ref_frames (4)
        max_buffering = reader.read_ue()
        self.assertEqual(max_buffering, 4)


if __name__ == '__main__':
    unittest.main()
