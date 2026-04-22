"""
H.264 SPS VUI rewriter for Discord compatibility.

Ports the WebRTC SPS VUI rewriter (originally C++, then TypeScript) to Python.
Modifies H.264 Sequence Parameter Set NALUs to add or rewrite the Video
Usability Information section for Discord's decoder requirements.

Key changes:
- Force bitstream_restriction_flag = 1
- Force max_num_reorder_frames = 0 (no B-frame reordering)
- Set max_dec_frame_buffering = max_num_ref_frames
- Strip video_signal_type information

Reference: webrtc.googlesource.com/src/+/5f2c9278f35e47ff72eb191669d473b7400c9f3e
           discord-video-stream/src/client/processing/SPSVUIRewriter.ts
           discord-video-stream/src/client/processing/AnnexBBitstreamReaderWriter.ts
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import List

__all__ = [
    'rewrite_sps_vui',
    'BitstreamReader',
    'BitstreamWriter',
]


# High profiles that have extra SPS fields after profile_idc
HIGH_PROFILES = frozenset([
    100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 144,
])


class BitstreamReader:
    """Exp-Golomb bitstream reader with emulation prevention byte handling.

    Reads from a byte buffer bit-by-bit, automatically skipping
    0x000003 emulation prevention sequences (0x00 00 03 xx -> skip the 03).
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._byte_offset = 0
        self._bit_offset = 0

    def read_bits(self, count: int) -> int:
        """Read `count` bits from the stream."""
        if count == 0:
            return 0

        result = 0

        while count > 0:
            if self._byte_offset >= len(self._data):
                raise ValueError('Read past end of bitstream')

            # Skip emulation prevention bytes (0x00 00 03)
            if (
                self._bit_offset == 0
                and self._byte_offset >= 2
                and self._data[self._byte_offset - 2] == 0
                and self._data[self._byte_offset - 1] == 0
                and self._data[self._byte_offset] == 3
            ):
                self._byte_offset += 1

            if self._bit_offset == 0 and count >= 8:
                # Byte-aligned, read whole byte
                result = (result << 8) | self._data[self._byte_offset]
                self._byte_offset += 1
                count -= 8
            else:
                # Read partial bits
                num_to_read = min(count, 8 - self._bit_offset)
                mask = (1 << num_to_read) - 1
                shift = 8 - self._bit_offset - num_to_read
                new_bits = (self._data[self._byte_offset] >> shift) & mask
                result = (result << num_to_read) | new_bits
                count -= num_to_read
                self._bit_offset += num_to_read

                if self._bit_offset == 8:
                    self._bit_offset = 0
                    self._byte_offset += 1

        return result

    def read_unsigned(self, bits: int) -> int:
        return self.read_bits(bits)

    def read_signed(self, bits: int) -> int:
        unsigned = self.read_unsigned(bits)
        if unsigned & (1 << (bits - 1)):
            return unsigned - (1 << bits)
        return unsigned

    def read_ue(self) -> int:
        """Read unsigned Exp-Golomb coded value."""
        leading_zeros = 0
        while self.read_bits(1) == 0:
            leading_zeros += 1
        return (1 << leading_zeros) + self.read_bits(leading_zeros) - 1

    def read_se(self) -> int:
        """Read signed Exp-Golomb coded value."""
        ue = self.read_ue()
        if ue % 2 == 0:
            return ue // -2
        return (ue + 1) // 2


