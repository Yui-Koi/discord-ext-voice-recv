"""
Tests for FFmpeg pipeline and NUT demuxing.

Tests:
- FFmpeg command construction (flag validation)
- Opus TOC byte duration parsing
- FFmpeg subprocess startup/shutdown
- Real FFmpeg + PyAV demuxer pipeline (needs test media)
"""

import sys
import os
import struct
import asyncio
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from media.ffmpeg import StreamOptions, FFmpegProcess
from media.demux import parse_opus_duration, FrameType


class TestStreamOptions(unittest.TestCase):
    """Test FFmpeg command construction."""

    def test_basic_command(self):
        opts = StreamOptions(url='input.mp4')
        ffmpeg = FFmpegProcess(opts)
        cmd = ffmpeg.command

        self.assertIn('ffmpeg', cmd)
        self.assertIn('-i', cmd)
        self.assertIn('input.mp4', cmd)
        self.assertIn('-vcodec', cmd)
        self.assertIn('libx264', cmd)
        self.assertIn('-preset', cmd)
        self.assertIn('superfast', cmd)
        self.assertIn('-f', cmd)
        self.assertIn('nut', cmd)
        self.assertIn('pipe:1', cmd)

    def test_critical_flags_present(self):
        """All critical flags from Node.js reference must be present."""
        opts = StreamOptions(url='test.mp4')
        ffmpeg = FFmpegProcess(opts)
        cmd = ffmpeg.command

        # No B-frames
        self.assertIn('-bf', cmd)
        bf_idx = cmd.index('-bf')
        self.assertEqual(cmd[bf_idx + 1], '0')

        # Forced IDR
        self.assertIn('-forced-idr', cmd)
        idr_idx = cmd.index('-forced-idr')
        self.assertEqual(cmd[idr_idx + 1], '1')

        # yuv420p pixel format
        self.assertIn('-pix_fmt', cmd)
        pix_idx = cmd.index('-pix_fmt')
        self.assertEqual(cmd[pix_idx + 1], 'yuv420p')

        # Force keyframes every 1s
        self.assertIn('-force_key_frames', cmd)
        kf_idx = cmd.index('-force_key_frames')
        self.assertIn('gte(t,n_forced*1)', cmd[kf_idx + 1])

    def test_bitrate_settings(self):
        opts = StreamOptions(url='test.mp4', bitrate_video=3000, bitrate_video_max=5000)
        ffmpeg = FFmpegProcess(opts)
        cmd = ffmpeg.command

        bv_idx = cmd.index('-b:v')
        self.assertEqual(cmd[bv_idx + 1], '3000k')

        max_idx = cmd.index('-maxrate:v')
        self.assertEqual(cmd[max_idx + 1], '5000k')

        buf_idx = cmd.index('-bufsize:v')
        self.assertEqual(cmd[buf_idx + 1], '1500k')  # bitrate / 2

    def test_audio_settings(self):
        opts = StreamOptions(url='test.mp4', include_audio=True, bitrate_audio=96)
        ffmpeg = FFmpegProcess(opts)
        cmd = ffmpeg.command

        self.assertIn('-acodec', cmd)
        ac_idx = cmd.index('-acodec')
        self.assertEqual(cmd[ac_idx + 1], 'libopus')

        ba_idx = cmd.index('-b:a')
        self.assertEqual(cmd[ba_idx + 1], '96k')

        self.assertIn('-ac', cmd)
        ac_ch_idx = cmd.index('-ac')
        self.assertEqual(cmd[ac_ch_idx + 1], '2')

        self.assertIn('-ar', cmd)
        ar_idx = cmd.index('-ar')
        self.assertEqual(cmd[ar_idx + 1], '48000')

    def test_no_audio(self):
        opts = StreamOptions(url='test.mp4', include_audio=False)
        ffmpeg = FFmpegProcess(opts)
        cmd = ffmpeg.command

        self.assertNotIn('-acodec', cmd)

    def test_no_transcoding(self):
        opts = StreamOptions(url='test.mp4', no_transcoding=True)
        ffmpeg = FFmpegProcess(opts)
        cmd = ffmpeg.command

        vcodec_idx = cmd.index('-vcodec')
        self.assertEqual(cmd[vcodec_idx + 1], 'copy')

    def test_frame_rate(self):
        opts = StreamOptions(url='test.mp4', frame_rate=30)
        ffmpeg = FFmpegProcess(opts)
        cmd = ffmpeg.command

        self.assertIn('-r', cmd)
        r_idx = cmd.index('-r')
        self.assertEqual(cmd[r_idx + 1], '30')

    def test_custom_flags(self):
        opts = StreamOptions(url='test.mp4', custom_flags=['-t', '60'])
        ffmpeg = FFmpegProcess(opts)
        cmd = ffmpeg.command

        self.assertIn('-t', cmd)
        t_idx = cmd.index('-t')
        self.assertEqual(cmd[t_idx + 1], '60')

    def test_scale_filter(self):
        opts = StreamOptions(url='test.mp4', width=1280, height=720)
        ffmpeg = FFmpegProcess(opts)
        cmd = ffmpeg.command

        self.assertIn('-vf', cmd)
        vf_idx = cmd.index('-vf')
        self.assertEqual(cmd[vf_idx + 1], 'scale=1280:720')

    def test_nut_output_format(self):
        opts = StreamOptions(url='test.mp4')
        ffmpeg = FFmpegProcess(opts)
        cmd = ffmpeg.command

        self.assertIn('-f', cmd)
        f_idx = cmd.index('-f')
        self.assertEqual(cmd[f_idx + 1], 'nut')
        self.assertEqual(cmd[-1], 'pipe:1')


