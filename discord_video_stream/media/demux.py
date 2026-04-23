"""
NUT container demuxing using PyAV.

Reads NUT format from FFmpeg's stdout pipe and yields video/audio frames
with proper timestamps. Supports both NUT and Matroska container formats.

PyAV reads synchronously from the pipe. To avoid starving the event loop,
we yield after each frame and call asyncio.sleep(0) periodically to give
other coroutines a chance to run. This is not true non-blocking I/O, but
it is the most reliable approach given PyAV's FFmpeg I/O bindings which
are not thread-safe.

The Opus frame duration parser handles cases where the NUT container
may not provide accurate duration info (RFC 6716 TOC byte parsing).
"""

from __future__ import annotations

import asyncio
import logging
import os
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


def _wrap_pipe(pipe):
    """Wrap an asyncio StreamReader into a synchronous file-like for PyAV.

    PyAV's av.open() needs a synchronous .read() method. asyncio's
    StreamReader has an async .read(). We duplicate the underlying fd
    and set it to blocking mode so PyAV can read synchronously.

    Returns (file_object, dup_fd) so the caller can close the dup_fd
    explicitly. Returns (pipe, None) if pipe is already a synchronous
    file-like object.
    """
    import fcntl

    if hasattr(pipe, '_transport'):
        pipe_fd = pipe._transport.get_extra_info('pipe').fileno()
        dup_fd = os.dup(pipe_fd)
        flags = fcntl.fcntl(dup_fd, fcntl.F_GETFL)
        fcntl.fcntl(dup_fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        return os.fdopen(dup_fd, 'rb'), dup_fd
    return pipe, None


class Demuxer:
    """Demuxes NUT (or Matroska) container from a readable stream.

    PyAV reads synchronously from the pipe. To avoid starving the event
    loop, we yield after each frame and call asyncio.sleep(0) every
    YIELD_INTERVAL frames to give other coroutines (heartbeats, pacing)
    a chance to run.

    Usage:
        demuxer = Demuxer()
        async for frame in demuxer.demux(process.stdout):
            if frame.frame_type == FrameType.VIDEO:
                ...  # send frame
    """

    # Yield to the event loop every N frames to prevent starvation
    YIELD_INTERVAL = 8

    def __init__(self, format: str = 'nut', buffer_size: int = 8192) -> None:
        self._format = format
        self._buffer_size = buffer_size
        self._sync_pipe = None
        self._dup_fd = None

    def _close_pipe(self):
        """Close the duplicated pipe fd if open."""
        if self._sync_pipe is not None:
            try:
                self._sync_pipe.close()
            except Exception:
                pass
            self._sync_pipe = None
        if self._dup_fd is not None:
            try:
                os.close(self._dup_fd)
            except OSError:
                pass
            self._dup_fd = None

    async def probe(self, pipe) -> Tuple[
        Optional[VideoStreamInfo],
        Optional[AudioStreamInfo],
    ]:
        """Probe the input to get stream info without consuming frames.

        Returns (video_info, audio_info) tuples. Either may be None if
        the corresponding stream is absent.
        """
        sync_pipe, dup_fd = _wrap_pipe(pipe)
        try:
            container = av.open(
                sync_pipe,
                format=self._format,
                buffer_size=self._buffer_size,
                options={'ffflags': 'nobuffer'},
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
        finally:
            if dup_fd is not None:
                try:
                    os.close(dup_fd)
                except OSError:
                    pass

    async def demux(self, pipe) -> AsyncIterator[MediaFrame]:
        """Demux frames from the input pipe.

        Yields MediaFrame objects for each video and audio packet.
        Calls asyncio.sleep(0) every YIELD_INTERVAL frames to prevent
        event loop starvation. This gives heartbeats, pacing, and other
        coroutines a chance to run between frame bursts.

        Parameters
        ----------
        pipe : asyncio.StreamReader or file-like
            Input stream containing NUT (or other container) data.
        """
        self._sync_pipe, self._dup_fd = _wrap_pipe(pipe)

        container = av.open(
            self._sync_pipe,
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

        frames_since_yield = 0

        try:
            for packet in container.demux():
                pkt_data = bytes(packet)
                if not pkt_data:
                    continue

                frame = None

                if packet.stream == video_stream:
                    is_kf = bool(packet.is_keyframe)
                    if not is_kf and pkt_data:
                        is_kf = _contains_idr_nalu(pkt_data)

                    frame = MediaFrame(
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

                    frame = MediaFrame(
                        frame_type=FrameType.AUDIO,
                        data=pkt_data,
                        pts=packet.pts or 0,
                        duration=duration,
                        is_keyframe=False,
                        time_base_num=packet.stream.time_base.numerator,
                        time_base_den=packet.stream.time_base.denominator,
                    )

                if frame is not None:
                    yield frame
                    frames_since_yield += 1

                    # Periodically yield to the event loop to prevent
                    # heartbeat timeout and allow other coroutines to run
                    if frames_since_yield >= self.YIELD_INTERVAL:
                        await asyncio.sleep(0)
                        frames_since_yield = 0

        except av.FFmpegError as e:
            log.warning('Demuxer error: %s', e)
        finally:
            container.close()
            self._close_pipe()
            log.debug('Demuxer closed')


def _contains_idr_nalu(data: bytes) -> bool:
    """Check if an Annex-B frame contains an IDR NAL unit (keyframe).

    Scans for start codes and checks NALU type of each NALU.
    IDR NALU type = 5 (data[0] & 0x1F after the start code).
    """
    pos = 0
    while pos < len(data):
        idx4 = data.find(b'\x00\x00\x00\x01', pos)
        idx3 = data.find(b'\x00\x00\x01', pos)

        if idx4 == -1 and idx3 == -1:
            break

        if idx4 != -1 and (idx3 == -1 or idx4 <= idx3):
            nalu_start = idx4 + 4
        else:
            nalu_start = idx3 + 3

        if nalu_start < len(data):
            nalu_type = data[nalu_start] & 0x1F
            if nalu_type == 5:
                return True

        pos = nalu_start

    return False


def parse_opus_duration(frame: bytes) -> int:
    """Parse Opus frame duration from the TOC byte (RFC 6716 section 3.1).

    Returns duration in samples at 48kHz.
    """
    if not frame:
        return 0

    frame_sizes_samples = [
        480, 960, 1920, 2880,
        480, 960, 1920, 2880,
        480, 960, 1920, 2880,
        480, 960,
        480, 960,
        120, 240, 480, 960,
        120, 240, 480, 960,
        120, 240, 480, 960,
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
        frame_count = frame[1] & 0x3F if len(frame) > 1 else 1

    return frame_size * frame_count
