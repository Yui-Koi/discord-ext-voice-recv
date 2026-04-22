"""
Phase 6 integration tests.

Tests component wiring and compatibility without requiring FFmpeg
or live Discord connections. The FFmpeg pipeline is already validated
in test_ffmpeg_demux.py.

Coverage:
1. Package exports (all public symbols importable)
2. StreamConnection + VideoSender wiring (SSRCs, keys, encryption)
3. Protocol round-trips (stream keys, codec configs, video payloads)
4. SPS VUI rewriting with various profiles
5. Frame pacing with realistic timing
6. DAVE protocol integration (session init, passthrough, transitions)
7. voice-recv compatibility (imports, decryptor, RTP format, hooks)
"""

import sys
import os
import struct
import asyncio
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    import nacl.secret
    HAS_NACL = True
except ImportError:
    HAS_NACL = False

from rtp.serialize import build_rtp_header, build_rtp_packet, set_marker, build_rtcp_sr
from rtp.h264 import H264Packetizer, split_nalu, get_nalu_type, H264NalUnitTypes
from rtp.crypto import TransportEncryptor
from protocol.vui import rewrite_sps_vui, BitstreamReader, BitstreamWriter
from protocol.types import (
    CodecConfig, CODEC_OPUS, CODEC_H264, ALL_CODECS,
    STREAMS_SIMULCAST, SUPPORTED_ENCRYPTION_MODES,
    GatewayOpCodes, VideoAttributes,
    generate_stream_key, parse_stream_key,
    build_video_payload, build_video_off_payload,
)
from media.ffmpeg import StreamOptions, FFmpegProcess
from media.demux import FrameType, parse_opus_duration
from media.pacer import FramePacer
from stream_connection import (
    StreamConnection, StreamConnectionState,
    StreamReadyParams, StreamSessionDescription,
    VoiceOpCodes, VoiceOpCodesBinary,
)
from voice_send import VideoSender


class TestPackageExports(unittest.TestCase):
    """Verify all public API symbols are importable."""

    def test_main_package_imports(self):
        import discord_video_stream as dvs
        self.assertTrue(hasattr(dvs, 'VideoStreamer'))
        self.assertTrue(hasattr(dvs, 'StreamConnection'))
        self.assertTrue(hasattr(dvs, 'VideoSender'))
        self.assertTrue(hasattr(dvs, 'StreamOptions'))
        self.assertTrue(hasattr(dvs, 'FFmpegProcess'))
        self.assertTrue(hasattr(dvs, 'Demuxer'))
        self.assertTrue(hasattr(dvs, 'FramePacer'))
        self.assertTrue(hasattr(dvs, 'FrameType'))
        self.assertTrue(hasattr(dvs, 'build_rtp_header'))
        self.assertTrue(hasattr(dvs, 'TransportEncryptor'))
        self.assertTrue(hasattr(dvs, 'H264Packetizer'))
        self.assertTrue(hasattr(dvs, 'split_nalu'))
        self.assertTrue(hasattr(dvs, 'CODEC_H264'))
        self.assertTrue(hasattr(dvs, 'ALL_CODECS'))
        self.assertTrue(hasattr(dvs, 'GatewayOpCodes'))
        self.assertTrue(hasattr(dvs, 'VideoAttributes'))
        self.assertTrue(hasattr(dvs, 'rewrite_sps_vui'))

    def test_compat_imports(self):
        from discord_video_stream.compat import VoiceSendRecvClient, HAS_VOICE_RECV
        self.assertIsInstance(HAS_VOICE_RECV, bool)

    def test_compat_has_voice_recv(self):
        from discord.ext.voice_recv import VoiceRecvClient
        self.assertTrue(issubclass(VoiceRecvClient, __import__('discord').VoiceClient))


