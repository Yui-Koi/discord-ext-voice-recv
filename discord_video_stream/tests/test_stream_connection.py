"""
Tests for stream connection.

Validates:
- Voice WebSocket opcode handling (IDENTIFY, READY, SELECT_PROTOCOL, etc.)
- DAVE session initialization and MLS opcode handling
- Binary message parsing (MLS_EXTERNAL_SENDER, MLS_PROPOSALS, etc.)
- Stream connection state management
- daveChannelId computation (serverId - 1)
- Speaking mode = 2 for Go Live
- Heartbeat loop lifecycle
"""

import sys
import os
import struct
import json
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


async def _mock_send_json_await(sent_list, op, data):
    """Mock for _send_json_await that appends to a list."""
    sent_list.append((op, data))


async def _mock_send_binary_await(sent_list, op, data):
    """Mock for _send_binary_await that appends to a list."""
    sent_list.append((op, data))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from stream_connection import (
    StreamConnection,
    StreamConnectionState,
    StreamReadyParams,
    StreamSessionDescription,
    VoiceOpCodes,
    VoiceOpCodesBinary,
)


class TestStreamConnectionState(unittest.TestCase):
    """Test StreamConnectionState lifecycle."""

    def test_initial_state(self):
        state = StreamConnectionState()
        self.assertFalse(state.has_session)
        self.assertFalse(state.has_token)
        self.assertFalse(state.started)
        self.assertFalse(state.resuming)
        self.assertFalse(state.closed)

    def test_can_start_requires_session_and_token(self):
        state = StreamConnectionState()
        self.assertFalse(state.can_start())

        state.has_session = True
        self.assertFalse(state.can_start())

        state.has_token = True
        self.assertTrue(state.can_start())

    def test_cannot_start_when_started(self):
        state = StreamConnectionState()
        state.has_session = True
        state.has_token = True
        state.started = True
        self.assertFalse(state.can_start())

    def test_reset(self):
        state = StreamConnectionState()
        state.started = True
        state.closed = True
        state.reset()
        self.assertFalse(state.started)
        self.assertFalse(state.closed)


