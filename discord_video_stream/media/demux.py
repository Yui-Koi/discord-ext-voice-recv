"""
NUT container demuxing using PyAV.

Reads NUT format from FFmpeg's stdout pipe and yields video/audio frames
with proper timestamps. Supports both NUT and Matroska container formats.

Two approaches:
- Primary: PyAV container demuxing (av.open with format='nut')
- Fallback: Raw Annex-B parsing (if PyAV latency is problematic)

The Opus frame duration parser handles cases where the NUT container
may not provide accurate duration info (RFC 6716 TOC byte parsing).
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import av

if TYPE_CHECKING:
    from typing import AsyncIterator, Optional, Tuple

__all__ = [
    'FrameType',
    'MediaFrame',
    'VideoStreamInfo',
    'AudioStreamInfo',
    'Demuxer',
    'parse_opus_duration',
]

log = logging.getLogger(__name__)


class FrameType(Enum):
    VIDEO = 'video'
    AUDIO = 'audio'


@dataclass
class MediaFrame:
    """A single demuxed frame with timing information."""

    frame_type: FrameType
    data: bytes
    pts: int           # Presentation timestamp in stream time base units
    duration: int      # Duration in stream time base units
    is_keyframe: bool  # True for IDR/key frames
    time_base_num: int  # Time base numerator
    time_base_den: int  # Time base denominator

    @property
    def pts_ms(self) -> float:
        """Presentation timestamp in milliseconds."""
        return (self.pts / self.time_base_den) * self.time_base_num * 1000

    @property
    def duration_ms(self) -> float:
        """Duration in milliseconds."""
        return (self.duration / self.time_base_den) * self.time_base_num * 1000

    @property
    def frametime_ms(self) -> float:
        """Frame time in milliseconds (alias for duration_ms)."""
        return self.duration_ms


@dataclass
class VideoStreamInfo:
    """Metadata about the video stream."""

    index: int
    codec_name: str
    width: int
    height: int
    framerate_num: int
    framerate_den: int
    time_base_num: int
    time_base_den: int


@dataclass
class AudioStreamInfo:
    """Metadata about the audio stream."""

    index: int
    codec_name: str
    sample_rate: int
    channels: int
    time_base_num: int
    time_base_den: int


class Demuxer:
    """Demuxes NUT (or Matroska) container from a readable stream.

    Uses PyAV's container API with buffering tuned for low-latency
    real-time pipe reading.

    Usage:
        demuxer = Demuxer()
        async for frame in demuxer.demux(process.stdout):
            if frame.frame_type == FrameType.VIDEO:
                ...  # send frame
    """

    def __init__(self, format: str = 'nut', buffer_size: int = 8192) -> None:
        self._format = format
        self._buffer_size = buffer_size

    def _wrap_pipe(self, pipe):
        """Wrap an asyncio StreamReader into a synchronous file-like for PyAV.

        PyAV's av.open() needs a synchronous .read() method. asyncio's
        StreamReader has an async .read(). We duplicate the underlying fd
        and set it to blocking mode so PyAV can read synchronously.
        """
        import os
        import fcntl

        if hasattr(pipe, '_transport'):
            # asyncio StreamReader from subprocess
            pipe_fd = pipe._transport.get_extra_info('pipe').fileno()
            # Duplicate the fd so closing the file doesn't close the subprocess pipe
            dup_fd = os.dup(pipe_fd)
            # Set to blocking mode (asyncio sets it to non-blocking)
            flags = fcntl.fcntl(dup_fd, fcntl.F_GETFL)
            fcntl.fcntl(dup_fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
            return os.fdopen(dup_fd, 'rb')
        return pipe

    async def probe(self, pipe) -> Tuple[
        Optional[VideoStreamInfo],
        Optional[AudioStreamInfo],
    ]:
        """Probe the input to get stream info without consuming frames.

        Returns (video_info, audio_info) tuples. Either may be None if
        the corresponding stream is absent.
        """
        sync_pipe = self._wrap_pipe(pipe)

        container = av.open(
            sync_pipe,
            format=self._format,
            buffer_size=self._buffer_size,
            options={'fflags': 'nobuffer'},
        )

        video_info = None
        audio_info = None

        for stream in container.streams:
            if stream.type == 'video':
                video_info = VideoStreamInfo(
                    index=stream.index,
                    codec_name=stream.codec_context.name,
                    width=stream.codec_context.width,
                    height=stream.codec_context.height,
                    framerate_num=stream.codec_context.framerate.numerator,
                    framerate_den=stream.codec_context.framerate.denominator,
                    time_base_num=stream.time_base.numerator,
                    time_base_den=stream.time_base.denominator,
                )
            elif stream.type == 'audio':
                audio_info = AudioStreamInfo(
                    index=stream.index,
                    codec_name=stream.codec_context.name,
                    sample_rate=stream.codec_context.sample_rate,
                    channels=stream.codec_context.channels,
                    time_base_num=stream.time_base.numerator,
                    time_base_den=stream.time_base.denominator,
                )

        container.close()
        return video_info, audio_info

    async def demux(self, pipe) -> AsyncIterator[MediaFrame]:
        """Demux frames from the input pipe.

        Yields MediaFrame objects for each video and audio packet.
        The pipe can be an asyncio StreamReader (from process.stdout)
        or a synchronous file-like object.
        """
        sync_pipe = self._wrap_pipe(pipe)

        container = av.open(
            sync_pipe,
            format=self._format,
            buffer_size=self._buffer_size,
            options={'fflags': 'nobuffer'},
        )

        video_stream = None
        audio_stream = None

        for stream in container.streams:
            if stream.type == 'video' and video_stream is None:
                video_stream = stream
            elif stream.type == 'audio' and audio_stream is None:
                audio_stream = stream

        log.debug(
            'Demuxer opened: video=%s, audio=%s',
            video_stream is not None,
            audio_stream is not None,
        )

        try:
            for packet in container.demux():
                # Skip empty packets (flush packets, etc.)
                pkt_data = bytes(packet)
                if not pkt_data:
                    continue

                if packet.stream == video_stream:
                    # NUT format may not report keyframe flag reliably.
                    # Detect keyframes from NALU data: IDR (type 5) = keyframe.
                    is_kf = bool(packet.is_keyframe)
                    if not is_kf and pkt_data:
                        is_kf = _contains_idr_nalu(pkt_data)

                    yield MediaFrame(
                        frame_type=FrameType.VIDEO,
                        data=pkt_data,
                        pts=packet.pts or 0,
                        duration=packet.duration or 0,
                        is_keyframe=is_kf,
                        time_base_num=packet.stream.time_base.numerator,
                        time_base_den=packet.stream.time_base.denominator,
                    )
                elif packet.stream == audio_stream:
                    duration = packet.duration
                    if duration is None or duration == 0:
                        duration = parse_opus_duration(pkt_data)

                    yield MediaFrame(
                        frame_type=FrameType.AUDIO,
                        data=pkt_data,
                        pts=packet.pts or 0,
                        duration=duration,
                        is_keyframe=False,
                        time_base_num=packet.stream.time_base.numerator,
                        time_base_den=packet.stream.time_base.denominator,
                    )
        except av.FFmpegError as e:
            log.warning('Demuxer error: %s', e)
        finally:
            container.close()
            log.debug('Demuxer closed')


def _contains_idr_nalu(data: bytes) -> bool:
    """Check if an Annex-B frame contains an IDR NAL unit (keyframe).

    Scans for start codes and checks NALU type of each NALU.
    IDR NALU type = 5 (data[0] & 0x1F after the start code).
    """
    pos = 0
    while pos < len(data):
        # Find next start code
        idx4 = data.find(b'\x00\x00\x00\x01', pos)
        idx3 = data.find(b'\x00\x00\x01', pos)

        if idx4 == -1 and idx3 == -1:
            break

        if idx4 != -1 and (idx3 == -1 or idx4 <= idx3):
            nalu_start = idx4 + 4
        else:
            # Check if it's a 4-byte code disguised as 3-byte
            if idx3 > 0 and data[idx3 - 1] == 0:
                nalu_start = idx3 + 3
            else:
                nalu_start = idx3 + 3

        if nalu_start < len(data):
            nalu_type = data[nalu_start] & 0x1F
            if nalu_type == 5:  # IDR
                return True

        pos = nalu_start

    return False


def parse_opus_duration(frame: bytes) -> int:
    """Parse Opus frame duration from the TOC byte (RFC 6716 section 3.1).

    Returns duration in samples at 48kHz. The TOC byte format:
    - Bits 7-3: Configuration number (determines frame size)
    - Bit 2: Stereo flag
    - Bits 1-0: Code (0=1 frame, 1-2=2 frames, 3=count in next byte)

    Frame sizes by configuration:
    0-3:   SILK narrowband    (10, 20, 40, 60ms)
    4-7:   SILK medium-band   (10, 20, 40, 60ms)
    8-11:  SILK wideband      (10, 20, 40, 60ms)
    12-13: Hybrid super-wideband (10, 20ms)
    14-15: Hybrid fullband    (10, 20ms)
    16-19: CELT narrowband    (2.5, 5, 10, 20ms)
    20-23: CELT wideband      (2.5, 5, 10, 20ms)
    24-27: CELT super-wideband (2.5, 5, 10, 20ms)
    28-31: CELT fullband      (2.5, 5, 10, 20ms)

    Duration in ms = (samples / 48) -> but we return raw samples
    since the caller can convert.
    """
    if not frame:
        return 0

    # Frame sizes in ms * 48 (to get samples at 48kHz)
    # Each entry is ms * 48
    frame_sizes_samples = [
        # SILK narrowband
        480, 960, 1920, 2880,
        # SILK medium-band
        480, 960, 1920, 2880,
        # SILK wideband
        480, 960, 1920, 2880,
        # Hybrid super-wideband
        480, 960,
        # Hybrid fullband
        480, 960,
        # CELT narrowband (2.5ms=120, 5ms=240, 10ms=480, 20ms=960)
        120, 240, 480, 960,
        # CELT wideband
        120, 240, 480, 960,
        # CELT super-wideband
        120, 240, 480, 960,
        # CELT fullband
        120, 240, 480, 960,
    ]

    toc = frame[0]
    config = toc >> 3
    code = toc & 0x03

    frame_size = frame_sizes_samples[config]

    if code == 0:
        frame_count = 1
    elif code in (1, 2):
        frame_count = 2
    else:
        # code == 3: frame count in next byte
        frame_count = frame[1] & 0x3F if len(frame) > 1 else 1

    return frame_size * frame_count