@unittest.skipUnless(HAS_NACL, "pynacl not installed")
class TestStreamConnectionVideoSenderWiring(unittest.TestCase):
    """Test StreamConnection + VideoSender integration."""

    def test_sender_uses_connection_ssrcs(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_ready({
            'ssrc': 1000, 'ip': '1.2.3.4', 'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [{'type': 'video', 'ssrc': 2000, 'rtx_ssrc': 2001,
                         'rid': '100', 'quality': 100, 'active': True}],
        })
        secret_key = nacl.utils.random(32)
        conn._handle_select_protocol_ack({
            'secret_key': list(secret_key),
            'mode': 'aead_xchacha20_poly1305_rtpsize',
            'dave_protocol_version': 0,
        })

        sender = VideoSender(conn)
        sender.start()

        packets = []
        sender.set_send_callback(lambda pkt, ip=None, port=None: packets.append(pkt))

        frame = b'\x00\x00\x00\x01\x65' + b'\x88' * 20
        sender.send_frame(frame, 0.0, 33.33)

        for pkt in packets:
            ssrc = struct.unpack('>I', pkt[8:12])[0]
            self.assertEqual(ssrc, 2000)

    def test_sender_uses_connection_secret_key(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_ready({
            'ssrc': 1000, 'ip': '1.2.3.4', 'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [{'type': 'video', 'ssrc': 2000, 'rtx_ssrc': 2001,
                         'rid': '100', 'quality': 100, 'active': True}],
        })
        secret_key = nacl.utils.random(32)
        conn._handle_select_protocol_ack({
            'secret_key': list(secret_key),
            'mode': 'aead_xchacha20_poly1305_rtpsize',
            'dave_protocol_version': 0,
        })

        sender = VideoSender(conn)
        sender.start()

        packets = []
        sender.set_send_callback(lambda pkt, ip=None, port=None: packets.append(pkt))

        frame = b'\x00\x00\x00\x01\x65' + b'\x88' * 20
        sender.send_frame(frame, 0.0, 33.33)

        pkt = packets[0]
        header = bytes(pkt[:12])
        encrypted_data = pkt[12:]
        nonce_bytes = encrypted_data[-4:]
        ciphertext = encrypted_data[:-4]

        nonce = bytearray(24)
        nonce[:4] = nonce_bytes

        box = nacl.secret.Aead(secret_key)
        decrypted = box.decrypt(bytes(ciphertext), header, bytes(nonce))
        self.assertGreater(len(decrypted), 0)

    def test_sender_timestamp_conversion(self):
        """pts_ms * 90 should give correct RTP timestamp."""
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_ready({
            'ssrc': 1000, 'ip': '1.2.3.4', 'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [{'type': 'video', 'ssrc': 2000, 'rtx_ssrc': 2001,
                         'rid': '100', 'quality': 100, 'active': True}],
        })
        conn._handle_select_protocol_ack({
            'secret_key': list(nacl.utils.random(32)),
            'mode': 'aead_xchacha20_poly1305_rtpsize',
            'dave_protocol_version': 0,
        })

        sender = VideoSender(conn)
        sender.start()

        packets = []
        sender.set_send_callback(lambda pkt, ip=None, port=None: packets.append(pkt))

        frame = b'\x00\x00\x00\x01\x65' + b'\x88' * 20
        sender.send_frame(frame, 100.0, 33.33)  # 100ms -> 9000 ticks

        for pkt in packets:
            ts = struct.unpack('>I', pkt[4:8])[0]
            self.assertEqual(ts, 9000)

    def test_sender_marker_bit_on_last_packet(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_ready({
            'ssrc': 1000, 'ip': '1.2.3.4', 'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [{'type': 'video', 'ssrc': 2000, 'rtx_ssrc': 2001,
                         'rid': '100', 'quality': 100, 'active': True}],
        })
        conn._handle_select_protocol_ack({
            'secret_key': list(nacl.utils.random(32)),
            'mode': 'aead_xchacha20_poly1305_rtpsize',
            'dave_protocol_version': 0,
        })

        sender = VideoSender(conn)
        sender.start()

        packets = []
        sender.set_send_callback(lambda pkt, ip=None, port=None: packets.append(pkt))

        # Large frame that produces multiple packets
        frame = b'\x00\x00\x00\x01\x65' + b'\x88' * 3000
        sender.send_frame(frame, 0.0, 33.33)

        self.assertGreater(len(packets), 1)
        for pkt in packets[:-1]:
            self.assertEqual(pkt[1] & 0x80, 0)
        self.assertEqual(packets[-1][1] & 0x80, 0x80)

    def test_sender_continuous_sequence(self):
        """Sequence should increment across multiple frames."""
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_ready({
            'ssrc': 1000, 'ip': '1.2.3.4', 'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [{'type': 'video', 'ssrc': 2000, 'rtx_ssrc': 2001,
                         'rid': '100', 'quality': 100, 'active': True}],
        })
        conn._handle_select_protocol_ack({
            'secret_key': list(nacl.utils.random(32)),
            'mode': 'aead_xchacha20_poly1305_rtpsize',
            'dave_protocol_version': 0,
        })

        sender = VideoSender(conn)
        sender.start()

        all_packets = []
        sender.set_send_callback(lambda pkt, ip=None, port=None: all_packets.append(pkt))

        frame1 = b'\x00\x00\x00\x01\x67' + b'\x42' * 10
        frame2 = b'\x00\x00\x00\x01\x65' + b'\x88' * 20
        sender.send_frame(frame1, 0.0, 33.33)
        sender.send_frame(frame2, 33.33, 33.33)

        seqs = [struct.unpack('>H', pkt[2:4])[0] for pkt in all_packets]
        for i in range(1, len(seqs)):
            self.assertEqual(seqs[i], (seqs[i-1] + 1) & 0xFFFF)


