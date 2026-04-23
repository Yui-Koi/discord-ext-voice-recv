"""
FFmpeg subprocess management for transcoding input media to H.264 + Opus.

Constructs FFmpeg commands with the specific flags validated from the
Node.js discord-video-stream reference implementation. Outputs NUT
format to stdout pipe for real-time demuxing.

Critical flags (from Node.js reference, validated):
- -bf 0: No B-frames (essential for low latency)
- -preset superfast: NOT ultrafast (causes bitrate spikes)
- -forced-idr 1: Every keyframe is IDR (SFU recovery)
- -force_key_frames expr:gte(t,n_forced*1): 1s keyframe interval
- -pix_fmt yuv420p: Only 4:2:0 chroma Discord supports
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import List, Optional

__all__ = [
    'StreamOptions',
    'FFmpegProcess',
]

log = logging.getLogger(__name__)


@dataclass
class StreamOptions:
    """Configuration for the FFmpeg transcoding pipeline."""

    url: str
    """Input URL or file path. Can be any FFmpeg-supported input."""

    width: int = -2
    """Output width. -2 maintains aspect ratio."""

    height: int = -2
    """Output height. -2 maintains aspect ratio."""

    frame_rate: Optional[int] = None
    """Output frame rate. None preserves input rate."""

    bitrate_video: int = 5000
    """Target video bitrate in kbps."""

    bitrate_video_max: int = 7000
    """Max video bitrate in kbps."""

    bitrate_audio: int = 128
    """Audio bitrate in kbps."""

    include_audio: bool = True
    """Whether to include audio stream."""

    hwaccel: bool = False
    """Enable hardware acceleration."""

    custom_input_options: List[str] = field(default_factory=list)
    """Extra input options (before -i)."""

    custom_flags: List[str] = field(default_factory=list)
    """Extra flags (after output mapping, before -f)."""

    no_transcoding: bool = False
    """Passthrough mode: copy video codec instead of re-encoding.
    Requires input already in H.264 with correct properties."""


class FFmpegProcess:
    """Manages an FFmpeg subprocess that outputs NUT format to stdout.

    A background task continuously drains stderr to prevent pipe buffer
    overflow that would deadlock FFmpeg. Stderr output is logged at
    debug level. Use read_stderr() to get the full output after the
    process ends.

    Usage:
        opts = StreamOptions(url='input.mp4', bitrate_video=5000)
        ffmpeg = FFmpegProcess(opts)
        proc = await ffmpeg.start()
        # Read from proc.stdout for NUT data
        await ffmpeg.stop()
    """

    def __init__(self, options: StreamOptions) -> None:
        self._options = options
        self._process: Optional[asyncio.subprocess.Process] = None
        self._cmd = self._build_command(options)
        self._stderr_drain_task: Optional[asyncio.Task] = None
        self._stderr_buffer: List[bytes] = []

    @property
    def command(self) -> List[str]:
        """The constructed FFmpeg command."""
        return self._cmd.copy()

    @property
    def process(self) -> Optional[asyncio.subprocess.Process]:
        """The running subprocess, or None if not started."""
        return self._process

    @property
    def stdout(self):
        """stdout pipe of the running process."""
        if self._process:
            return self._process.stdout
        return None

    @property
    def stderr(self):
        """stderr pipe of the running process."""
        if self._process:
            return self._process.stderr
        return None

    def _build_command(self, opts: StreamOptions) -> List[str]:
        cmd = ['ffmpeg', '-y', '-loglevel', 'warning', '-nostats']

        # Input options
        if opts.hwaccel:
            cmd += ['-hwaccel', 'auto']

        cmd += opts.custom_input_options
        cmd += ['-i', opts.url]

        # Video encoding
        cmd += ['-map', '0:v']

        if opts.no_transcoding:
            cmd += ['-vcodec', 'copy']
        else:
            bufsize = opts.bitrate_video // 2
            cmd += [
                '-vcodec', 'libx264',
                '-preset', 'superfast',
                '-tune', 'film',
                '-forced-idr', '1',
                '-bf', '0',
                '-pix_fmt', 'yuv420p',
                '-force_key_frames', 'expr:gte(t,n_forced*1)',
                '-b:v', f'{opts.bitrate_video}k',
                '-maxrate:v', f'{opts.bitrate_video_max}k',
                '-bufsize:v', f'{bufsize}k',
                '-vf', f'scale={opts.width}:{opts.height}',
            ]

        if opts.frame_rate:
            cmd += ['-r', str(opts.frame_rate)]

        # Audio encoding
        if opts.include_audio:
            cmd += [
                '-map', '0:a:0?',
                '-ac', '2',
                '-ar', '48000',
                '-acodec', 'libopus',
                '-b:a', f'{opts.bitrate_audio}k',
            ]

        # Custom flags
        cmd += opts.custom_flags

        # Output: NUT format to stdout
        cmd += ['-f', 'nut', 'pipe:1']

        return cmd

    async def start(self) -> asyncio.subprocess.Process:
        """Start the FFmpeg subprocess.

        Returns the process with stdout available for reading NUT data.
        A background task drains stderr to prevent pipe buffer overflow.
        """
        log.debug('Starting FFmpeg: %s', ' '.join(self._cmd))

        self._process = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Start background stderr drain to prevent pipe buffer overflow
        self._stderr_buffer = []
        self._stderr_drain_task = asyncio.create_task(
            self._drain_stderr(),
            name='ffmpeg-stderr-drain',
        )

        log.debug('FFmpeg started, pid=%d', self._process.pid)
        return self._process

    async def _drain_stderr(self) -> None:
        """Continuously read stderr to prevent pipe buffer overflow.

        FFmpeg writes diagnostic output to stderr. If the OS pipe buffer
        (typically 64KB) fills up, FFmpeg blocks on stderr writes, which
        blocks stdout writes, which deadlocks the entire pipeline.
        """
        if self._process is None or self._process.stderr is None:
            return
        try:
            while True:
                data = await self._process.stderr.read(4096)
                if not data:
                    break
                self._stderr_buffer.append(data)
                # Log at debug level so it's available if needed
                for line in data.decode('utf-8', errors='replace').splitlines():
                    line = line.strip()
                    if line:
                        log.debug('ffmpeg: %s', line)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug('stderr drain error: %s', e)

    async def stop(self) -> None:
        """Stop FFmpeg gracefully, waiting for it to finish."""
        if self._process is None:
            return

        # Cancel stderr drain task
        if self._stderr_drain_task is not None:
            self._stderr_drain_task.cancel()
            try:
                await self._stderr_drain_task
            except asyncio.CancelledError:
                pass
            self._stderr_drain_task = None

        if self._process.returncode is None:
            log.debug('Terminating FFmpeg pid=%d', self._process.pid)
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning('FFmpeg did not terminate, killing pid=%d', self._process.pid)
                self._process.kill()
                await self._process.wait()

        log.debug('FFmpeg exited with code %d', self._process.returncode)
        self._process = None

    async def wait(self) -> int:
        """Wait for FFmpeg to finish and return exit code."""
        if self._process:
            return await self._process.wait()
        return -1

    async def read_stderr(self) -> str:
        """Return all stderr output collected by the drain task.

        Returns the buffered stderr output. Call after stop() for
        complete output.
        """
        return b''.join(self._stderr_buffer).decode('utf-8', errors='replace')
