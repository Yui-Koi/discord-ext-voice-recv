"""
Frame pacing and A/V synchronization.

Controls frame send timing to maintain correct playback speed and
lip sync between video and audio streams. Ported from the Node.js
discord-video-stream BaseMediaStream class.

Algorithm:
1. On first frame: record start_time (wall clock) and start_pts
2. For each frame: compute sleep = pts - start_pts + frametime - elapsed
3. If sleep > 0: sleep for that duration
4. If behind sync partner: skip sleep, reset timing
5. If ahead of sync partner: wait in loop until caught up
6. Sync tolerance: 20ms default
"""

from __future__ import annotations

import time
import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Optional

__all__ = [
    'FramePacer',
]

log = logging.getLogger(__name__)


class FramePacer:
    """Controls frame send timing with A/V synchronization.

    Usage:
        pacer = FramePacer(clock_rate=90000)  # video
        audio_pacer = FramePacer(clock_rate=48000)  # audio

        # Link for sync
        pacer.sync_partner = audio_pacer

        # In the send loop:
        await pacer.pace(pts_ms, frametime_ms)
        ... send frame ...
    """

    def __init__(self, clock_rate: int, sync_tolerance_ms: float = 20.0) -> None:
        self._clock_rate = clock_rate
        self._sync_tolerance_ms = sync_tolerance_ms

        self._start_time: Optional[float] = None
        self._start_pts: Optional[float] = None
        self._pts: Optional[float] = None

        self._sync_partner: Optional[FramePacer] = None
        self._sync_enabled: bool = True
        self._no_sleep: bool = False

        # Event signaled when this pacer's PTS is updated.
        # The sync partner awaits this instead of polling.
        self._pts_updated = asyncio.Event()

    @property
    def clock_rate(self) -> int:
        return self._clock_rate

    @property
    def pts(self) -> Optional[float]:
        """Current presentation timestamp in milliseconds."""
        return self._pts

    @pts.setter
    def pts(self, value: float) -> None:
        self._pts = value

    @property
    def sync_partner(self) -> Optional['FramePacer']:
        return self._sync_partner

    @sync_partner.setter
    def sync_partner(self, partner: Optional['FramePacer']) -> None:
        # Prevent circular sync
        if partner is not None and partner._sync_partner is self:
            raise ValueError('Cannot sync two streams with each other')
        self._sync_partner = partner

    @property
    def sync_enabled(self) -> bool:
        return self._sync_enabled

    @sync_enabled.setter
    def sync_enabled(self, value: bool) -> None:
        self._sync_enabled = value
        if value:
            log.debug('Sync enabled')
        else:
            log.debug('Sync disabled')

    @property
    def no_sleep(self) -> bool:
        """When True, pace() returns immediately (no sleeping)."""
        return self._no_sleep

    @no_sleep.setter
    def no_sleep(self, value: bool) -> None:
        self._no_sleep = value
        if value:
            self.reset_timing()

    @property
    def sync_tolerance_ms(self) -> float:
        return self._sync_tolerance_ms

    @sync_tolerance_ms.setter
    def sync_tolerance_ms(self, value: float) -> None:
        if value < 0:
            return
        self._sync_tolerance_ms = value

    def reset_timing(self) -> None:
        """Reset timing compensation. Call after seek or stream restart."""
        self._start_time = None
        self._start_pts = None

    def update_pts(self, pts_ms: float) -> None:
        """Update the current PTS and signal the sync partner."""
        self._pts = pts_ms
        self._pts_updated.set()
        self._pts_updated.clear()

    async def pace(self, pts_ms: float, frametime_ms: float) -> None:
        """Sleep if needed to maintain correct frame timing.

        Parameters
        ----------
        pts_ms : float
            Presentation timestamp in milliseconds.
        frametime_ms : float
            Expected frame duration in milliseconds.
        """
        self._pts = pts_ms

        if self._no_sleep:
            return

        now = time.monotonic() * 1000

        # Initialize on first frame (explicit None check: 0.0 is valid start_pts)
        if self._start_time is None:
            self._start_time = now
        if self._start_pts is None:
            self._start_pts = pts_ms

        # How long we should have been running
        expected_elapsed = pts_ms - self._start_pts + frametime_ms
        actual_elapsed = now - self._start_time
        sleep_ms = expected_elapsed - actual_elapsed

        # Check sync with partner
        if self._sync_enabled and self._sync_partner is not None:
            partner_pts = self._sync_partner._pts

            if partner_pts is not None:
                delta = pts_ms - partner_pts

                if self._is_behind(delta):
                    log.debug(
                        'Stream is behind by %.1fms (pts=%.1f, partner=%.1f), skipping sleep',
                        abs(delta), pts_ms, partner_pts,
                    )
                    self.reset_timing()
                    return

                if self._is_ahead(delta, frametime_ms):
                    log.debug(
                        'Stream is ahead by %.1fms (pts=%.1f, partner=%.1f), waiting',
                        delta, pts_ms, partner_pts,
                    )
                    # Wait for the sync partner to update its PTS
                    # instead of polling at frametime intervals
                    while self._sync_enabled and self._sync_partner is not None:
                        partner_pts = self._sync_partner._pts
                        if partner_pts is None:
                            break
                        delta = pts_ms - partner_pts
                        if not self._is_ahead(delta, frametime_ms):
                            break
                        # Wait for partner PTS update with timeout
                        try:
                            await asyncio.wait_for(
                                self._sync_partner._pts_updated.wait(),
                                timeout=frametime_ms / 1000,
                            )
                        except asyncio.TimeoutError:
                            pass
                    self.reset_timing()
                    return

        if sleep_ms > 0:
            log.debug('Sleeping for %.1fms (pts=%.1f)', sleep_ms, pts_ms)
            await asyncio.sleep(sleep_ms / 1000)

    def _is_behind(self, delta: float) -> bool:
        """Check if this stream is behind its sync partner."""
        return (
            self._sync_partner is not None
            and self._sync_partner._pts is not None
            and delta < -self._sync_tolerance_ms
        )

    def _is_ahead(self, delta: float, frametime_ms: float) -> bool:
        """Check if this stream is ahead of its sync partner."""
        return (
            self._sync_partner is not None
            and self._sync_partner._pts is not None
            and delta > self._sync_tolerance_ms
        )