@unittest.skipUnless(HAS_NACL, "pynacl not installed")
class TestStreamEndpointFix(unittest.TestCase):
    """Test that video packets are sent to the STREAM server's endpoint,
    not the main voice connection's endpoint. This is the error 2012 fix."""

    def test_sender_uses_stream_endpoint(self):
        """VideoSender must use the stream server's IP:port from READY."""
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        # Stream server is at 10.0.0.1:5000 (different from voice)
        conn._handle_ready({
            'ssrc': 1000, 'ip': '10.0.0.1', 'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [{'type': 'video', 'ssrc': 2000, 'rtx_ssrc': 2001,
                         'rid': '100', 'quality': 100, 'active': True}],
        })
        conn._handle_select_protocol_ack({
            'secret_key': list(nacl.utils.random(32)),
            'mode': 'aead_xchacha20_poly1305_rtpsize',
            'dave_protocol_version': 0,
        })

        sender = VideoSender(conn)
        sender.start()

        sent = []
        sender.set_send_callback(lambda pkt, ip, port: sent.append((pkt, ip, port)))

        frame = b'\x00\x00\x00\x01\x65' + b'\x88' * 20
        sender.send_frame(frame, 0.0, 33.33)

        self.assertGreater(len(sent), 0)
        for _, ip, port in sent:
            self.assertEqual(ip, '10.0.0.1', 'Must send to stream server IP')
            self.assertEqual(port, 5000, 'Must send to stream server port')

    def test_sender_not_using_main_voice_endpoint(self):
        """VideoSender must NOT use the main voice connection's endpoint."""
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        # Stream server is at 10.0.0.1:5000
        conn._handle_ready({
            'ssrc': 1000, 'ip': '10.0.0.1', 'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [{'type': 'video', 'ssrc': 2000, 'rtx_ssrc': 2001,
                         'rid': '100', 'quality': 100, 'active': True}],
        })
        conn._handle_select_protocol_ack({
            'secret_key': list(nacl.utils.random(32)),
            'mode': 'aead_xchacha20_poly1305_rtpsize',
            'dave_protocol_version': 0,
        })

        sender = VideoSender(conn)
        sender.start()

        sent = []
        sender.set_send_callback(lambda pkt, ip, port: sent.append((pkt, ip, port)))

        frame = b'\x00\x00\x00\x01\x65' + b'\x88' * 20
        sender.send_frame(frame, 0.0, 33.33)

        # Verify NOT sending to a different endpoint
        for _, ip, port in sent:
            self.assertNotEqual(ip, '1.2.3.4', 'Must NOT send to voice server IP')
            self.assertNotEqual(port, 1234, 'Must NOT send to voice server port')

    def test_stream_ready_params_ip_port(self):
        """READY params must correctly expose IP and port."""
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_ready({
            'ssrc': 1000, 'ip': '10.0.0.1', 'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [{'type': 'video', 'ssrc': 2000, 'rtx_ssrc': 2001,
                         'rid': '100', 'quality': 100, 'active': True}],
        })

        self.assertIsNotNone(conn.ready_params)
        self.assertEqual(conn.ready_params.ip, '10.0.0.1')
        self.assertEqual(conn.ready_params.port, 5000)