class TestOpusDuration(unittest.TestCase):
    """Test Opus TOC byte duration parsing (RFC 6716)."""

    def test_silk_narrowband_20ms(self):
        """Config 1: SILK narrowband 20ms, mono (code=0)."""
        # config=1 -> bits 7-3 = 00010 -> toc = 0b00010000 = 0x10
        # code=0 -> 1 frame
        toc = (1 << 3) | 0  # config=1, stereo=0, code=0
        frame = bytes([toc]) + b'\x00' * 10
        duration = parse_opus_duration(frame)
        self.assertEqual(duration, 960)  # 20ms * 48 samples/ms

    def test_celt_fullband_20ms(self):
        """Config 31: CELT fullband 20ms, code=0."""
        toc = (31 << 3) | 0  # config=31, code=0
        frame = bytes([toc])
        duration = parse_opus_duration(frame)
        self.assertEqual(duration, 960)  # 20ms * 48

    def test_celt_narrowband_2_5ms(self):
        """Config 16: CELT narrowband 2.5ms, code=0."""
        toc = (16 << 3) | 0
        frame = bytes([toc])
        duration = parse_opus_duration(frame)
        self.assertEqual(duration, 120)  # 2.5ms * 48

    def test_two_frames(self):
        """Code 1 = 2 frames."""
        toc = (16 << 3) | 1  # config=16, code=1
        frame = bytes([toc])
        duration = parse_opus_duration(frame)
        self.assertEqual(duration, 240)  # 120 * 2 frames

    def test_code_3_frame_count(self):
        """Code 3 = frame count in next byte."""
        toc = (16 << 3) | 3  # config=16, code=3
        # Next byte: frame count = 5 (bits 0-5)
        frame = bytes([toc, 5])
        duration = parse_opus_duration(frame)
        self.assertEqual(duration, 600)  # 120 * 5 frames

    def test_empty_frame(self):
        self.assertEqual(parse_opus_duration(b''), 0)

    def test_silk_wideband_60ms(self):
        """Config 11: SILK wideband 60ms."""
        toc = (11 << 3) | 0
        frame = bytes([toc])
        duration = parse_opus_duration(frame)
        self.assertEqual(duration, 2880)  # 60ms * 48

    def test_hybrid_super_wideband_10ms(self):
        """Config 12: Hybrid super-wideband 10ms."""
        toc = (12 << 3) | 0
        frame = bytes([toc])
        duration = parse_opus_duration(frame)
        self.assertEqual(duration, 480)  # 10ms * 48


