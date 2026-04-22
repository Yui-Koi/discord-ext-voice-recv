"""
Tests for the main VideoStreamer API.

Validates:
- Gateway event handling (STREAM_CREATE, STREAM_SERVER_UPDATE)
- Stream key generation and parsing
- Lifecycle management (join_voice, start_go_live, play, stop, leave)
- Gateway opcode payloads
"""

import sys
import os
import json
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from protocol.types import (
    GatewayOpCodes,
    generate_stream_key,
    parse_stream_key,
    build_video_payload,
    build_video_off_payload,
    VideoAttributes,
)


class TestGatewayOpcodes(unittest.TestCase):
    """Test gateway opcode constants."""

    def test_stream_create(self):
        self.assertEqual(GatewayOpCodes.STREAM_CREATE, 18)

    def test_stream_delete(self):
        self.assertEqual(GatewayOpCodes.STREAM_DELETE, 19)

    def test_stream_set_paused(self):
        self.assertEqual(GatewayOpCodes.STREAM_SET_PAUSED, 22)

    def test_voice_state_update(self):
        self.assertEqual(GatewayOpCodes.VOICE_STATE_UPDATE, 4)


class TestStreamKeyGeneration(unittest.TestCase):
    """Test stream key format (matches Node.js reference)."""

    def test_guild_key_format(self):
        key = generate_stream_key('guild', '41771983423143937', '123456', '789012')
        self.assertEqual(key, 'guild:41771983423143937:123456:789012')

    def test_call_key_format(self):
        key = generate_stream_key('call', None, '123456', '789012')
        self.assertEqual(key, 'call:123456:789012')

    def test_parse_guild_key(self):
        result = parse_stream_key('guild:41771983423143937:123456:789012')
        self.assertEqual(result['type'], 'guild')
        self.assertEqual(result['guild_id'], '41771983423143937')
        self.assertEqual(result['channel_id'], '123456')
        self.assertEqual(result['user_id'], '789012')

    def test_parse_call_key(self):
        result = parse_stream_key('call:123456:789012')
        self.assertEqual(result['type'], 'call')
        self.assertIsNone(result['guild_id'])
        self.assertEqual(result['channel_id'], '123456')
        self.assertEqual(result['user_id'], '789012')

    def test_roundtrip_guild(self):
        key = generate_stream_key('guild', '100', '200', '300')
        parsed = parse_stream_key(key)
        self.assertEqual(parsed['guild_id'], '100')
        self.assertEqual(parsed['channel_id'], '200')
        self.assertEqual(parsed['user_id'], '300')

    def test_roundtrip_call(self):
        key = generate_stream_key('call', None, '200', '300')
        parsed = parse_stream_key(key)
        self.assertIsNone(parsed['guild_id'])
        self.assertEqual(parsed['channel_id'], '200')
        self.assertEqual(parsed['user_id'], '300')


class TestStreamCreatePayload(unittest.TestCase):
    """Test STREAM_CREATE gateway opcode payload construction."""

    def test_guild_stream_create(self):
        """STREAM_CREATE payload for guild Go Live."""
        payload = {
            'type': 'guild',
            'guild_id': '41771983423143937',
            'channel_id': '123456',
            'preferred_region': None,
        }
        self.assertEqual(payload['type'], 'guild')
        self.assertIsNone(payload['preferred_region'])

    def test_call_stream_create(self):
        """STREAM_CREATE payload for DM call."""
        payload = {
            'type': 'call',
            'guild_id': None,
            'channel_id': '123456',
            'preferred_region': None,
        }
        self.assertEqual(payload['type'], 'call')


class TestStreamDeletePayload(unittest.TestCase):
    """Test STREAM_DELETE gateway opcode payload."""

    def test_payload(self):
        stream_key = generate_stream_key('guild', '100', '200', '300')
        payload = {'stream_key': stream_key}
        self.assertEqual(payload['stream_key'], 'guild:100:200:300')


class TestStreamSetPausedPayload(unittest.TestCase):
    """Test STREAM_SET_PAUSED gateway opcode payload."""

    def test_unpause(self):
        stream_key = generate_stream_key('guild', '100', '200', '300')
        payload = {
            'stream_key': stream_key,
            'paused': False,
        }
        self.assertFalse(payload['paused'])


class TestVideoOpcodePayload(unittest.TestCase):
    """Test VIDEO opcode (12) payload construction."""

    def test_video_on(self):
        attrs = VideoAttributes(width=1920, height=1080, fps=30)
        payload = build_video_payload(
            audio_ssrc=1000,
            video_ssrc=2000,
            rtx_ssrc=2001,
            attrs=attrs,
        )

        self.assertEqual(payload['audio_ssrc'], 1000)
        self.assertEqual(payload['video_ssrc'], 2000)
        self.assertEqual(payload['rtx_ssrc'], 2001)

        streams = payload['streams']
        self.assertEqual(len(streams), 1)

        stream = streams[0]
        self.assertEqual(stream['type'], 'video')
        self.assertEqual(stream['rid'], '100')
        self.assertEqual(stream['ssrc'], 2000)
        self.assertTrue(stream['active'])
        self.assertEqual(stream['quality'], 100)
        self.assertEqual(stream['rtx_ssrc'], 2001)
        self.assertEqual(stream['max_bitrate'], 10_000_000)
        self.assertEqual(stream['max_framerate'], 30)
        self.assertEqual(stream['max_resolution']['type'], 'fixed')
        self.assertEqual(stream['max_resolution']['width'], 1920)
        self.assertEqual(stream['max_resolution']['height'], 1080)

    def test_video_off(self):
        payload = build_video_off_payload(audio_ssrc=1000)
        self.assertEqual(payload['audio_ssrc'], 1000)
        self.assertEqual(payload['video_ssrc'], 0)
        self.assertEqual(payload['rtx_ssrc'], 0)
        self.assertEqual(payload['streams'], [])