class TestProtocolRoundTrip(unittest.TestCase):
    """Test protocol types round-trip correctly."""

    def test_stream_key_roundtrip_guild(self):
        key = generate_stream_key('guild', '111', '222', '333')
        parsed = parse_stream_key(key)
        key2 = generate_stream_key(parsed['type'], parsed['guild_id'],
                                    parsed['channel_id'], parsed['user_id'])
        self.assertEqual(key, key2)

    def test_stream_key_roundtrip_call(self):
        key = generate_stream_key('call', None, '222', '333')
        parsed = parse_stream_key(key)
        key2 = generate_stream_key(parsed['type'], parsed['guild_id'],
                                    parsed['channel_id'], parsed['user_id'])
        self.assertEqual(key, key2)

    def test_video_payload_roundtrip(self):
        attrs = VideoAttributes(width=1280, height=720, fps=60)
        payload = build_video_payload(1000, 2000, 2001, attrs)
        stream = payload['streams'][0]
        self.assertEqual(stream['max_resolution']['width'], 1280)
        self.assertEqual(stream['max_resolution']['height'], 720)
        self.assertEqual(stream['max_framerate'], 60)

    def test_codec_config_roundtrip(self):
        for codec in ALL_CODECS:
            d = codec.to_dict()
            self.assertEqual(d['name'], codec.name)
            self.assertEqual(d['payload_type'], codec.payload_type)
            self.assertEqual(d['clockRate'], codec.clock_rate)


class TestSPSVUIIntegration(unittest.TestCase):
    """Test SPS VUI rewriting with various profiles."""

    def _make_sps(self, profile_idc=66, level_idc=30, has_vui=False):
        writer = BitstreamWriter()
        writer.write_unsigned(profile_idc, 8)
        writer.write_unsigned(0, 8)
        writer.write_unsigned(level_idc, 8)
        writer.write_ue(0)
        writer.write_ue(0)
        writer.write_ue(0)
        writer.write_ue(0)
        writer.write_ue(4)
        writer.write_bits(0, 1)
        writer.write_ue(21)
        writer.write_ue(17)
        writer.write_bits(1, 1)
        writer.write_bits(0, 1)
        writer.write_bits(0, 1)
        writer.write_bits(1 if has_vui else 0, 1)
        if has_vui:
            # Valid minimal VUI: aspect=0, overscan=0, video_signal=0,
            # chroma_loc=0, timing=0, nal_hrd=0, vcl_hrd=0, pic_struct=0,
            # bitstream_restriction=0
            writer.write_bits(0, 1)  # aspect_ratio_info_present_flag
            writer.write_bits(0, 1)  # overscan_info_present_flag
            writer.write_bits(0, 1)  # video_signal_type_present_flag
            writer.write_bits(0, 1)  # chroma_loc_info_present_flag
            writer.write_bits(0, 1)  # timing_info_present_flag
            writer.write_bits(0, 1)  # nal_hrd_parameters_present_flag
            writer.write_bits(0, 1)  # vcl_hrd_parameters_present_flag
            writer.write_bits(0, 1)  # pic_struct_present_flag
            writer.write_bits(0, 1)  # bitstream_restriction_flag
        writer.write_bits(1, 1)
        writer.flush()
        return bytes([0x67]) + writer.to_bytes()

    def test_baseline_no_vui(self):
        sps = self._make_sps(profile_idc=66, has_vui=False)
        result = rewrite_sps_vui(sps)
        self.assertNotEqual(result, sps)
        self.assertEqual(result[0], 0x67)
        self.assertGreater(len(result), len(sps))

    def test_baseline_with_vui(self):
        sps = self._make_sps(profile_idc=66, has_vui=True)
        result = rewrite_sps_vui(sps)
        self.assertNotEqual(result, sps)

    def test_constrained_baseline(self):
        sps = self._make_sps(profile_idc=66, level_idc=30)
        result = rewrite_sps_vui(sps)
        reader = BitstreamReader(result[1:])
        profile = reader.read_unsigned(8)
        self.assertEqual(profile, 66)

    def test_main_profile(self):
        sps = self._make_sps(profile_idc=77, level_idc=40)
        result = rewrite_sps_vui(sps)
        reader = BitstreamReader(result[1:])
        profile = reader.read_unsigned(8)
        self.assertEqual(profile, 77)

    def test_high_profile(self):
        """High profile (100) has extra SPS fields after profile_idc."""
        writer = BitstreamWriter()
        writer.write_unsigned(100, 8)  # profile_idc (High)
        writer.write_unsigned(0, 8)    # constraint_flags
        writer.write_unsigned(40, 8)   # level_idc
        writer.write_ue(0)             # sps_id
        writer.write_ue(0)             # chroma_format_idc
        writer.write_ue(0)             # bit_depth_luma
        writer.write_ue(0)             # bit_depth_chroma
        writer.write_bits(0, 1)        # qpprime_y_zero
        writer.write_bits(0, 1)        # seq_scaling_matrix
        writer.write_ue(0)
        writer.write_ue(0)
        writer.write_ue(0)
        writer.write_ue(0)
        writer.write_ue(4)
        writer.write_bits(0, 1)
        writer.write_ue(21)
        writer.write_ue(17)
        writer.write_bits(1, 1)
        writer.write_bits(0, 1)
        writer.write_bits(0, 1)
        writer.write_bits(0, 1)  # no VUI
        writer.write_bits(1, 1)
        writer.flush()
        sps = bytes([0x67]) + writer.to_bytes()

        result = rewrite_sps_vui(sps)
        reader = BitstreamReader(result[1:])
        profile = reader.read_unsigned(8)
        self.assertEqual(profile, 100)


