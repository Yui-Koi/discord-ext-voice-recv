"""
Tests for frame pacing and A/V synchronization.
"""

import sys
import os
import asyncio
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from media.pacer import FramePacer


class TestFramePacerBasic(unittest.TestCase):
    """Test basic FramePacer functionality."""

    def test_initialization(self):
        pacer = FramePacer(clock_rate=90000)
        self.assertEqual(pacer.clock_rate, 90000)
        self.assertIsNone(pacer.pts)
        self.assertIsNone(pacer._start_time)
        self.assertIsNone(pacer._start_pts)

    def test_pts_update(self):
        pacer = FramePacer(clock_rate=90000)
        pacer.update_pts(100.0)
        self.assertEqual(pacer.pts, 100.0)

    def test_reset_timing(self):
        pacer = FramePacer(clock_rate=90000)
        pacer._start_time = 1000.0
        pacer._start_pts = 50.0
        pacer.reset_timing()
        self.assertIsNone(pacer._start_time)
        self.assertIsNone(pacer._start_pts)

    def test_no_sleep_mode(self):
        """In no_sleep mode, pace() returns immediately."""
        async def run():
            pacer = FramePacer(clock_rate=90000)
            pacer.no_sleep = True
            # Should return instantly without sleeping
            await pacer.pace(100.0, 33.33)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()

    def test_sync_tolerance(self):
        pacer = FramePacer(clock_rate=90000, sync_tolerance_ms=50.0)
        self.assertEqual(pacer.sync_tolerance_ms, 50.0)

        pacer.sync_tolerance_ms = 10.0
        self.assertEqual(pacer.sync_tolerance_ms, 10.0)

        # Negative should be ignored
        pacer.sync_tolerance_ms = -1.0
        self.assertEqual(pacer.sync_tolerance_ms, 10.0)

    def test_circular_sync_rejected(self):
        """Cannot set two pacers to sync with each other."""
        pacer_a = FramePacer(clock_rate=90000)
        pacer_b = FramePacer(clock_rate=48000)
        pacer_a.sync_partner = pacer_b

        with self.assertRaises(ValueError):
            pacer_b.sync_partner = pacer_a


class TestFramePacerTiming(unittest.TestCase):
    """Test timing behavior of the frame pacer."""

    def test_first_frame_no_sleep(self):
        """First frame should initialize timing, not sleep."""
        async def run():
            pacer = FramePacer(clock_rate=90000)
            # First frame at pts=0 with frametime=33.33ms
            # Should not sleep because start_time is set to 'now'
            await pacer.pace(0.0, 33.33)
            self.assertIsNotNone(pacer._start_time)
            self.assertEqual(pacer._start_pts, 0.0)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()

    def test_second_frame_may_sleep(self):
        """Second frame at pts=33.33 should have ~33ms elapsed since first."""
        async def run():
            pacer = FramePacer(clock_rate=90000)
            await pacer.pace(0.0, 33.33)
            # Immediately pace the second frame
            # elapsed time is ~0ms but expected is 33.33ms
            # So it should sleep ~33ms
            import time
            start = time.monotonic()
            await pacer.pace(33.33, 33.33)
            elapsed = (time.monotonic() - start) * 1000
            # Should have slept approximately 33ms
            self.assertGreater(elapsed, 20)  # at least 20ms

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()


class TestFramePacerSync(unittest.TestCase):
    """Test A/V synchronization between two pacers."""

    def test_sync_partner_property(self):
        video_pacer = FramePacer(clock_rate=90000)
        audio_pacer = FramePacer(clock_rate=48000)

        video_pacer.sync_partner = audio_pacer
        self.assertIs(video_pacer.sync_partner, audio_pacer)

        video_pacer.sync_partner = None
        self.assertIsNone(video_pacer.sync_partner)

    def test_no_sync_when_no_partner(self):
        """Without sync partner, no sync logic runs."""
        async def run():
            pacer = FramePacer(clock_rate=90000)
            self.assertIsNone(pacer.sync_partner)
            # Should work fine without partner
            await pacer.pace(0.0, 33.33)
            await pacer.pace(33.33, 33.33)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()

    def test_behind_skips_sleep(self):
        """Video behind audio should skip sleep."""
        async def run():
            video_pacer = FramePacer(clock_rate=90000, sync_tolerance_ms=20.0)
            audio_pacer = FramePacer(clock_rate=48000)

            video_pacer.sync_partner = audio_pacer

            # Initialize both
            await video_pacer.pace(0.0, 33.33)
            await audio_pacer.pace(0.0, 20.0)

            # Audio advances far ahead
            audio_pacer.update_pts(100.0)

            # Video at pts=33.33 is behind audio at 100ms
            # delta = 33.33 - 100 = -66.67ms < -20ms tolerance -> behind
            import time
            start = time.monotonic()
            await video_pacer.pace(33.33, 33.33)
            elapsed = (time.monotonic() - start) * 1000
            # Should have skipped sleep (returned immediately)
            self.assertLess(elapsed, 10)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()

    def test_ahead_waits(self):
        """Video ahead of audio should wait."""
        async def run():
            video_pacer = FramePacer(clock_rate=90000, sync_tolerance_ms=20.0)
            audio_pacer = FramePacer(clock_rate=48000)

            video_pacer.sync_partner = audio_pacer

            # Initialize both
            await video_pacer.pace(0.0, 33.33)
            await audio_pacer.pace(0.0, 20.0)

            # Audio stays at pts=0, video tries to go to pts=100
            # delta = 100 - 0 = 100ms > 20ms tolerance -> ahead
            # But the while loop will also check, and audio won't advance
            # So this would block. Let's test with a timeout.
            import time
            start = time.monotonic()

            # Advance audio in a task
            async def advance_audio():
                await asyncio.sleep(0.05)  # 50ms
                audio_pacer.update_pts(110.0)  # catch up

            task = asyncio.create_task(advance_audio())
            await video_pacer.pace(100.0, 33.33)
            elapsed = (time.monotonic() - start) * 1000
            await task

            # Should have waited for audio to catch up
            self.assertGreater(elapsed, 30)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()

    def test_sync_disabled_no_behavior_change(self):
        """When sync is disabled, no sync logic applies."""
        async def run():
            video_pacer = FramePacer(clock_rate=90000)
            audio_pacer = FramePacer(clock_rate=48000)
            video_pacer.sync_partner = audio_pacer
            video_pacer.sync_enabled = False

            # Initialize
            await video_pacer.pace(0.0, 33.33)

            # Audio far ahead but sync is disabled
            audio_pacer.update_pts(1000.0)

            # Should behave as if no sync partner exists
            # Just normal timing, not skipping
            import time
            start = time.monotonic()
            await video_pacer.pace(33.33, 33.33)
            elapsed = (time.monotonic() - start) * 1000
            # Should have done normal timing, not skipped
            self.assertGreater(elapsed, 15)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()


class TestFramePacerNoSleepResetsTiming(unittest.TestCase):
    """Verify that setting no_sleep resets timing compensation."""

    def test_no_sleep_setter_resets(self):
        pacer = FramePacer(clock_rate=90000)
        pacer._start_time = 1000.0
        pacer._start_pts = 50.0

        pacer.no_sleep = True
        self.assertIsNone(pacer._start_time)
        self.assertIsNone(pacer._start_pts)


if __name__ == '__main__':
    unittest.main()