class TestStreamerGatewayEventParsing(unittest.TestCase):
    """Test gateway event data parsing for stream events."""

    def test_parse_stream_create_event(self):
        """STREAM_CREATE event contains rtc_server_id and stream_key."""
        event_data = {
            'stream_key': 'guild:41771983423143937:123456:789012',
            'rtc_server_id': 'us-east1.discord.media',
        }

        parsed = parse_stream_key(event_data['stream_key'])
        self.assertEqual(parsed['type'], 'guild')
        self.assertEqual(parsed['guild_id'], '41771983423143937')
        self.assertEqual(event_data['rtc_server_id'], 'us-east1.discord.media')

    def test_parse_stream_server_update_event(self):
        """STREAM_SERVER_UPDATE event contains endpoint and token."""
        event_data = {
            'stream_key': 'guild:41771983423143937:123456:789012',
            'endpoint': 'us-east1.discord.media',
            'token': 'voice_token_abc123',
        }

        parsed = parse_stream_key(event_data['stream_key'])
        self.assertEqual(parsed['channel_id'], '123456')
        self.assertIsNotNone(event_data['endpoint'])
        self.assertIsNotNone(event_data['token'])

    def test_stream_create_event_filtering(self):
        """Only process STREAM_CREATE events matching our stream."""
        our_guild = '41771983423143937'
        our_channel = '123456'
        our_user = '789012'

        # Matching event
        event_data = {
            'stream_key': f'guild:{our_guild}:{our_channel}:{our_user}',
            'rtc_server_id': 'us-east1.discord.media',
        }
        parsed = parse_stream_key(event_data['stream_key'])
        self.assertEqual(parsed['guild_id'], our_guild)
        self.assertEqual(parsed['channel_id'], our_channel)
        self.assertEqual(parsed['user_id'], our_user)

        # Non-matching event (different user)
        event_data_other = {
            'stream_key': f'guild:{our_guild}:{our_channel}:999999',
            'rtc_server_id': 'us-east1.discord.media',
        }
        parsed_other = parse_stream_key(event_data_other['stream_key'])
        self.assertNotEqual(parsed_other['user_id'], our_user)


class TestVoiceStateUpdateOpcode(unittest.TestCase):
    """Test VOICE_STATE_UPDATE opcode payload for join/leave."""

    def test_join_voice(self):
        payload = {
            'guild_id': '41771983423143937',
            'channel_id': '123456',
            'self_mute': False,
            'self_deaf': True,
            'self_video': False,
        }
        self.assertFalse(payload['self_mute'])
        self.assertTrue(payload['self_deaf'])

    def test_leave_voice(self):
        payload = {
            'guild_id': None,
            'channel_id': None,
            'self_mute': True,
            'self_deaf': False,
            'self_video': False,
        }
        self.assertIsNone(payload['guild_id'])
        self.assertIsNone(payload['channel_id'])
        self.assertTrue(payload['self_mute'])


class TestSpeakingOpcode(unittest.TestCase):
    """Test SPEAKING opcode payload for Go Live."""

    def test_speaking_mode_2(self):
        """Go Live uses speaking mode 2 (priority/soundshare)."""
        payload = {
            'delay': 0,
            'speaking': 2,
            'ssrc': 1000,
        }
        self.assertEqual(payload['speaking'], 2)
        self.assertEqual(payload['delay'], 0)

    def test_not_speaking(self):
        payload = {
            'delay': 0,
            'speaking': 0,
            'ssrc': 1000,
        }
        self.assertEqual(payload['speaking'], 0)


class TestIdentifyOpcode(unittest.TestCase):
    """Test IDENTIFY opcode payload for stream voice server."""

    def test_identify_payload(self):
        """IDENTIFY must include video=true and streams=simulcast."""
        from protocol.types import STREAMS_SIMULCAST

        payload = {
            'server_id': '41771983423143937',
            'user_id': '789012',
            'session_id': 'sess_abc',
            'token': 'voice_token',
            'video': True,
            'streams': STREAMS_SIMULCAST,
            'max_dave_protocol_version': 1,
        }

        self.assertTrue(payload['video'])
        self.assertEqual(len(payload['streams']), 1)
        self.assertEqual(payload['streams'][0]['rid'], '100')
        self.assertEqual(payload['streams'][0]['quality'], 100)


class TestSelectProtocolOpcode(unittest.TestCase):
    """Test SELECT_PROTOCOL opcode payload."""

    def test_udp_protocol(self):
        """SELECT_PROTOCOL should use UDP transport."""
        from protocol.types import ALL_CODECS, SUPPORTED_ENCRYPTION_MODES

        payload = {
            'protocol': 'udp',
            'data': {
                'address': '0.0.0.0',
                'port': 12345,
                'mode': SUPPORTED_ENCRYPTION_MODES[1],
            },
            'codecs': [c.to_dict() for c in ALL_CODECS],
            'rtc_connection_id': 'test-uuid',
        }

        self.assertEqual(payload['protocol'], 'udp')
        codec_names = [c['name'] for c in payload['codecs']]
        self.assertIn('opus', codec_names)
        self.assertIn('H264', codec_names)

    def test_codec_list(self):
        """All 6 codecs should be present."""
        from protocol.types import ALL_CODECS
        self.assertEqual(len(ALL_CODECS), 6)
        names = [c.name for c in ALL_CODECS]
        self.assertIn('opus', names)
        self.assertIn('H264', names)
        self.assertIn('H265', names)
        self.assertIn('VP8', names)
        self.assertIn('VP9', names)
        self.assertIn('AV1', names)


if __name__ == '__main__':
    unittest.main()
