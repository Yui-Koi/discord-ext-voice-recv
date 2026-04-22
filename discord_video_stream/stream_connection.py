"""
Stream connection for Discord Go Live.

Manages a separate voice WebSocket connection to the stream server.
This is distinct from the main voice connection handled by discord.py's
VoiceClient. The stream connection has its own SSRCs, secret key, and
DAVE session.

Connection lifecycle:
1. Connect to wss://{endpoint}/?v=8
2. Server sends HELLO (opcode 8) with heartbeat_interval
3. Client sends IDENTIFY (opcode 0) with credentials
4. Server sends READY (opcode 2) with SSRCs and stream info
5. Client sends SELECT_PROTOCOL (opcode 1) with codec configs
6. Server sends SELECT_PROTOCOL_ACK (opcode 4) with secret_key and mode
7. DAVE key exchange if dave_protocol_version > 0
8. Client sends VIDEO (opcode 12) with stream SSRCs
9. Client sends SPEAKING (opcode 5) with mode=2 (priority/soundshare)

Reference: nodejs-reference/src/client/voice/BaseMediaConnection.ts
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import websockets

try:
    from .protocol.types import (
        ALL_CODECS,
        STREAMS_SIMULCAST,
        SUPPORTED_ENCRYPTION_MODES,
        CodecConfig,
        VideoAttributes,
        build_video_payload,
        build_video_off_payload,
    )
except ImportError:
    from protocol.types import (
        ALL_CODECS,
        STREAMS_SIMULCAST,
        SUPPORTED_ENCRYPTION_MODES,
        CodecConfig,
        VideoAttributes,
        build_video_payload,
        build_video_off_payload,
    )

if TYPE_CHECKING:
    from typing import Any, Callable, Coroutine, Dict, List, Optional

__all__ = [
    'StreamConnection',
    'StreamConnectionState',
    'StreamReadyParams',
    'StreamSessionDescription',
]

log = logging.getLogger(__name__)


# Voice WebSocket opcodes (from VoiceOpCodes.ts)
class VoiceOpCodes:
    IDENTIFY = 0
    SELECT_PROTOCOL = 1
    READY = 2
    HEARTBEAT = 3
    SELECT_PROTOCOL_ACK = 4
    SPEAKING = 5
    HEARTBEAT_ACK = 6
    RESUME = 7
    HELLO = 8
    RESUMED = 9
    CLIENTS_CONNECT = 11
    VIDEO = 12
    CLIENT_DISCONNECT = 13
    SESSION_UPDATE = 14
    DAVE_PREPARE_TRANSITION = 21
    DAVE_EXECUTE_TRANSITION = 22
    DAVE_TRANSITION_READY = 23
    DAVE_PREPARE_EPOCH = 24
    MLS_INVALID_COMMIT_WELCOME = 31


# Binary opcodes for DAVE/MLS (from VoiceOpCodesBinary.ts)
class VoiceOpCodesBinary:
    MLS_EXTERNAL_SENDER = 25
    MLS_KEY_PACKAGE = 26
    MLS_PROPOSALS = 27
    MLS_COMMIT_WELCOME = 28
    MLS_ANNOUNCE_COMMIT_TRANSITION = 29
    MLS_WELCOME = 30


@dataclass
class StreamReadyParams:
    """Parameters received from the READY opcode."""
    ssrc: int
    ip: str
    port: int
    modes: List[str]
    video_ssrc: int
    rtx_ssrc: int
    streams: List[Dict[str, Any]]


@dataclass
class StreamSessionDescription:
    """Parameters received from SELECT_PROTOCOL_ACK."""
    secret_key: bytes
    mode: str
    dave_protocol_version: int


class StreamConnectionState:
    """Tracks the lifecycle state of a stream connection."""

    def __init__(self) -> None:
        self.has_session: bool = False
        self.has_token: bool = False
        self.started: bool = False
        self.resuming: bool = False
        self.closed: bool = False

    def can_start(self) -> bool:
        return self.has_session and self.has_token and not self.started

    def reset(self) -> None:
        self.started = False
        self.closed = False


class StreamConnection:
    """Manages a Go Live voice WebSocket connection to the stream server.

    This is a separate connection from the main voice connection. It has
    its own SSRCs, secret key, DAVE session, and heartbeat.

    Usage:
        conn = StreamConnection(
            guild_id='123456',
            channel_id='789012',
            user_id='999',
            session_id='abc',
        )
        conn.set_tokens('endpoint.discord.gg', 'voice_token')
        await conn.connect()
        # ... send media ...
        conn.stop()
    """

    def __init__(
        self,
        guild_id: Optional[str],
        channel_id: str,
        user_id: str,
        session_id: str,
    ) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.session_id = session_id

        # Connection state
        self.state = StreamConnectionState()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._receive_task: Optional[asyncio.Task] = None

        # Tokens (set via set_tokens)
        self._endpoint: Optional[str] = None
        self._token: Optional[str] = None

        # Stream server ID (from STREAM_CREATE gateway event)
        self._server_id: Optional[str] = None
        self._stream_key: Optional[str] = None

        # Parameters from READY
        self._ready_params: Optional[StreamReadyParams] = None

        # Parameters from SELECT_PROTOCOL_ACK
        self._session_desc: Optional[StreamSessionDescription] = None

        # DAVE session (separate from main voice connection)
        self._dave_session = None
        self._dave_protocol_version: int = 0
        self._dave_pending_transitions: Dict[int, int] = {}
        self._dave_downgraded: bool = False
        # Initialize connected users with our own user_id — we are
        # always "connected" to the stream server. This ensures DAVE
        # proposals reference us correctly.
        self._connected_users: set = {user_id}

        # Sequence number for voice WS messages
        self._sequence: int = -1

        # Callbacks
        self._on_ready: Optional[Callable[[], Coroutine]] = None

    @property
    def type(self) -> str:
        """Connection type: 'guild' or 'call'."""
        return 'guild' if self.guild_id else 'call'

    @property
    def server_id(self) -> Optional[str]:
        """Server ID for the stream connection.

        For guild channels: guild_id.
        For DM/call: channel_id.
        """
        if self._server_id is not None:
            return self._server_id
        if self.guild_id:
            return self.guild_id
        return self.channel_id

    @server_id.setter
    def server_id(self, value: str) -> None:
        self._server_id = value

    @property
    def dave_channel_id(self) -> str:
        """DAVE channel ID for the stream connection.

        Computed as serverId - 1 (BigInt arithmetic from Node.js reference).
        This is a documented quirk of the Go Live protocol.
        """
        sid = self.server_id
        if sid is None:
            raise RuntimeError('server_id not set')
        return str(int(sid) - 1)

    @property
    def stream_key(self) -> Optional[str]:
        return self._stream_key

    @stream_key.setter
    def stream_key(self, value: str) -> None:
        self._stream_key = value

    @property
    def ready_params(self) -> Optional[StreamReadyParams]:
        return self._ready_params

    @property
    def session_description(self) -> Optional[StreamSessionDescription]:
        return self._session_desc

    @property
    def dave_session(self):
        return self._dave_session

    @property
    def dave_ready(self) -> bool:
        return (
            self._dave_protocol_version != 0
            and self._dave_session is not None
            and self._dave_session.ready
        )

    @property
    def audio_ssrc(self) -> int:
        if self._ready_params is None:
            return 0
        return self._ready_params.ssrc

    @property
    def video_ssrc(self) -> int:
        if self._ready_params is None:
            return 0
        return self._ready_params.video_ssrc

    @property
    def rtx_ssrc(self) -> int:
        if self._ready_params is None:
            return 0
        return self._ready_params.rtx_ssrc

    @property
    def secret_key(self) -> Optional[bytes]:
        if self._session_desc is None:
            return None
        return self._session_desc.secret_key

    @property
    def encryption_mode(self) -> Optional[str]:
        if self._session_desc is None:
            return None
        return self._session_desc.mode

    def set_tokens(self, endpoint: str, token: str) -> None:
        """Set the stream server endpoint and token.

        Called when STREAM_SERVER_UPDATE is received from the gateway.
        """
        self._endpoint = endpoint
        self._token = token
        self.state.has_token = True

    def set_session(self, session_id: str) -> None:
        """Set the session ID.

        Called when STREAM_CREATE is received from the gateway.
        """
        self.session_id = session_id
        self.state.has_session = True

    def set_on_ready(self, callback: Callable[[], Coroutine]) -> None:
        """Set callback invoked when the stream connection is ready."""
        self._on_ready = callback

    async def connect(self) -> None:
        """Connect to the stream voice server.

        Blocks until the connection handshake is complete (READY received
        and SELECT_PROTOCOL sent).
        """
        if not self.state.can_start():
            if self.state.closed:
                self.state.reset()
            else:
                raise RuntimeError(
                    'Cannot connect: missing session or token'
                )

        self.state.started = True
        endpoint = self._endpoint
        if endpoint is None:
            raise RuntimeError('endpoint not set')

        url = f'wss://{endpoint}/?v=9'
        log.info('Connecting to stream voice server: %s', url)

        try:
            self._ws = await websockets.connect(
                url,
                additional_headers={
                    'User-Agent': 'Discord-Video-Stream-Python/1.0',
                },
            )
        except Exception as e:
            log.error('Failed to connect to stream voice server: %s', e)
            self.state.started = False
            raise

        log.info('Connected to stream voice server')

        # Send IDENTIFY immediately after connecting (before any messages)
        self.identify()

        # Start receive loop
        self._receive_task = asyncio.create_task(
            self._receive_loop(),
            name='stream-ws-receive',
        )

        # Wait for READY by polling (the receive loop processes messages)
        # We use an event to signal when READY has been processed
        self._ready_event = asyncio.Event()
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            log.error('Timeout waiting for READY from stream server')
            await self._close_ws()
            raise RuntimeError('Stream voice server READY timeout')

    async def _close_ws(self) -> None:
        """Close the WebSocket connection."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _receive_loop(self) -> None:
        """Main receive loop for the stream voice WebSocket."""
        if self._ws is None:
            return

        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    self._handle_binary_message(message)
                elif isinstance(message, str):
                    import json
                    data = json.loads(message)
                    await self._handle_json_message(data)
        except websockets.ConnectionClosed as e:
            log.info('Stream WS closed: code=%s reason=%s', e.code, e.reason)
            can_resume = e.code == 4015 or e.code < 4000
            if can_resume and not self.state.closed:
                log.info('Attempting stream WS resume')
                self.state.resuming = True
                self.state.started = False
                # Reconnect will be handled by the caller
        except Exception as e:
            log.error('Stream WS receive error: %s', e)
        finally:
            self.state.started = False
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()

    async def _handle_json_message(self, msg: Dict[str, Any]) -> None:
        """Handle a JSON voice WebSocket message."""
        op = msg.get('op')
        data = msg.get('d', {})
        seq = msg.get('seq')

        if seq is not None:
            self._sequence = seq

        if op not in (VoiceOpCodes.HEARTBEAT, VoiceOpCodes.HEARTBEAT_ACK):
            log.debug('Stream WS recv op=%s data=%s', op, data)

        if op == VoiceOpCodes.HELLO:
            await self._handle_hello(data)

        elif op == VoiceOpCodes.READY:
            self._handle_ready(data)
            await self._send_select_protocol()
            self._ready_event.set()
            if self._on_ready is not None:
                await self._on_ready()

        elif op == VoiceOpCodes.SELECT_PROTOCOL_ACK:
            self._handle_select_protocol_ack(data)
            self._init_dave()

        elif op == VoiceOpCodes.SPEAKING:
            # Ignore speaking updates from other users on stream server
            pass

        elif op == VoiceOpCodes.HEARTBEAT_ACK:
            pass

        elif op == VoiceOpCodes.RESUMED:
            self.state.started = True
            log.info('Stream WS resumed')

        elif op == VoiceOpCodes.CLIENTS_CONNECT:
            uids = data.get('user_ids', [])
            for uid in uids:
                self._connected_users.add(str(uid))

        elif op == VoiceOpCodes.CLIENT_DISCONNECT:
            uid = str(data.get('user_id', ''))
            self._connected_users.discard(uid)

        elif op == VoiceOpCodes.DAVE_PREPARE_TRANSITION:
            await self._handle_dave_prepare_transition(data)

        elif op == VoiceOpCodes.DAVE_EXECUTE_TRANSITION:
            self._handle_dave_execute_transition(data)

        elif op == VoiceOpCodes.DAVE_PREPARE_EPOCH:
            await self._handle_dave_prepare_epoch(data)

        elif op is not None and op >= 4000:
            log.error('Stream WS error op=%s: %s', op, data)

        else:
            log.debug('Unhandled stream WS op=%s', op)

    def _handle_binary_message(self, msg: bytes) -> None:
        """Handle a binary voice WebSocket message (DAVE/MLS opcodes).

        Binary format (server-to-client):
            [2-byte sequence BE][1-byte opcode][variable payload]
        """
        if len(msg) < 3:
            return

        self._sequence = struct.unpack('>H', msg[0:2])[0]
        op = msg[2]

        log.debug('Stream WS binary op=%s', op)

        if op == VoiceOpCodesBinary.MLS_EXTERNAL_SENDER:
            if self._dave_session is not None:
                self._dave_session.set_external_sender(msg[3:])
                log.debug('Set MLS external sender')

        elif op == VoiceOpCodesBinary.MLS_PROPOSALS:
            self._handle_mls_proposals(msg)

        elif op == VoiceOpCodesBinary.MLS_ANNOUNCE_COMMIT_TRANSITION:
            self._handle_mls_announce_commit_transition(msg)

        elif op == VoiceOpCodesBinary.MLS_WELCOME:
            self._handle_mls_welcome(msg)

    def _handle_mls_proposals(self, msg: bytes) -> None:
        """Handle MLS_PROPOSALS binary message.

        Format: [2-byte seq][1-byte op=27][1-byte optype][proposals data]
        """
        if len(msg) < 5:
            return

        optype = msg[3]
        proposals_data = msg[4:]

        if self._dave_session is None:
            log.warning('Received MLS_PROPOSALS but no DAVE session')
            return

        try:
            result = self._dave_session.process_proposals(
                optype,
                proposals_data,
                list(self._connected_users),
            )
            if result is not None:
                commit = result.commit if hasattr(result, 'commit') else result[0]
                welcome = result.welcome if hasattr(result, 'welcome') else (result[1] if len(result) > 1 else None)

                if commit:
                    payload = commit + (welcome if welcome else b'')
                    self._send_binary(VoiceOpCodesBinary.MLS_COMMIT_WELCOME, payload)
                    log.debug('Sent MLS_COMMIT_WELCOME')
        except Exception as e:
            log.error('Error processing MLS proposals: %s', e)

    def _handle_mls_announce_commit_transition(self, msg: bytes) -> None:
        """Handle MLS_ANNOUNCE_COMMIT_TRANSITION binary message.

        Format: [2-byte seq][1-byte op=29][2-byte transition_id BE][commit data]
        """
        if len(msg) < 6:
            return

        transition_id = struct.unpack('>H', msg[3:5])[0]
        commit_data = msg[5:]

        try:
            if self._dave_session is not None:
                self._dave_session.process_commit(commit_data)

            if transition_id:
                self._dave_pending_transitions[transition_id] = self._dave_protocol_version
                self._send_json(VoiceOpCodes.DAVE_TRANSITION_READY, {
                    'transition_id': transition_id,
                })
                log.debug('MLS commit processed, transition_id=%s', transition_id)
        except Exception as e:
            log.error('MLS commit error: %s', e)
            self._process_invalid_commit(transition_id)

    def _handle_mls_welcome(self, msg: bytes) -> None:
        """Handle MLS_WELCOME binary message.

        Format: [2-byte seq][1-byte op=30][2-byte transition_id BE][welcome data]
        """
        if len(msg) < 6:
            return

        transition_id = struct.unpack('>H', msg[3:5])[0]
        welcome_data = msg[5:]

        try:
            if self._dave_session is not None:
                self._dave_session.process_welcome(welcome_data)

            if transition_id:
                self._dave_pending_transitions[transition_id] = self._dave_protocol_version
                self._send_json(VoiceOpCodes.DAVE_TRANSITION_READY, {
                    'transition_id': transition_id,
                })
                log.debug('MLS welcome processed, transition_id=%s', transition_id)
        except Exception as e:
            log.error('MLS welcome error: %s', e)
            self._process_invalid_commit(transition_id)

    def _process_invalid_commit(self, transition_id: int) -> None:
        """Handle an unprocessable commit by requesting re-initialization."""
        log.debug('Invalid commit, reinitializing DAVE, transition_id=%s', transition_id)
        self._send_json(VoiceOpCodes.MLS_INVALID_COMMIT_WELCOME, {
            'transition_id': transition_id,
        })
        self._init_dave()

    async def _handle_hello(self, data: Dict[str, Any]) -> None:
        """Handle HELLO opcode: start heartbeat."""
        interval_ms = data.get('heartbeat_interval', 41250)
        log.debug('Stream WS HELLO, heartbeat_interval=%s', interval_ms)

        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()

        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(interval_ms / 1000.0),
            name='stream-ws-heartbeat',
        )

    def _handle_ready(self, data: Dict[str, Any]) -> None:
        """Handle READY opcode: extract SSRCs and connection params."""
        ssrc = data.get('ssrc', 0)
        ip = data.get('ip', '')
        port = data.get('port', 0)
        modes = data.get('modes', [])
        streams = data.get('streams', [])

        # Extract video SSRCs from the first simulcast stream
        video_ssrc = 0
        rtx_ssrc = 0
        if streams:
            stream = streams[0]
            video_ssrc = stream.get('ssrc', 0)
            rtx_ssrc = stream.get('rtx_ssrc', 0)

        self._ready_params = StreamReadyParams(
            ssrc=ssrc,
            ip=ip,
            port=port,
            modes=modes,
            video_ssrc=video_ssrc,
            rtx_ssrc=rtx_ssrc,
            streams=streams,
        )

        log.info(
            'Stream READY: audio_ssrc=%s video_ssrc=%s rtx_ssrc=%s',
            ssrc, video_ssrc, rtx_ssrc,
        )

    async def _send_select_protocol(self) -> None:
        """Send SELECT_PROTOCOL with UDP transport and codec configs."""
        # Build codec list matching the Node.js reference
        codecs = []
        for codec in ALL_CODECS:
            codec_dict = codec.to_dict()
            codecs.append(codec_dict)

        # For send-only connections, address and port can be randomized
        # (per Discord documentation)
        import random
        address = '0.0.0.0'
        port = random.randint(1024, 65535)

        self._send_json(VoiceOpCodes.SELECT_PROTOCOL, {
            'protocol': 'udp',
            'data': {
                'address': address,
                'port': port,
                'mode': SUPPORTED_ENCRYPTION_MODES[1],  # xchacha20 preferred
            },
            'codecs': codecs,
            'rtc_connection_id': str(random.randint(0, 2**128)),
        })

        log.debug('Sent SELECT_PROTOCOL')

    def _handle_select_protocol_ack(self, data: Dict[str, Any]) -> None:
        """Handle SELECT_PROTOCOL_ACK: extract secret key and mode."""
        # For UDP protocol, the response contains secret_key and mode
        secret_key_list = data.get('secret_key', [])
        secret_key = bytes(secret_key_list) if secret_key_list else b''
        mode = data.get('mode', '')
        dave_version = data.get('dave_protocol_version', 0)

        self._session_desc = StreamSessionDescription(
            secret_key=secret_key,
            mode=mode,
            dave_protocol_version=dave_version,
        )
        self._dave_protocol_version = dave_version

        log.info(
            'Stream SESSION_DESCRIPTION: mode=%s dave_version=%s key_len=%s',
            mode, dave_version, len(secret_key),
        )

    def _init_dave(self) -> None:
        """Initialize or reinitialize the DAVE session."""
        if self._dave_protocol_version == 0:
            if self._dave_session is not None:
                self._dave_session.reset()
                self._dave_session.set_passthrough_mode(True, 10)
            return

        try:
            import davey
        except ImportError:
            log.error('davey library required for DAVE')
            return

        if self._dave_session is not None:
            self._dave_session.reinit(
                self._dave_protocol_version,
                int(self.user_id),
                int(self.dave_channel_id),
            )
            log.debug('Reinitialized DAVE session')
        else:
            self._dave_session = davey.DaveSession(
                self._dave_protocol_version,
                int(self.user_id),
                int(self.dave_channel_id),
            )
            log.debug(
                'Initialized DAVE session: version=%s user=%s channel=%s',
                self._dave_protocol_version, self.user_id, self.dave_channel_id,
            )

        # Send key package
        key_package = self._dave_session.get_serialized_key_package()
        self._send_binary(VoiceOpCodesBinary.MLS_KEY_PACKAGE, key_package)
        log.debug('Sent MLS_KEY_PACKAGE')

    async def _handle_dave_prepare_transition(self, data: Dict[str, Any]) -> None:
        """Handle DAVE_PREPARE_TRANSITION opcode."""
        transition_id = data.get('transition_id', 0)
        protocol_version = data.get('protocol_version', 0)
        log.debug(
            'DAVE_PREPARE_TRANSITION: id=%s version=%s',
            transition_id, protocol_version,
        )

        self._dave_pending_transitions[transition_id] = protocol_version

        if transition_id == 0:
            self._execute_pending_transition(transition_id)
        else:
            if protocol_version == 0 and self._dave_session is not None:
                self._dave_session.set_passthrough_mode(True, 120)
            self._send_json(VoiceOpCodes.DAVE_TRANSITION_READY, {
                'transition_id': transition_id,
            })

    def _handle_dave_execute_transition(self, data: Dict[str, Any]) -> None:
        """Handle DAVE_EXECUTE_TRANSITION opcode."""
        transition_id = data.get('transition_id', 0)
        self._execute_pending_transition(transition_id)

    def _execute_pending_transition(self, transition_id: int) -> None:
        """Execute a pending DAVE transition."""
        new_version = self._dave_pending_transitions.get(transition_id)
        if new_version is None:
            log.error('Unknown transition_id=%s', transition_id)
            return

        old_version = self._dave_protocol_version
        self._dave_protocol_version = new_version

        if old_version != new_version and new_version == 0:
            self._dave_downgraded = True
            log.debug('Downgraded to non-E2E stream')
        elif transition_id > 0 and self._dave_downgraded:
            self._dave_downgraded = False
            if self._dave_session is not None:
                self._dave_session.set_passthrough_mode(True, 10)
            log.debug('Upgraded to E2E stream')

        del self._dave_pending_transitions[transition_id]
        log.debug('Executed transition_id=%s, new_version=%s', transition_id, new_version)

    async def _handle_dave_prepare_epoch(self, data: Dict[str, Any]) -> None:
        """Handle DAVE_PREPARE_EPOCH opcode."""
        epoch = data.get('epoch', 0)
        protocol_version = data.get('protocol_version', 0)
        log.debug('DAVE_PREPARE_EPOCH: epoch=%s version=%s', epoch, protocol_version)

        if epoch == 1:
            self._dave_protocol_version = protocol_version
            self._init_dave()

    async def _heartbeat_loop(self, interval: float) -> None:
        """Send periodic heartbeats to the stream voice server."""
        try:
            while True:
                await asyncio.sleep(interval)
                if self._ws is not None and self._ws.state == websockets.State.OPEN:
                    self._send_json(VoiceOpCodes.HEARTBEAT, {
                        't': int(time.time() * 1000),
                        'seq_ack': self._sequence,
                    })
        except asyncio.CancelledError:
            pass

    def _send_json(self, op: int, data: Dict[str, Any]) -> None:
        """Send a JSON message over the stream voice WebSocket."""
        import json
        if self._ws is None or not self._ws.state == websockets.State.OPEN:
            log.warning('Cannot send: stream WS not connected')
            return
        payload = json.dumps({'op': op, 'd': data})
        task = asyncio.ensure_future(self._ws.send(payload))
        task.add_done_callback(self._handle_send_error)

    def _send_binary(self, op: int, data: bytes) -> None:
        """Send a binary message over the stream voice WebSocket.

        Format (client-to-server): [1-byte opcode][payload]
        No sequence number prefix (unlike server-to-client).
        """
        if self._ws is None or not self._ws.state == websockets.State.OPEN:
            log.warning('Cannot send binary: stream WS not connected')
            return
        buf = bytes([op]) + data
        task = asyncio.ensure_future(self._ws.send(buf))
        task.add_done_callback(self._handle_send_error)

    @staticmethod
    def _handle_send_error(task: asyncio.Task) -> None:
        """Callback to log errors from fire-and-forget send tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning('Stream WS send error: %s', exc)

    def identify(self) -> None:
        """Send IDENTIFY opcode to the stream voice server."""
        sid = self.server_id
        if sid is None:
            raise RuntimeError('server_id not set')

        try:
            import davey
            max_dave = davey.DAVE_PROTOCOL_VERSION
        except ImportError:
            max_dave = 0

        self._send_json(VoiceOpCodes.IDENTIFY, {
            'server_id': sid,
            'user_id': self.user_id,
            'session_id': self.session_id,
            'token': self._token,
            'video': True,
            'streams': STREAMS_SIMULCAST,
            'max_dave_protocol_version': max_dave,
            'channel_id': self.channel_id,  # v9 addition
        })
        log.debug('Sent IDENTIFY: server_id=%s user_id=%s channel_id=%s', sid, self.user_id, self.channel_id)

    def set_speaking(self, speaking: bool) -> None:
        """Send SPEAKING opcode with mode=2 (priority/soundshare).

        Go Live uses speaking mode 2, not mode 1 (normal speaking)
        which is used for camera streams.
        """
        if self._ready_params is None:
            raise RuntimeError('Not ready: no SSRCs')

        self._send_json(VoiceOpCodes.SPEAKING, {
            'delay': 0,
            'speaking': 2 if speaking else 0,
            'ssrc': self._ready_params.ssrc,
        })

    def set_video_attributes(
        self,
        enabled: bool,
        attrs: Optional[VideoAttributes] = None,
    ) -> None:
        """Send VIDEO opcode to enable/disable video.

        When enabled=True, attrs must be provided with width, height, fps.
        When enabled=False, sends an empty video payload.
        """
        if self._ready_params is None:
            raise RuntimeError('Not ready: no SSRCs')

        if not enabled:
            payload = build_video_off_payload(
                audio_ssrc=self._ready_params.ssrc,
            )
        else:
            if attrs is None:
                raise ValueError('attrs required when enabled=True')
            payload = build_video_payload(
                audio_ssrc=self._ready_params.ssrc,
                video_ssrc=self._ready_params.video_ssrc,
                rtx_ssrc=self._ready_params.rtx_ssrc,
                attrs=attrs,
            )

        self._send_json(VoiceOpCodes.VIDEO, payload)

    def send_dave_key_package(self) -> None:
        """Manually send MLS_KEY_PACKAGE (for re-initialization)."""
        if self._dave_session is None:
            raise RuntimeError('No DAVE session')
        key_package = self._dave_session.get_serialized_key_package()
        self._send_binary(VoiceOpCodesBinary.MLS_KEY_PACKAGE, key_package)

    async def resume(self) -> None:
        """Resume a disconnected stream voice WebSocket."""
        if self._endpoint is None or self._token is None:
            raise RuntimeError('Cannot resume: no endpoint/token')

        self.state.started = True
        url = f'wss://{self._endpoint}/?v=9'

        try:
            self._ws = await websockets.connect(url)
        except Exception as e:
            log.error('Failed to resume stream WS: %s', e)
            self.state.started = False
            raise

        self._send_json(VoiceOpCodes.RESUME, {
            'server_id': self.server_id,
            'session_id': self.session_id,
            'token': self._token,
            'seq_ack': self._sequence,
        })

        self._receive_task = asyncio.create_task(
            self._receive_loop(),
            name='stream-ws-receive',
        )

    def stop(self) -> None:
        """Stop the stream connection."""
        self.state.closed = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._receive_task is not None:
            self._receive_task.cancel()
            self._receive_task = None
        if self._ws is not None:
            asyncio.ensure_future(self._close_ws())