class TestFramePacingIntegration(unittest.TestCase):
    """Test frame pacing with realistic timing."""

    def test_30fps_pacing(self):
        async def run():
            pacer = FramePacer(clock_rate=90000)
            import time
            start = time.monotonic()
            for i in range(10):
                await pacer.pace(i * 33.33, 33.33)
            elapsed = (time.monotonic() - start) * 1000
            self.assertGreater(elapsed, 200)
            self.assertLess(elapsed, 500)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()

    def test_av_sync(self):
        """Video pacer should not block when audio stays close."""
        async def run():
            video_pacer = FramePacer(clock_rate=90000, sync_tolerance_ms=50.0)
            audio_pacer = FramePacer(clock_rate=48000)
            video_pacer.sync_partner = audio_pacer
            import time
            start = time.monotonic()
            for i in range(10):
                # Audio advances close to video (within tolerance)
                audio_pacer.update_pts(i * 33.0)
                await video_pacer.pace(i * 33.33, 33.33)
            elapsed = (time.monotonic() - start) * 1000
            # Should complete in reasonable time (not hang)
            self.assertLess(elapsed, 2000)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()

    def test_pacer_no_sleep_mode(self):
        async def run():
            pacer = FramePacer(clock_rate=90000)
            pacer.no_sleep = True
            import time
            start = time.monotonic()
            for i in range(100):
                await pacer.pace(i * 33.33, 33.33)
            elapsed = (time.monotonic() - start) * 1000
            self.assertLess(elapsed, 50, 'no_sleep should return immediately')

        loop = asyncio.new_event_loop()
        loop.run_until_complete(run())
        loop.close()


