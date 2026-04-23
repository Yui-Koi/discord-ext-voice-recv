"""
H.264 NALU processing and RTP packetization.

Handles splitting Annex-B frames into NALUs, FU-A fragmentation for
large NALUs, and STAP-A aggregation for bundling small NALUs (SPS+PPS).

Reference implementations:
- discord-video-stream/src/client/processing/AnnexBHelper.ts (NALU splitting)
- discord-video-stream/src/client/voice/WebRtcWrapper.ts (sendVideoFrame)
- RFC 6184 (H.264 RTP payload format)
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from .serialize import build_rtp_header, build_rtp_packet, set_marker

if TYPE_CHECKING:
    from typing import List, Tuple

__all__ = [
    'split_nalu',
    'get_nalu_type',
    'H264Packetizer',
    'H264NalUnitTypes',
]

# NAL unit types relevant to our packetizer
class H264NalUnitTypes:
    NON_IDR    = 1   # Coded slice of non-IDR picture (P-frame)
    IDR        = 5   # Coded slice of IDR picture (I-frame / keyframe)
    SEI        = 6   # Supplemental Enhancement Information
    SPS        = 7   # Sequence Parameter Set
    PPS        = 8   # Picture Parameter Set
    AUD        = 9   # Access Unit Delimiter

# Annex-B start codes
START_CODE_3 = b'\x00\x00\x01'  # 3-byte
START_CODE_4 = b'\x00\x00\x00\x01'  # 4-byte


def split_nalu(frame: bytes) -> List[bytes]:
    """Split an Annex-B encoded frame into individual NAL units.

    Handles both 3-byte (0x000001) and 4-byte (0x00000001) start codes.
    The start codes are NOT included in the returned NALUs.

    Parameters
    ----------
    frame : bytes
        Complete frame in Annex-B format (start-code delimited).

    Returns
    -------
    list[bytes]
        List of NAL unit byte strings (without start codes).
    """
    # Find all start code positions (with their lengths)
    # Each entry is (position, code_length)
    codes = []
    pos = 0
    while pos < len(frame):
        idx = frame.find(START_CODE_3, pos)
        if idx == -1:
            break

        # Check if this 3-byte code is actually part of a 4-byte code
        if idx > 0 and frame[idx - 1] == 0x00:
            # It's a 4-byte start code at idx-1
            codes.append((idx - 1, 4))
            pos = idx + 3  # skip past the 3-byte part
        else:
            # It's a genuine 3-byte start code
            codes.append((idx, 3))
            pos = idx + 3

    if not codes:
        # No start codes found, treat entire frame as one NALU
        if frame:
            return [frame]
        return []

    nalus = []
    for i, (start, code_len) in enumerate(codes):
        nalu_start = start + code_len

        # NALU ends at the next start code position (or end of frame)
        if i + 1 < len(codes):
            nalu_end = codes[i + 1][0]
        else:
            nalu_end = len(frame)

        nalu = frame[nalu_start:nalu_end]
        if nalu:
            nalus.append(nalu)

    return nalus


def get_nalu_type(nalu: bytes) -> int:
    """Return the H.264 NAL unit type from the first byte.

    Type = nalu[0] & 0x1F
    """
    if not nalu:
        return -1
    return nalu[0] & 0x1F


class H264Packetizer:
    """Packetizes H.264 frames into RTP packets.

    Handles:
    - Single NAL unit packets (NALU <= MAX_PAYLOAD)
    - FU-A fragmentation (NALU > MAX_PAYLOAD)
    - Marker bit on last packet of each access unit (frame)

    Does NOT handle STAP-A aggregation yet (can be added if needed).
    """

    # Max RTP payload size, leaving room for RTP header (12) + some margin
    MAX_PAYLOAD = 1300

    def __init__(self, ssrc: int, payload_type: int = 101):
        self.ssrc = ssrc
        self.payload_type = payload_type
        self._sequence = 0
        self._timestamp = 0

    def set_timestamp(self, timestamp: int) -> None:
        """Set the RTP timestamp for the current frame."""
        self._timestamp = timestamp & 0xFFFFFFFF

    def packetize_frame(self, frame: bytes) -> List[bytearray]:
        """Convert an Annex-B H.264 frame into RTP packets.

        Parameters
        ----------
        frame : bytes
            Complete H.264 frame in Annex-B format (start-code delimited).

        Returns
        -------
        list[bytearray]
            List of complete RTP packets (header + payload). The marker bit
            is set on the last packet of the frame.
        """
        nalus = split_nalu(frame)
        packets = []

        for nalu in nalus:
            if len(nalu) <= self.MAX_PAYLOAD:
                # Single NAL unit packet
                packets.append(self._single_nalu(nalu))
            else:
                # FU-A fragmentation
                packets.extend(self._fu_a(nalu))

        # Set marker bit on last packet of the frame
        if packets:
            set_marker(packets[-1], True)

        return packets

    def _single_nalu(self, nalu: bytes) -> bytearray:
        """Build a single NAL unit RTP packet."""
        header = build_rtp_header(
            sequence=self._next_seq(),
            timestamp=self._timestamp,
            ssrc=self.ssrc,
            payload_type=self.payload_type,
        )
        return build_rtp_packet(header, nalu)

    def _fu_a(self, nalu: bytes) -> List[bytearray]:
        """Fragment a large NAL unit using FU-A.

        FU indicator byte: (nalu[0] & 0x60) | 28
            - 0x60 preserves the NRI bits
            - 28 is the FU-A NAL unit type

        FU header byte:
            - bit 7 (0x80): start bit
            - bit 6 (0x40): end bit
            - bits 4-0: original NAL unit type
        """
        f_nri = nalu[0] & 0x60
        nal_type = nalu[0] & 0x1F
        fu_indicator = f_nri | 28  # FU-A type = 28

        payload = nalu[1:]  # strip the NAL header byte
        max_frag = self.MAX_PAYLOAD - 2  # subtract FU indicator + FU header

        fragments = []
        offset = 0

        while offset < len(payload):
            chunk = payload[offset:offset + max_frag]
            is_start = offset == 0
            is_end = offset + max_frag >= len(payload)

            fu_header = nal_type
            if is_start:
                fu_header |= 0x80
            if is_end:
                fu_header |= 0x40

            rtp_payload = bytes([fu_indicator, fu_header]) + chunk
            header = build_rtp_header(
                sequence=self._next_seq(),
                timestamp=self._timestamp,
                ssrc=self.ssrc,
                payload_type=self.payload_type,
            )
            fragments.append(build_rtp_packet(header, rtp_payload))
            offset += max_frag

        return fragments

    def _next_seq(self) -> int:
        """Get next sequence number, wrapping at 65536."""
        seq = self._sequence
        self._sequence = (self._sequence + 1) & 0xFFFF
        return seq