class TestStreamConnectionInit(unittest.TestCase):
    """Test StreamConnection initialization."""

    def test_guild_type(self):
        conn = StreamConnection(
            guild_id='123456',
            channel_id='789012',
            user_id='999',
            session_id='abc',
        )
        self.assertEqual(conn.type, 'guild')

    def test_call_type(self):
        conn = StreamConnection(
            guild_id=None,
            channel_id='789012',
            user_id='999',
            session_id='abc',
        )
        self.assertEqual(conn.type, 'call')

    def test_server_id_guild(self):
        conn = StreamConnection(
            guild_id='123456',
            channel_id='789012',
            user_id='999',
            session_id='abc',
        )
        self.assertEqual(conn.server_id, '123456')

    def test_server_id_call(self):
        conn = StreamConnection(
            guild_id=None,
            channel_id='789012',
            user_id='999',
            session_id='abc',
        )
        self.assertEqual(conn.server_id, '789012')

    def test_server_id_override(self):
        conn = StreamConnection(
            guild_id='123456',
            channel_id='789012',
            user_id='999',
            session_id='abc',
        )
        conn.server_id = '999888'
        self.assertEqual(conn.server_id, '999888')

    def test_dave_channel_id_guild(self):
        """daveChannelId = serverId - 1 (confirmed from Node.js)."""
        conn = StreamConnection(
            guild_id='41771983423143937',
            channel_id='123456',
            user_id='789012',
            session_id='abc',
        )
        # BigInt("41771983423143937") - 1n = "41771983423143936"
        self.assertEqual(conn.dave_channel_id, '41771983423143936')

    def test_dave_channel_id_call(self):
        conn = StreamConnection(
            guild_id=None,
            channel_id='123456',
            user_id='789012',
            session_id='abc',
        )
        self.assertEqual(conn.dave_channel_id, '123455')

    def test_dave_channel_id_after_override(self):
        conn = StreamConnection(
            guild_id='123456',
            channel_id='789012',
            user_id='999',
            session_id='abc',
        )
        conn.server_id = '1000'
        self.assertEqual(conn.dave_channel_id, '999')

    def test_initial_ready_params_none(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        self.assertIsNone(conn.ready_params)
        self.assertEqual(conn.audio_ssrc, 0)
        self.assertEqual(conn.video_ssrc, 0)
        self.assertEqual(conn.rtx_ssrc, 0)

    def test_initial_dave_state(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        self.assertIsNone(conn.dave_session)
        self.assertFalse(conn.dave_ready)

    def test_set_tokens(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn.set_tokens('endpoint.discord.gg', 'voice_token')
        self.assertTrue(conn.state.has_token)
        self.assertEqual(conn._endpoint, 'endpoint.discord.gg')
        self.assertEqual(conn._token, 'voice_token')

    def test_set_session(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn.set_session('new_session')
        self.assertTrue(conn.state.has_session)
        self.assertEqual(conn.session_id, 'new_session')


class TestReadyHandling(unittest.TestCase):
    """Test READY opcode handling."""

    def test_handle_ready(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_ready({
            'ssrc': 1000,
            'ip': '1.2.3.4',
            'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [
                {
                    'type': 'video',
                    'ssrc': 2000,
                    'rtx_ssrc': 2001,
                    'rid': '100',
                    'quality': 100,
                    'active': True,
                },
            ],
        })

        self.assertIsNotNone(conn.ready_params)
        self.assertEqual(conn.audio_ssrc, 1000)
        self.assertEqual(conn.video_ssrc, 2000)
        self.assertEqual(conn.rtx_ssrc, 2001)
        self.assertEqual(conn.ready_params.ip, '1.2.3.4')
        self.assertEqual(conn.ready_params.port, 5000)

    def test_handle_ready_no_streams(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_ready({
            'ssrc': 1000,
            'ip': '1.2.3.4',
            'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [],
        })

        self.assertEqual(conn.audio_ssrc, 1000)
        self.assertEqual(conn.video_ssrc, 0)
        self.assertEqual(conn.rtx_ssrc, 0)


class TestSelectProtocolAckHandling(unittest.TestCase):
    """Test SELECT_PROTOCOL_ACK handling."""

    def test_handle_select_protocol_ack(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        secret_key = list(range(32))
        conn._handle_select_protocol_ack({
            'secret_key': secret_key,
            'mode': 'aead_xchacha20_poly1305_rtpsize',
            'dave_protocol_version': 1,
        })

        self.assertIsNotNone(conn.session_description)
        self.assertEqual(conn.secret_key, bytes(range(32)))
        self.assertEqual(conn.encryption_mode, 'aead_xchacha20_poly1305_rtpsize')

    def test_handle_select_protocol_ack_no_dave(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_select_protocol_ack({
            'secret_key': [0] * 32,
            'mode': 'aead_xchacha20_poly1305_rtpsize',
            'dave_protocol_version': 0,
        })

        self.assertIsNotNone(conn.session_description)


class TestSpeakingMode(unittest.TestCase):
    """Test that Go Live uses speaking mode 2."""

    def test_speaking_on(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_ready({
            'ssrc': 1000, 'ip': '1.2.3.4', 'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [{'type': 'video', 'ssrc': 2000, 'rtx_ssrc': 2001, 'rid': '100', 'quality': 100, 'active': True}],
        })

        # Mock the send_json method
        sent = []
        conn._send_json = lambda op, data: sent.append((op, data))

        conn.set_speaking(True)
        self.assertEqual(len(sent), 1)
        op, data = sent[0]
        self.assertEqual(op, VoiceOpCodes.SPEAKING)
        self.assertEqual(data['speaking'], 2)  # mode=2 for Go Live

    def test_speaking_off(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_ready({
            'ssrc': 1000, 'ip': '1.2.3.4', 'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [{'type': 'video', 'ssrc': 2000, 'rtx_ssrc': 2001, 'rid': '100', 'quality': 100, 'active': True}],
        })

        sent = []
        conn._send_json = lambda op, data: sent.append((op, data))

        conn.set_speaking(False)
        op, data = sent[0]
        self.assertEqual(data['speaking'], 0)


class TestVideoAttributes(unittest.TestCase):
    """Test VIDEO opcode construction."""

    def test_video_on(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_ready({
            'ssrc': 1000, 'ip': '1.2.3.4', 'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [{'type': 'video', 'ssrc': 2000, 'rtx_ssrc': 2001, 'rid': '100', 'quality': 100, 'active': True}],
        })

        sent = []
        conn._send_json = lambda op, data: sent.append((op, data))

        from protocol.types import VideoAttributes
        conn.set_video_attributes(True, VideoAttributes(width=1920, height=1080, fps=30))

        op, data = sent[0]
        self.assertEqual(op, VoiceOpCodes.VIDEO)
        self.assertEqual(data['audio_ssrc'], 1000)
        self.assertEqual(data['video_ssrc'], 2000)
        self.assertEqual(data['rtx_ssrc'], 2001)
        self.assertEqual(len(data['streams']), 1)
        self.assertEqual(data['streams'][0]['max_resolution']['width'], 1920)

    def test_video_off(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._handle_ready({
            'ssrc': 1000, 'ip': '1.2.3.4', 'port': 5000,
            'modes': ['aead_xchacha20_poly1305_rtpsize'],
            'streams': [{'type': 'video', 'ssrc': 2000, 'rtx_ssrc': 2001, 'rid': '100', 'quality': 100, 'active': True}],
        })

        sent = []
        conn._send_json = lambda op, data: sent.append((op, data))

        conn.set_video_attributes(False)

        op, data = sent[0]
        self.assertEqual(data['video_ssrc'], 0)
        self.assertEqual(data['streams'], [])


class TestBinaryMessageHandling(unittest.TestCase):
    """Test binary WS message parsing."""

    def test_mls_external_sender(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        # Mock dave session
        mock_session = MagicMock()
        conn._dave_session = mock_session

        # Build binary message: [2-byte seq][1-byte op=25][sender data]
        sender_data = b'\x01\x02\x03\x04'
        msg = struct.pack('>H', 100) + bytes([VoiceOpCodesBinary.MLS_EXTERNAL_SENDER]) + sender_data

        asyncio.get_event_loop().run_until_complete(conn._handle_binary_message(msg))
        mock_session.set_external_sender.assert_called_once_with(sender_data)

    def test_mls_proposals(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.commit = b'commit_data'
        mock_result.welcome = None
        mock_session.process_proposals.return_value = mock_result
        conn._dave_session = mock_session
        conn._connected_users = {'111', '222'}

        sent_binary = []
        conn._send_binary_await = lambda op, data: _mock_send_binary_await(sent_binary, op, data)

        # Build: [2-byte seq][1-byte op=27][1-byte optype=0][proposals]
        proposals = b'\x00\x01\x02'
        msg = struct.pack('>H', 100) + bytes([VoiceOpCodesBinary.MLS_PROPOSALS, 0]) + proposals

        asyncio.get_event_loop().run_until_complete(conn._handle_binary_message(msg))
        mock_session.process_proposals.assert_called_once()

    def test_mls_announce_commit_transition(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        mock_session = MagicMock()
        conn._dave_session = mock_session
        conn._dave_protocol_version = 1

        sent_json = []
        conn._send_json_await = lambda op, data: _mock_send_json_await(sent_json, op, data)

        # Build: [2-byte seq][1-byte op=29][2-byte transition_id][commit data]
        transition_id = 42
        commit_data = b'\x01\x02\x03'
        msg = struct.pack('>H', 100) + bytes([VoiceOpCodesBinary.MLS_ANNOUNCE_COMMIT_TRANSITION]) + struct.pack('>H', transition_id) + commit_data

        asyncio.get_event_loop().run_until_complete(conn._handle_binary_message(msg))
        mock_session.process_commit.assert_called_once_with(commit_data)

        # Should send DAVE_TRANSITION_READY
        self.assertEqual(len(sent_json), 1)
        op, data = sent_json[0]
        self.assertEqual(op, VoiceOpCodes.DAVE_TRANSITION_READY)
        self.assertEqual(data['transition_id'], 42)

    def test_mls_welcome(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        mock_session = MagicMock()
        conn._dave_session = mock_session
        conn._dave_protocol_version = 1

        sent_json = []
        conn._send_json_await = lambda op, data: _mock_send_json_await(sent_json, op, data)

        # Build: [2-byte seq][1-byte op=30][2-byte transition_id][welcome data]
        transition_id = 5
        welcome_data = b'\x04\x05\x06'
        msg = struct.pack('>H', 100) + bytes([VoiceOpCodesBinary.MLS_WELCOME]) + struct.pack('>H', transition_id) + welcome_data

        asyncio.get_event_loop().run_until_complete(conn._handle_binary_message(msg))
        mock_session.process_welcome.assert_called_once_with(welcome_data)

    def test_binary_message_too_short(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        # Should not raise
        asyncio.get_event_loop().run_until_complete(conn._handle_binary_message(b'\x00'))
        asyncio.get_event_loop().run_until_complete(conn._handle_binary_message(b''))


class TestIdentifyOpcode(unittest.TestCase):
    """Test IDENTIFY opcode construction."""

    def test_identify_guild(self):
        conn = StreamConnection(
            guild_id='41771983423143937',
            channel_id='123456',
            user_id='789012',
            session_id='sess_abc',
        )
        conn.set_tokens('endpoint.discord.gg', 'voice_token')

        sent = []
        conn._send_json_await = lambda op, data: _mock_send_json_await(sent, op, data)

        asyncio.get_event_loop().run_until_complete(conn.identify())

        op, data = sent[0]
        self.assertEqual(op, VoiceOpCodes.IDENTIFY)
        self.assertEqual(data['server_id'], '41771983423143937')
        self.assertEqual(data['user_id'], '789012')
        self.assertEqual(data['session_id'], 'sess_abc')
        self.assertEqual(data['token'], 'voice_token')
        self.assertTrue(data['video'])
        self.assertIsInstance(data['streams'], list)
        self.assertGreater(len(data['streams']), 0)

    def test_identify_call(self):
        conn = StreamConnection(
            guild_id=None,
            channel_id='123456',
            user_id='789012',
            session_id='sess_abc',
        )
        conn.set_tokens('endpoint.discord.gg', 'voice_token')

        sent = []
        conn._send_json_await = lambda op, data: _mock_send_json_await(sent, op, data)

        asyncio.get_event_loop().run_until_complete(conn.identify())

        op, data = sent[0]
        self.assertEqual(data['server_id'], '123456')


class TestDAVETransitions(unittest.TestCase):
    """Test DAVE transition handling."""

    def test_prepare_transition_zero(self):
        """Transition ID 0 should execute immediately."""
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._dave_protocol_version = 0

        conn._execute_pending_transition = MagicMock()
        conn._dave_pending_transitions[0] = 1

        # Simulate handle with transition_id=0
        loop = asyncio.new_event_loop()

        async def run():
            await conn._handle_dave_prepare_transition({
                'transition_id': 0,
                'protocol_version': 1,
            })

        loop.run_until_complete(run())
        loop.close()

        conn._execute_pending_transition.assert_called_once_with(0)

    def test_prepare_transition_nonzero(self):
        """Non-zero transition ID should send DAVE_TRANSITION_READY."""
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._dave_protocol_version = 1

        sent = []
        conn._send_json_await = lambda op, data: _mock_send_json_await(sent, op, data)

        loop = asyncio.new_event_loop()

        async def run():
            await conn._handle_dave_prepare_transition({
                'transition_id': 5,
                'protocol_version': 1,
            })

        loop.run_until_complete(run())
        loop.close()

        self.assertEqual(len(sent), 1)
        op, data = sent[0]
        self.assertEqual(op, VoiceOpCodes.DAVE_TRANSITION_READY)
        self.assertEqual(data['transition_id'], 5)

    def test_execute_transition_version_change(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn._dave_protocol_version = 0
        conn._dave_pending_transitions[1] = 1

        conn._execute_pending_transition(1)
        self.assertEqual(conn._dave_protocol_version, 1)
        self.assertNotIn(1, conn._dave_pending_transitions)


class TestStop(unittest.TestCase):
    """Test connection stop/cleanup."""

    def test_stop_sets_closed(self):
        conn = StreamConnection(
            guild_id='123', channel_id='456',
            user_id='789', session_id='abc',
        )
        conn.stop()
        self.assertTrue(conn.state.closed)


if __name__ == '__main__':
    unittest.main()