class TestDAVEProtocolIntegration(unittest.TestCase):
    """Test DAVE session integration."""

    @unittest.skipUnless(HAS_NACL, "pynacl not installed")
    def test_dave_session_init_on_protocol_ack(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_ready({
            'ssrc': 1000, 'ip': '1.2.3.4', 'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [{'type': 'video', 'ssrc': 2000, 'rtx_ssrc': 2001,
                         'rid': '100', 'quality': 100, 'active': True}],
        })

        sent_binary = []
        conn._send_binary = lambda op, data: sent_binary.append((op, data))

        conn._handle_select_protocol_ack({
            'secret_key': list(nacl.utils.random(32)),
            'mode': 'aead_xchacha20_poly1305_rtpsize',
            'dave_protocol_version': 1,
        })

        # _handle_select_protocol_ack sets version but doesn't call _init_dave
        # That happens in the JSON handler. Call it explicitly to test init.
        # Mock davey since it's not installed in this sandbox
        mock_davey = MagicMock()
        mock_davey.DaveSession.return_value = MagicMock()
        mock_davey.DaveSession.return_value.get_serialized_key_package.return_value = b'\x01\x02'
        with patch.dict('sys.modules', {'davey': mock_davey}):
            conn._init_dave()

        self.assertIsNotNone(conn.dave_session)
        self.assertEqual(len(sent_binary), 1)
        op, data = sent_binary[0]
        self.assertEqual(op, VoiceOpCodesBinary.MLS_KEY_PACKAGE)

    def test_dave_passthrough_when_version_zero(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_select_protocol_ack({
            'secret_key': [0] * 32,
            'mode': 'aead_xchacha20_poly1305_rtpsize',
            'dave_protocol_version': 0,
        })
        self.assertFalse(conn.dave_ready)

    def test_dave_transition_flow(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._dave_protocol_version = 0
        conn._dave_pending_transitions[1] = 1

        sent = []
        conn._send_json = lambda op, data: sent.append((op, data))

        conn._execute_pending_transition(1)
        self.assertEqual(conn._dave_protocol_version, 1)
        self.assertNotIn(1, conn._dave_pending_transitions)

    def test_dave_downgrade_upgrade(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._dave_protocol_version = 1
        conn._dave_pending_transitions[1] = 0

        conn._execute_pending_transition(1)
        self.assertTrue(conn._dave_downgraded)
        self.assertEqual(conn._dave_protocol_version, 0)

        conn._dave_pending_transitions[2] = 1
        conn._execute_pending_transition(2)
        self.assertFalse(conn._dave_downgraded)
        self.assertEqual(conn._dave_protocol_version, 1)


class TestVoiceRecvCompat(unittest.TestCase):
    """Test voice-recv compatibility."""

    def test_has_voice_recv(self):
        from discord.ext.voice_recv import VoiceRecvClient
        self.assertTrue(issubclass(VoiceRecvClient, __import__('discord').VoiceClient))

    def test_voice_recv_client_attributes(self):
        from discord.ext.voice_recv import VoiceRecvClient
        self.assertTrue(hasattr(VoiceRecvClient, 'listen'))
        self.assertTrue(hasattr(VoiceRecvClient, 'stop_listening'))
        self.assertTrue(hasattr(VoiceRecvClient, 'is_listening'))

    def test_packet_decryptor_modes(self):
        from discord.ext.voice_recv.reader import PacketDecryptor
        self.assertIn('aead_xchacha20_poly1305_rtpsize', PacketDecryptor.supported_modes)

    def test_rtp_packet_parses_our_headers(self):
        from discord.ext.voice_recv.rtp import RTPPacket
        header = build_rtp_header(
            sequence=42, timestamp=1440, ssrc=99999,
            payload_type=101, marker=True,
        )
        payload = b'\x65\x88\x84'
        pkt = RTPPacket(bytes(header) + payload)
        self.assertEqual(pkt.sequence, 42)
        self.assertEqual(pkt.timestamp, 1440)
        self.assertEqual(pkt.ssrc, 99999)
        self.assertEqual(pkt.payload, 101)
        self.assertTrue(pkt.marker)

    def test_gateway_hook_importable(self):
        from discord.ext.voice_recv.gateway import hook
        self.assertTrue(callable(hook))

    def test_dm_voice_fixes_present(self):
        from discord.ext.voice_recv import voice_client
        import inspect
        src = inspect.getsource(voice_client)
        self.assertIn('guild', src.lower())

    def test_voice_recv_ssrc_tracking(self):
        """VoiceRecvClient has bidirectional SSRC mapping."""
        from discord.ext.voice_recv.voice_client import VoiceRecvClient
        self.assertTrue(hasattr(VoiceRecvClient, '_add_ssrc'))
        self.assertTrue(hasattr(VoiceRecvClient, '_remove_ssrc'))
        self.assertTrue(hasattr(VoiceRecvClient, '_get_ssrc_from_id'))
        self.assertTrue(hasattr(VoiceRecvClient, '_get_id_from_ssrc'))


class TestCompatModule(unittest.TestCase):
    """Test the compat/voice_recv module."""

    def test_has_voice_recv_constant(self):
        from discord_video_stream.compat.voice_recv import HAS_VOICE_RECV
        self.assertTrue(HAS_VOICE_RECV)

    def test_voice_send_recv_client_exists(self):
        from discord_video_stream.compat.voice_recv import VoiceSendRecvClient
        self.assertTrue(issubclass(VoiceSendRecvClient, __import__('discord').VoiceClient))

    def test_voice_send_recv_has_stream_attrs(self):
        from discord_video_stream.compat.voice_recv import VoiceSendRecvClient
        self.assertTrue(hasattr(VoiceSendRecvClient, 'stream_connection'))
        self.assertTrue(hasattr(VoiceSendRecvClient, 'video_sender'))
        self.assertTrue(hasattr(VoiceSendRecvClient, 'set_stream_components'))
        self.assertTrue(hasattr(VoiceSendRecvClient, 'clear_stream_components'))
        self.assertTrue(hasattr(VoiceSendRecvClient, 'send_video_packet'))

    def test_voice_send_recv_inherits_recv(self):
        from discord_video_stream.compat.voice_recv import VoiceSendRecvClient
        from discord.ext.voice_recv import VoiceRecvClient
        self.assertTrue(issubclass(VoiceSendRecvClient, VoiceRecvClient))


class TestFFmpegCommandIntegration(unittest.TestCase):
    """Test FFmpeg command construction."""

    def test_default_command(self):
        opts = StreamOptions(url='input.mp4')
        ffmpeg = FFmpegProcess(opts)
        cmd = ffmpeg.command
        self.assertIn('-bf', cmd)
        self.assertIn('0', cmd)
        self.assertIn('-forced-idr', cmd)
        self.assertIn('1', cmd)
        self.assertIn('-pix_fmt', cmd)
        self.assertIn('yuv420p', cmd)
        self.assertIn('-force_key_frames', cmd)
        self.assertIn('-preset', cmd)
        self.assertIn('superfast', cmd)
        self.assertIn('-f', cmd)
        self.assertIn('nut', cmd)

    def test_custom_bitrate(self):
        opts = StreamOptions(url='test.mp4', bitrate_video=8000, bitrate_video_max=10000)
        ffmpeg = FFmpegProcess(opts)
        cmd = ffmpeg.command
        bv_idx = cmd.index('-b:v')
        self.assertEqual(cmd[bv_idx + 1], '8000k')
        max_idx = cmd.index('-maxrate:v')
        self.assertEqual(cmd[max_idx + 1], '10000k')
        buf_idx = cmd.index('-bufsize:v')
        self.assertEqual(cmd[buf_idx + 1], '4000k')


class TestOpusDurationIntegration(unittest.TestCase):
    """Test Opus TOC byte parsing edge cases."""

    def test_all_silk_configs(self):
        """All SILK configs should produce valid durations."""
        for config in range(12):
            for code in range(3):
                toc = (config << 3) | code
                frame = bytes([toc]) + b'\x00' * 10
                duration = parse_opus_duration(frame)
                self.assertGreater(duration, 0, f'config={config} code={code}')

    def test_all_celt_configs(self):
        """All CELT configs should produce valid durations."""
        for config in range(16, 32):
            for code in range(3):
                toc = (config << 3) | code
                frame = bytes([toc])
                duration = parse_opus_duration(frame)
                self.assertGreater(duration, 0, f'config={config} code={code}')


if __name__ == '__main__':
    unittest.main()