class BitstreamWriter:
    """Exp-Golomb bitstream writer with emulation prevention byte handling.

    Writes bits to an internal buffer, automatically inserting 0x03
    emulation prevention bytes when needed.
    """

    def __init__(self) -> None:
        self._data: List[int] = []
        self._pending_byte = 0
        self._bit_offset = 0

    def to_bytes(self) -> bytes:
        """Flush and return the complete bitstream."""
        return bytes(self._data)

    def flush(self) -> None:
        """Write the pending byte and reset.

        Insert emulation prevention byte (0x03) when the pending byte
        would create a 0x00 00 00, 0x00 00 01, 0x00 00 02, or 0x00 00 03
        sequence in the output.
        """
        if (
            self._pending_byte <= 3
            and len(self._data) >= 2
            and self._data[-1] == 0
            and self._data[-2] == 0
        ):
            self._data.append(3)  # emulation prevention
        self._data.append(self._pending_byte)
        self._pending_byte = 0
        self._bit_offset = 0

    def write_bits(self, bits: int, count: int) -> None:
        """Write `count` bits from `bits` to the stream."""
        while count > 0:
            if self._bit_offset == 0:
                if count >= 8:
                    # Byte-aligned, write whole byte
                    self._pending_byte = (bits >> (count - 8)) & 0xFF
                    count -= 8
                    self.flush()
                else:
                    # Less than 1 byte remaining
                    mask = (1 << count) - 1
                    self._pending_byte |= (bits & mask) << (8 - count)
                    self._bit_offset = count
                    count = 0
            else:
                # Write enough bits to become byte-aligned
                num_to_write = min(8 - self._bit_offset, count)
                shift = count - num_to_write
                mask = (1 << num_to_write) - 1
                bits_to_write = (bits >> shift) & mask
                self._pending_byte |= bits_to_write << (8 - self._bit_offset - num_to_write)
                count -= num_to_write
                self._bit_offset += num_to_write

                if self._bit_offset == 8:
                    self._bit_offset = 0
                    self.flush()

    def write_unsigned(self, num: int, count: int) -> None:
        """Write an unsigned integer as `count` bits."""
        self.write_bits(num, count)

    def write_signed(self, num: int, count: int) -> None:
        """Write a signed integer as `count` bits (two's complement)."""
        if count <= 0:
            return
        mask = (1 << count) - 1 if count < 32 else 0xFFFFFFFF
        unsigned = num & mask
        self.write_bits(unsigned, count)

    def write_ue(self, num: int) -> None:
        """Write an unsigned Exp-Golomb coded value."""
        if num < 0:
            raise ValueError('write_ue requires non-negative value')
        num += 1
        bit_count = num.bit_length()
        self.write_bits(0, bit_count - 1)
        self.write_bits(num, bit_count)

    def write_se(self, num: int) -> None:
        """Write a signed Exp-Golomb coded value.

        Mapping: num <= 0 -> ue(-2 * num), num > 0 -> ue(2 * num - 1)
        se(0) = ue(0), se(-1) = ue(2), se(1) = ue(1)
        """
        if num <= 0:
            self.write_ue(-2 * num)
        else:
            self.write_ue(2 * num - 1)


