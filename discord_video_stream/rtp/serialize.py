"""
RTP packet construction for Discord voice/video transport.

Builds RTP packets from individual components. The header format mirrors
discord-ext-voice-recv's rtp.py parsing (same struct layout, reverse direction).

Reference: _hstruct = struct.Struct('>xxHII') in voice-recv's RTPPacket
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Optional

__all__ = [
    'build_rtp_header',
    'build_rtp_packet',
    'set_marker',
    'build_rtcp_sr',
]

# Header layout:
#   byte 0:  V(2) P(1) X(1) CC(4)
#   byte 1:  M(1) PT(7)
#   bytes 2-3:   sequence (H)
#   bytes 4-7:   timestamp (I)
#   bytes 8-11:  ssrc (I)

_HEADER_FMT = struct.Struct('>BBHII')


def build_rtp_header(
    sequence: int,
    timestamp: int,
    ssrc: int,
    payload_type: int,
    marker: bool = False,
    extension: bool = False,
    padding: bool = False,
    csrc_count: int = 0,
) -> bytearray:
    """Build a 12-byte RTP header.

    Parameters
    ----------
    sequence : int
        16-bit sequence number (wraps at 65536).
    timestamp : int
        32-bit RTP timestamp (wraps at 2^32).
    ssrc : int
        32-bit synchronization source identifier.
    payload_type : int
        7-bit payload type (e.g. 120 for opus, 101 for H.264).
    marker : bool
        Marker bit. For video: last packet of a frame. For audio: typically False.
    extension : bool
        Extension bit. Set if RTP header extensions follow the fixed header.
    padding : bool
        Padding bit. Set if padding bytes appended to payload.
    csrc_count : int
        Number of CSRC identifiers (0-15).
    """
    byte0 = (2 << 6)  # version 2
    if padding:
        byte0 |= 0x20
    if extension:
        byte0 |= 0x10
    byte0 |= (csrc_count & 0x0F)

    byte1 = payload_type & 0x7F
    if marker:
        byte1 |= 0x80

    header = bytearray(_HEADER_FMT.size)
    _HEADER_FMT.pack_into(header, 0, byte0, byte1, sequence & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
    return header


def build_rtp_packet(
    header: bytearray,
    payload: bytes,
) -> bytearray:
    """Build a complete RTP packet from header and payload.

    Returns header + payload concatenated.
    """
    packet = bytearray(len(header) + len(payload))
    packet[:len(header)] = header
    packet[len(header):] = payload
    return packet


def set_marker(packet: bytearray, marker: bool) -> bytearray:
    """Set or clear the marker bit in an existing RTP packet (byte 1, bit 7)."""
    if marker:
        packet[1] |= 0x80
    else:
        packet[1] &= 0x7F
    return packet


def build_rtcp_sr(
    ssrc: int,
    ntp_timestamp: int,
    rtp_timestamp: int,
    packet_count: int,
    octet_count: int,
) -> bytearray:
    """Build an RTCP Sender Report (type 200).

    Parameters
    ----------
    ssrc : int
        SSRC of the sender.
    ntp_timestamp : int
        64-bit NTP timestamp.
    rtp_timestamp : int
        32-bit RTP timestamp.
    packet_count : int
        Total packets sent.
    octet_count : int
        Total octets (payload bytes) sent.
    """
    # Header: version=2, padding=0, report_count=0, type=200, length=6 (24 bytes / 4 - 1)
    header = struct.pack('>BBHI',
        (2 << 6) | 0,   # V=2, P=0, RC=0
        200,             # SR type
        6,               # length in 32-bit words minus 1 (28 bytes / 4 - 1 = 6)
        ssrc,
    )
    # Sender info: NTP (8 bytes) + RTP timestamp (4) + packet count (4) + octet count (4)
    ntp_high = (ntp_timestamp >> 32) & 0xFFFFFFFF
    ntp_low = ntp_timestamp & 0xFFFFFFFF
    sender_info = struct.pack('>IIIII',
        ntp_high,
        ntp_low,
        rtp_timestamp & 0xFFFFFFFF,
        packet_count & 0xFFFFFFFF,
        octet_count & 0xFFFFFFFF,
    )
    return bytearray(header + sender_info)