class TestFFmpegProcess(unittest.TestCase):
    """Test FFmpeg subprocess lifecycle."""

    def test_command_property_returns_copy(self):
        opts = StreamOptions(url='test.mp4')
        ffmpeg = FFmpegProcess(opts)
        cmd1 = ffmpeg.command
        cmd2 = ffmpeg.command
        self.assertIsNot(cmd1, cmd2)
        self.assertEqual(cmd1, cmd2)

    def test_process_none_before_start(self):
        opts = StreamOptions(url='test.mp4')
        ffmpeg = FFmpegProcess(opts)
        self.assertIsNone(ffmpeg.process)
        self.assertIsNone(ffmpeg.stdout)
        self.assertIsNone(ffmpeg.stderr)


class TestFFmpegDemuxIntegration(unittest.TestCase):
    """Integration test: run FFmpeg on a real file and demux."""

    def _create_test_video(self, path: str):
        """Create a short test video using FFmpeg."""
        import subprocess
        cmd = [
            'ffmpeg', '-y',
            '-f', 'lavfi', '-i', 'testsrc=duration=2:size=320x240:rate=30',
            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
            '-vcodec', 'libx264', '-preset', 'superfast',
            '-pix_fmt', 'yuv420p', '-bf', '0',
            '-forced-idr', '1', '-force_key_frames', 'expr:gte(t,n_forced*1)',
            '-acodec', 'libopus',
            '-ac', '2', '-ar', '48000',
            path,
        ]
        result = subprocess.run(cmd, capture_output=True)
        return result.returncode == 0

    def test_full_ffmpeg_demux_pipeline(self):
        """Run FFmpeg, demux NUT output, verify we get video+audio frames."""
        test_file = '/tmp/test_phase2_input.mp4'

        # Create test input
        if not os.path.exists(test_file):
            created = self._create_test_video(test_file)
            self.assertTrue(created, 'Failed to create test video')

        # Run FFmpeg producing NUT to stdout
        opts = StreamOptions(url=test_file, bitrate_video=1000)
        ffmpeg = FFmpegProcess(opts)

        async def run_test():
            proc = await ffmpeg.start()
            from media.demux import Demuxer

            demuxer = Demuxer(format='nut')
            video_count = 0
            audio_count = 0
            keyframe_count = 0

            async for frame in demuxer.demux(proc.stdout):
                if frame.frame_type == FrameType.VIDEO:
                    video_count += 1
                    if frame.is_keyframe:
                        keyframe_count += 1
                    # Verify frame has data
                    self.assertGreater(len(frame.data), 0)
                    # Verify timing
                    self.assertGreaterEqual(frame.pts, 0)
                elif frame.frame_type == FrameType.AUDIO:
                    audio_count += 1
                    self.assertGreater(len(frame.data), 0)

                # Don't read forever
                if video_count + audio_count > 100:
                    break

            await ffmpeg.stop()

            # We should have gotten video and audio frames
            self.assertGreater(video_count, 0, 'No video frames demuxed')
            self.assertGreater(audio_count, 0, 'No audio frames demuxed')
            self.assertGreater(keyframe_count, 0, 'No keyframes found')

            return video_count, audio_count, keyframe_count

        loop = asyncio.new_event_loop()
        vc, ac, kc = loop.run_until_complete(run_test())
        loop.close()

        print(f'Demuxed {vc} video frames ({kc} keyframes), {ac} audio frames')


if __name__ == '__main__':
    unittest.main()