def rewrite_sps_vui(nalu: bytes) -> bytes:
    """Rewrite an H.264 SPS NAL unit to fix VUI parameters for Discord.

    Input: Complete SPS NALU (including the 1-byte NAL header).
    Output: Rewritten SPS NALU with corrected VUI section.

    Changes:
    - Forces bitstream_restriction_flag = 1
    - Forces max_num_reorder_frames = 0
    - Sets max_dec_frame_buffering = max_num_ref_frames
    - Strips video_signal_type (sets present flag to 0)
    """
    if not nalu:
        return nalu

    reader = BitstreamReader(nalu[1:])  # skip NAL header byte
    writer = BitstreamWriter()

    # NAL header byte is copied at the end (after writer.to_bytes())

    profile_idc = reader.read_unsigned(8)
    writer.write_unsigned(profile_idc, 8)

    constraint_flags = reader.read_unsigned(8)
    writer.write_unsigned(constraint_flags, 8)

    level_idc = reader.read_unsigned(8)
    writer.write_unsigned(level_idc, 8)

    seq_parameter_set_id = reader.read_ue()
    writer.write_ue(seq_parameter_set_id)

    # High profile additional fields
    if profile_idc in HIGH_PROFILES:
        chroma_format_idc = reader.read_ue()
        writer.write_ue(chroma_format_idc)

        if chroma_format_idc == 3:
            separate_colour_plane_flag = reader.read_bits(1)
            writer.write_bits(separate_colour_plane_flag, 1)

        bit_depth_luma_minus8 = reader.read_ue()
        writer.write_ue(bit_depth_luma_minus8)

        bit_depth_chroma_minus8 = reader.read_ue()
        writer.write_ue(bit_depth_chroma_minus8)

        qpprime_y_zero_transform_bypass_flag = reader.read_bits(1)
        writer.write_bits(qpprime_y_zero_transform_bypass_flag, 1)

        seq_scaling_matrix_present_flag = reader.read_bits(1)
        writer.write_bits(seq_scaling_matrix_present_flag, 1)

        if seq_scaling_matrix_present_flag:
            scaling_count = 12 if chroma_format_idc == 3 else 8
            for i in range(scaling_count):
                seq_scaling_list_present_flag = reader.read_bits(1)
                writer.write_bits(seq_scaling_list_present_flag, 1)
                if seq_scaling_list_present_flag:
                    size = 64 if i >= 6 else 16
                    last_scale = 8
                    next_scale = 8
                    for _ in range(size):
                        delta = reader.read_se()
                        writer.write_se(delta)
                        next_scale = (last_scale + delta + 256) % 256
                        if next_scale != 0:
                            last_scale = next_scale

    log2_max_frame_num_minus4 = reader.read_ue()
    writer.write_ue(log2_max_frame_num_minus4)

    pic_order_cnt_type = reader.read_ue()
    writer.write_ue(pic_order_cnt_type)

    if pic_order_cnt_type == 0:
        log2_max_pic_order_cnt_lsb_minus4 = reader.read_ue()
        writer.write_ue(log2_max_pic_order_cnt_lsb_minus4)
    elif pic_order_cnt_type == 1:
        delta_pic_order_always_zero_flag = reader.read_bits(1)
        writer.write_bits(delta_pic_order_always_zero_flag, 1)

        offset_for_non_ref_pic = reader.read_se()
        writer.write_se(offset_for_non_ref_pic)

        offset_for_top_to_bottom_field = reader.read_se()
        writer.write_se(offset_for_top_to_bottom_field)

        num_ref_frames_in_pic_order_cnt_cycle = reader.read_ue()
        writer.write_ue(num_ref_frames_in_pic_order_cnt_cycle)

        for _ in range(num_ref_frames_in_pic_order_cnt_cycle):
            offset_for_ref_frame = reader.read_se()
            writer.write_se(offset_for_ref_frame)

    max_num_ref_frames = reader.read_ue()
    writer.write_ue(max_num_ref_frames)

    gaps_in_frame_num_value_allowed_flag = reader.read_bits(1)
    writer.write_bits(gaps_in_frame_num_value_allowed_flag, 1)

    pic_width_in_mbs_minus1 = reader.read_ue()
    writer.write_ue(pic_width_in_mbs_minus1)

    pic_height_in_map_units_minus1 = reader.read_ue()
    writer.write_ue(pic_height_in_map_units_minus1)

    frame_mbs_only_flag = reader.read_bits(1)
    writer.write_bits(frame_mbs_only_flag, 1)

    if frame_mbs_only_flag == 0:
        mb_adaptive_frame_field_flag = reader.read_bits(1)
        writer.write_bits(mb_adaptive_frame_field_flag, 1)

    direct_8x8_inference_flag = reader.read_bits(1)
    writer.write_bits(direct_8x8_inference_flag, 1)

    frame_cropping_flag = reader.read_bits(1)
    writer.write_bits(frame_cropping_flag, 1)

    if frame_cropping_flag:
        for _ in range(4):
            val = reader.read_ue()
            writer.write_ue(val)

    # VUI parameters
    vui_parameters_present_flag = reader.read_bits(1)
    writer.write_bits(1, 1)  # force VUI present

    if not vui_parameters_present_flag:
        # No VUI exists, inject a minimal one with just bitstream restriction
        _write_empty_vui(writer, max_num_ref_frames)
    else:
        _rewrite_existing_vui(reader, writer, max_num_ref_frames)

    # RBSP stop bit
    writer.write_bits(1, 1)
    writer.flush()

    return bytes([nalu[0]]) + writer.to_bytes()


def _write_empty_vui(writer: BitstreamWriter, max_num_ref_frames: int) -> None:
    """Write a minimal VUI section with just bitstream restriction."""
    # aspect_ratio_info_present_flag, overscan_info_present_flag: u(1) each
    writer.write_bits(0, 2)

    # video_signal_type_present_flag: u(1) -> 0 (strip)
    writer.write_bits(0, 1)

    # chroma_loc_info_present_flag, timing_info_present_flag,
    # nal_hrd_parameters_present_flag, vcl_hrd_parameters_present_flag,
    # pic_struct_present_flag: u(1) each
    writer.write_bits(0, 5)

    # bitstream_restriction_flag: u(1) -> 1
    writer.write_bits(1, 1)

    _write_bitstream_restriction(writer, max_num_ref_frames)


def _rewrite_existing_vui(
    reader: BitstreamReader,
    writer: BitstreamWriter,
    max_num_ref_frames: int,
) -> None:
    """Parse and rewrite an existing VUI section."""
    # aspect_ratio_info_present_flag
    aspect_ratio_info_present_flag = reader.read_bits(1)
    writer.write_bits(aspect_ratio_info_present_flag, 1)

    if aspect_ratio_info_present_flag:
        aspect_ratio_idc = reader.read_unsigned(8)
        writer.write_unsigned(aspect_ratio_idc, 8)
        if aspect_ratio_idc == 255:
            sar_width = reader.read_unsigned(16)
            writer.write_unsigned(sar_width, 16)
            sar_height = reader.read_unsigned(16)
            writer.write_unsigned(sar_height, 16)

    # overscan_info_present_flag
    overscan_info_present_flag = reader.read_bits(1)
    writer.write_bits(overscan_info_present_flag, 1)

    if overscan_info_present_flag:
        overscan_appropriate_flag = reader.read_bits(1)
        writer.write_bits(overscan_appropriate_flag, 1)

    # video_signal_type: read but strip (write 0)
    video_signal_type_present_flag = reader.read_bits(1)
    writer.write_bits(0, 1)  # strip video signal type

    if video_signal_type_present_flag:
        # Read but discard: video_format (3), video_full_range_flag (1)
        reader.read_bits(3)
        reader.read_bits(1)
        colour_description_present_flag = reader.read_bits(1)
        if colour_description_present_flag:
            # Read but discard: colour_primaries, transfer_characteristics, matrix_coeffs
            reader.read_unsigned(8)
            reader.read_unsigned(8)
            reader.read_unsigned(8)

    # chroma_loc_info_present_flag
    chroma_loc_info_present_flag = reader.read_bits(1)
    writer.write_bits(chroma_loc_info_present_flag, 1)

    if chroma_loc_info_present_flag:
        chroma_sample_loc_type_top_field = reader.read_ue()
        writer.write_ue(chroma_sample_loc_type_top_field)
        chroma_sample_loc_type_bottom_field = reader.read_ue()
        writer.write_ue(chroma_sample_loc_type_bottom_field)

    # timing_info_present_flag
    timing_info_present_flag = reader.read_bits(1)
    writer.write_bits(timing_info_present_flag, 1)

    if timing_info_present_flag:
        num_units_in_tick = reader.read_unsigned(32)
        writer.write_unsigned(num_units_in_tick, 32)
        time_scale = reader.read_unsigned(32)
        writer.write_unsigned(time_scale, 32)
        fixed_frame_rate_flag = reader.read_bits(1)
        writer.write_bits(fixed_frame_rate_flag, 1)

    # nal_hrd_parameters_present_flag
    nal_hrd_parameters_present_flag = reader.read_bits(1)
    writer.write_bits(nal_hrd_parameters_present_flag, 1)

    if nal_hrd_parameters_present_flag:
        _copy_hrd_parameters(reader, writer)

    # vcl_hrd_parameters_present_flag
    vcl_hrd_parameters_present_flag = reader.read_bits(1)
    writer.write_bits(vcl_hrd_parameters_present_flag, 1)

    if vcl_hrd_parameters_present_flag:
        _copy_hrd_parameters(reader, writer)

    if nal_hrd_parameters_present_flag or vcl_hrd_parameters_present_flag:
        low_delay_hrd_flag = reader.read_bits(1)
        writer.write_bits(low_delay_hrd_flag, 1)

    # pic_struct_present_flag
    pic_struct_present_flag = reader.read_bits(1)
    writer.write_bits(pic_struct_present_flag, 1)

    # bitstream_restriction_flag
    bitstream_restriction_flag = reader.read_bits(1)
    writer.write_bits(1, 1)  # force bitstream restriction

    if not bitstream_restriction_flag:
        _write_bitstream_restriction(writer, max_num_ref_frames)
    else:
        # Copy existing fields but override reorder/buffering
        motion_vectors_over_pic_boundaries_flag = reader.read_bits(1)
        writer.write_bits(motion_vectors_over_pic_boundaries_flag, 1)

        max_bytes_per_pic_denom = reader.read_ue()
        writer.write_ue(max_bytes_per_pic_denom)

        max_bits_per_mb_denom = reader.read_ue()
        writer.write_ue(max_bits_per_mb_denom)

        log2_max_mv_length_horizontal = reader.read_ue()
        writer.write_ue(log2_max_mv_length_horizontal)

        log2_max_mv_length_vertical = reader.read_ue()
        writer.write_ue(log2_max_mv_length_vertical)

        # Read and override max_num_reorder_frames
        _num_reorder_frames = reader.read_ue()
        writer.write_ue(0)  # force 0

        # Read and override max_dec_frame_buffering
        _max_dec_frame_buffering = reader.read_ue()
        writer.write_ue(max_num_ref_frames)


def _copy_hrd_parameters(reader: BitstreamReader, writer: BitstreamWriter) -> None:
    """Copy HRD (Hypothetical Reference Decoder) parameters verbatim."""
    cpb_cnt_minus1 = reader.read_ue()
    writer.write_ue(cpb_cnt_minus1)

    bit_rate_scale = reader.read_bits(4)
    writer.write_bits(bit_rate_scale, 4)

    cpb_size_scale = reader.read_bits(4)
    writer.write_bits(cpb_size_scale, 4)

    for _ in range(cpb_cnt_minus1 + 1):
        bit_rate_value_minus1 = reader.read_ue()
        writer.write_ue(bit_rate_value_minus1)

        cpb_size_value_minus1 = reader.read_ue()
        writer.write_ue(cpb_size_value_minus1)

        cbr_flag = reader.read_bits(1)
        writer.write_bits(cbr_flag, 1)

    initial_cpb_removal_delay_length_minus1 = reader.read_bits(5)
    writer.write_bits(initial_cpb_removal_delay_length_minus1, 5)

    cpb_removal_delay_length_minus1 = reader.read_bits(5)
    writer.write_bits(cpb_removal_delay_length_minus1, 5)

    dpb_output_delay_length_minus1 = reader.read_bits(5)
    writer.write_bits(dpb_output_delay_length_minus1, 5)

    time_offset_length = reader.read_bits(5)
    writer.write_bits(time_offset_length, 5)


def _write_bitstream_restriction(writer: BitstreamWriter, max_num_ref_frames: int) -> None:
    """Write bitstream restriction fields (with our overrides)."""
    # motion_vectors_over_pic_boundaries_flag: u(1) -> default 1
    writer.write_bits(1, 1)

    # max_bytes_per_pic_denom: ue(v) -> default 2
    writer.write_ue(2)

    # max_bits_per_mb_denom: ue(v) -> default 1
    writer.write_ue(1)

    # log2_max_mv_length_horizontal: ue(v) -> default 16
    writer.write_ue(16)

    # log2_max_mv_length_vertical: ue(v) -> default 16
    writer.write_ue(16)

    # max_num_reorder_frames: ue(v) -> 0 (no reordering!)
    writer.write_ue(0)

    # max_dec_frame_buffering: ue(v) -> max_num_ref_frames
    writer.write_ue(max_num_ref_frames)
