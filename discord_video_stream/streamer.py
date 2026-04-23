"""
Main API for Discord video streaming.

Orchestrates the complete Go Live pipeline:
1. Join voice channel via discord.py
2. Create stream connection (STREAM_CREATE gateway event)
3. Connect to stream voice server
4. Start FFmpeg transcoding pipeline
5. Demux frames, packetize, encrypt, send via UDP
6. Handle stream lifecycle (start, stop, leave)

Usage:
    import discord
    from discord_video_stream import VideoStreamer

    client = discord.Client(...)
    streamer = VideoStreamer(client)

    @client.event
    async def on_ready():
        guild = client.get_guild(GUILD_ID)
        channel = guild.get_channel(CHANNEL_ID)
        await streamer.join_voice(guild.id, channel.id)
        await streamer.start_go_live()
        await streamer.play('https://example.com/video.mp4')

Reference: nodejs-reference/src/client/Streamer.ts
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

try:
    from .stream_connection import StreamConnection
    from .voice_send import VideoSender, AudioSender
    from .media.ffmpeg import StreamOptions, FFmpegProcess
    from .media.demux import Demuxer, FrameType
    from .media.pacer import FramePacer
    from .protocol.types import (
        GatewayOpCodes,
        VideoAttributes,
        generate_stream_key,
        parse_stream_key,
    )
except ImportError:
    from stream_connection import StreamConnection
    from voice_send import VideoSender, AudioSender
    from media.ffmpeg import StreamOptions, FFmpegProcess
    from media.demux import Demuxer, FrameType
    from media.pacer import FramePacer
    from protocol.types import (
        GatewayOpCodes,
        VideoAttributes,
        generate_stream_key,
        parse_stream_key,
    )

if TYPE_CHECKING:
    from typing import Any, Callable, Dict, Optional
    import discord  # type: ignore

__all__ = [
    'VideoStreamer',
]

log = logging.getLogger(__name__)


class VideoStreamer:
    """Main entry point for Discord video streaming via Go Live.

    This class manages:
    - Voice channel connection (via discord.py's VoiceClient)
    - Stream connection (separate voice WS to stream server)
    - Media pipeline (FFmpeg -> demux -> packetize -> encrypt -> send)

    Usage:
        streamer = VideoStreamer(client)
        await streamer.join_voice(guild_id, channel_id)
        await streamer.start_go_live()
        await streamer.play('input.mp4', StreamOptions(...))
        streamer.stop()
    """

    def __init__(self, client: discord.Client) -> None:
        self._client = client
        self._voice_client: Optional[discord.VoiceClient] = None
        self._stream_conn: Optional[StreamConnection] = None
        self._video_sender: Optional[VideoSender] = None

        # Media pipeline
        self._ffmpeg: Optional[FFmpegProcess] = None
        self._demuxer: Optional[Demuxer] = None
        self._video_pacer: Optional[FramePacer] = None
        self._audio_pacer: Optional[FramePacer] = None
        self._send_task: Optional[asyncio.Task] = None
        self._rtcp_task: Optional[asyncio.Task] = None

        # Senders
        self._video_sender: Optional[VideoSender] = None
        self._audio_sender: Optional[AudioSender] = None

        # Gateway event listeners
        self._stream_create_event: Optional[asyncio.Event] = None
        self._stream_server_event: Optional[asyncio.Event] = None
        self._voice_ready_event: Optional[asyncio.Event] = None

        # State
        self._guild_id: Optional[str] = None
        self._channel_id: Optional[str] = None
        self._user_id: Optional[str] = None
        self._session_id: Optional[str] = None

        # Register gateway event listener
        self._setup_gateway_listener()

    @property
    def voice_client(self) -> Optional[discord.VoiceClient]:
        return self._voice_client

    @property
    def stream_connection(self) -> Optional[StreamConnection]:
        return self._stream_conn

    @property
    def is_streaming(self) -> bool:
        return self._send_task is not None and not self._send_task.done()

    def _setup_gateway_listener(self) -> None:
        """Register a listener for raw gateway events.

        We need to intercept STREAM_CREATE and STREAM_SERVER_UPDATE
        events from the Discord gateway. These are not handled by
        discord.py natively, so we listen via the socket_raw_receive
        dispatch.

        Chains onto any existing on_socket_raw_receive handler to
        avoid overriding it.
        """
        async def _on_socket_raw_receive(data):
            if isinstance(data, str):
                try:
                    msg = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    return
            elif isinstance(data, dict):
                msg = data
            else:
                return

            event = msg.get('t')
            event_data = msg.get('d', {})

            if event == 'STREAM_CREATE':
                await self._on_stream_create(event_data)
            elif event == 'STREAM_SERVER_UPDATE':
                await self._on_stream_server_update(event_data)
            elif event == 'VOICE_STATE_UPDATE':
                await self._on_voice_state_update(event_data)
            elif event == 'VOICE_SERVER_UPDATE':
                await self._on_voice_server_update(event_data)

        # Chain onto existing handler if present
        prev = getattr(self._client, 'on_socket_raw_receive', None)

        async def _chained_handler(data):
            if prev:
                result = prev(data)
                if asyncio.iscoroutine(result):
                    await result
            await _on_socket_raw_receive(data)

        self._client.on_socket_raw_receive = _chained_handler

    async def _on_stream_create(self, data: Dict[str, Any]) -> None:
        """Handle STREAM_CREATE gateway event.

        Extracts rtc_server_id and stream_key, assigns to stream connection.
        """
        stream_key = data.get('stream_key', '')
        rtc_server_id = data.get('rtc_server_id', '')

        if self._stream_conn is None:
            return

        # Verify this event is for our stream
        parsed = parse_stream_key(stream_key)
        if (
            parsed.get('guild_id') != self._guild_id
            or parsed.get('channel_id') != self._channel_id
            or parsed.get('user_id') != self._user_id
        ):
            return

        self._stream_conn.server_id = str(rtc_server_id)
        self._stream_conn.stream_key = stream_key
        self._stream_conn.set_session(self._session_id or '')

        log.info('STREAM_CREATE: server_id=%s key=%s', rtc_server_id, stream_key)

        if self._stream_create_event is not None:
            self._stream_create_event.set()

    async def _on_stream_server_update(self, data: Dict[str, Any]) -> None:
        """Handle STREAM_SERVER_UPDATE gateway event.

        Extracts endpoint and token for the stream voice server.
        """
        stream_key = data.get('stream_key', '')
        endpoint = data.get('endpoint', '')
        token = data.get('token', '')

        if self._stream_conn is None:
            return

        # Verify this event is for our stream
        parsed = parse_stream_key(stream_key)
        if (
            parsed.get('guild_id') != self._guild_id
            or parsed.get('channel_id') != self._channel_id
            or parsed.get('user_id') != self._user_id
        ):
            return

        self._stream_conn.set_tokens(endpoint, token)

        log.info('STREAM_SERVER_UPDATE: endpoint=%s', endpoint)

        if self._stream_server_event is not None:
            self._stream_server_event.set()

    async def _on_voice_state_update(self, data: Dict[str, Any]) -> None:
        """Handle VOICE_STATE_UPDATE to capture session_id."""
        user_id = str(data.get('user_id', ''))
        if user_id == self._user_id:
            self._session_id = data.get('session_id')
            # Propagate updated session_id to stream connection if it exists.
            # Discord sends a new session_id when STREAM_CREATE is processed,
            # and the stream connection needs the updated value for IDENTIFY.
            if self._stream_conn is not None:
                self._stream_conn.session_id = self._session_id or ''
            if self._voice_ready_event is not None:
                self._voice_ready_event.set()

    async def _on_voice_server_update(self, data: Dict[str, Any]) -> None:
        """Handle VOICE_SERVER_UPDATE (handled by discord.py internally)."""
        pass

    def _send_gateway(self, op: int, data: Dict[str, Any]) -> None:
        """Send an opcode to the Discord MAIN gateway (not voice WS).

        Gateway opcodes like STREAM_CREATE, STREAM_SET_PAUSED, and
        VOICE_STATE_UPDATE must be sent over the main gateway connection,
        not the voice WebSocket.
        """
        try:
            # dpy-self: client.ws is the main gateway
            ws = self._client.ws
            if ws is None:
                log.error('Main gateway WS not available')
                return
            payload = {'op': op, 'd': data}
            # dpy-self's DiscordWebSocket has .send() for JSON
            task = asyncio.ensure_future(ws.send_as_json(payload))
            task.add_done_callback(self._handle_gateway_send_error)
        except Exception as e:
            log.error('Failed to send gateway opcode %s: %s', op, e)

    @staticmethod
    def _handle_gateway_send_error(task: asyncio.Task) -> None:
        """Callback to log errors from fire-and-forget gateway sends."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error('Gateway send error: %s', exc)

    async def join_voice(self, guild_id: int, channel_id: int) -> None:
        """Join a voice channel using discord.py's VoiceClient.

        Parameters
        ----------
        guild_id : int
            The guild ID containing the voice channel.
        channel_id : int
            The voice channel ID to join.
        """
        import discord

        guild = self._client.get_guild(guild_id)
        if guild is None:
            raise ValueError(f'Guild {guild_id} not found')

        channel = guild.get_channel(channel_id)
        if channel is None:
            raise ValueError(f'Channel {channel_id} not found in guild {guild_id}')

        if not isinstance(channel, discord.VoiceChannel) and not isinstance(channel, discord.StageChannel):
            raise ValueError(f'Channel {channel_id} is not a voice channel')

        self._guild_id = str(guild_id)
        self._channel_id = str(channel_id)
        self._user_id = str(self._client.user.id) if self._client.user else None

        if self._user_id is None:
            raise RuntimeError('Client not logged in')

        # Wait for voice state update to get session_id
        self._voice_ready_event = asyncio.Event()

        # Connect to voice
        self._voice_client = await channel.connect()
        log.info('Joined voice channel %s in guild %s', channel_id, guild_id)

        # Wait for session_id
        try:
            await asyncio.wait_for(self._voice_ready_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            log.warning('Timeout waiting for voice state update')

    async def start_go_live(self) -> None:
        """Start a Go Live stream.

        Sends STREAM_CREATE and STREAM_SET_PAUSED to the gateway,
        waits for STREAM_CREATE and STREAM_SERVER_UPDATE events,
        then connects to the stream voice server.
        """
        if self._voice_client is None:
            raise RuntimeError('Not in a voice channel')

        if self._guild_id is None or self._channel_id is None or self._user_id is None:
            raise RuntimeError('Missing guild/channel/user ID')

        # Create stream connection
        self._stream_conn = StreamConnection(
            guild_id=self._guild_id,
            channel_id=self._channel_id,
            user_id=self._user_id,
            session_id=self._session_id or '',
        )

        # Set up events for waiting
        self._stream_create_event = asyncio.Event()
        self._stream_server_event = asyncio.Event()

        # Send STREAM_CREATE gateway opcode
        self._send_gateway(GatewayOpCodes.STREAM_CREATE, {
            'type': self._stream_conn.type,
            'guild_id': self._guild_id,
            'channel_id': self._channel_id,
            'preferred_region': None,
        })

        # Send STREAM_SET_PAUSED
        stream_key = generate_stream_key(
            self._stream_conn.type,
            self._guild_id,
            self._channel_id,
            self._user_id,
        )
        self._send_gateway(GatewayOpCodes.STREAM_SET_PAUSED, {
            'stream_key': stream_key,
            'paused': False,
        })

        log.info('Sent STREAM_CREATE and STREAM_SET_PAUSED')

        # Wait for STREAM_CREATE event
        try:
            await asyncio.wait_for(self._stream_create_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            raise RuntimeError('Timeout waiting for STREAM_CREATE event')

        # Wait for STREAM_SERVER_UPDATE event
        try:
            await asyncio.wait_for(self._stream_server_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            raise RuntimeError('Timeout waiting for STREAM_SERVER_UPDATE event')

        # Connect to stream voice server
        await self._stream_conn.connect()

        # Send SPEAKING opcode (mode=2 for Go Live)
        self._stream_conn.set_speaking(True)

        # VIDEO attributes are sent when play() is called with actual resolution
        log.info('Go Live stream started, ready for media')

    async def play(
        self,
        url: str,
        options: Optional[StreamOptions] = None,
    ) -> None:
        """Start streaming media via Go Live.

        Parameters
        ----------
        url : str
            Input URL or file path. Any FFmpeg-supported input.
        options : StreamOptions, optional
            Transcoding options. Defaults to 720p30 at 5000kbps.
        """
        if self._stream_conn is None:
            raise RuntimeError('Go Live not started')

        if self.is_streaming:
            raise RuntimeError('Already streaming')

        # Create options
        if options is None:
            options = StreamOptions(
                url=url,
                width=1280,
                height=720,
                bitrate_video=5000,
                bitrate_video_max=7000,
                bitrate_audio=128,
                include_audio=True,
            )
        else:
            options.url = url

        # Start FFmpeg
        self._ffmpeg = FFmpegProcess(options)
        proc = await self._ffmpeg.start()
        log.info('FFmpeg started: pid=%s', proc.pid)

        # Create demuxer
        self._demuxer = Demuxer(format='nut')

        # Determine actual video dimensions
        vid_width = options.width if options.width > 0 else 1280
        vid_height = options.height if options.height > 0 else 720
        vid_fps = options.frame_rate or 30

        # Send VIDEO opcode with actual stream attributes
        self._stream_conn.set_video_attributes(
            enabled=True,
            attrs=VideoAttributes(width=vid_width, height=vid_height, fps=vid_fps),
        )

        # Create video sender
        self._video_sender = VideoSender(self._stream_conn)
        self._video_sender.configure(
            width=vid_width,
            height=vid_height,
            fps=vid_fps,
        )

        # Create audio sender
        self._audio_sender = AudioSender(self._stream_conn)

        # Set up UDP send callback -- sends to the STREAM server's
        # endpoint, NOT the main voice connection's endpoint.
        def send_udp(packet: bytes, ip: str, port: int) -> None:
            if self._voice_client is not None and hasattr(self._voice_client, '_connection'):
                conn = self._voice_client._connection
                if hasattr(conn, 'socket') and conn.socket is not None:
                    try:
                        conn.socket.sendto(packet, (ip, port))
                    except Exception as e:
                        log.warning('UDP send error: %s', e)

        self._video_sender.set_send_callback(send_udp)
        self._audio_sender.set_send_callback(send_udp)

        # Start both senders (reads SSRCs, keys, endpoint from stream conn)
        self._video_sender.start()
        self._audio_sender.start()

        # Create pacers
        self._video_pacer = FramePacer(clock_rate=90000)
        self._audio_pacer = FramePacer(clock_rate=48000)
        self._video_pacer.sync_partner = self._audio_pacer

        # Start send loop
        self._send_task = asyncio.create_task(
            self._send_loop(proc.stdout),
            name='stream-send-loop',
        )

        # Start periodic RTCP Sender Report task (every 5 seconds)
        self._rtcp_task = asyncio.create_task(
            self._rtcp_sr_loop(),
            name='stream-rtcp-sr',
        )

        log.info('Streaming started')

    async def _send_loop(self, pipe) -> None:
        """Main loop: demux frames, pace, packetize, encrypt, send.

        Both video and audio frames are sent through the stream
        connection's RTP path with packet-level pacing at 25 Mbps
        to prevent burst-induced packet loss.
        """
        if self._demuxer is None or self._video_sender is None:
            return
        if self._audio_sender is None:
            return
        if self._video_pacer is None or self._audio_pacer is None:
            return

        # Packet pacing: 25 Mbps = 3,125,000 bytes/sec
        # Sleep time per byte = 1 / 3_125_000 seconds
        PACING_BYTES_PER_SEC = 25_000_000 // 8

        try:
            async for frame in self._demuxer.demux(pipe):
                if frame.frame_type == FrameType.VIDEO:
                    # Pace video frame timing
                    await self._video_pacer.pace(
                        frame.pts_ms,
                        frame.frametime_ms,
                    )

                    # Send video frame (returns list of RTP packets)
                    pkt_count = self._video_sender.send_frame(
                        frame.data,
                        frame.pts_ms,
                        frame.frametime_ms,
                    )

                    # Packet-level pacing: sleep proportional to data sent
                    # to prevent burst packet loss on keyframes
                    if pkt_count > 1:
                        # Estimate total wire bytes: ~1300 per packet
                        pacing_sleep = (pkt_count * 1300) / PACING_BYTES_PER_SEC
                        await asyncio.sleep(pacing_sleep)

                    # Update audio pacer with current video PTS (for sync)
                    self._audio_pacer.update_pts(frame.pts_ms)

                elif frame.frame_type == FrameType.AUDIO:
                    # Pace audio frame timing for sync reference
                    await self._audio_pacer.pace(
                        frame.pts_ms,
                        frame.frametime_ms,
                    )

                    # Send audio frame through stream connection
                    self._audio_sender.send_frame(
                        frame.data,
                        frame.pts_ms,
                        frame.frametime_ms,
                    )

                    # Update video pacer with current audio PTS (for sync)
                    self._video_pacer.update_pts(frame.pts_ms)

        except asyncio.CancelledError:
            log.info('Send loop cancelled')
        except Exception as e:
            log.error('Send loop error: %s', e)
        finally:
            log.info('Send loop ended')

    async def _rtcp_sr_loop(self) -> None:
        """Periodically send RTCP Sender Reports for A/V sync.

        Sends a Sender Report every 5 seconds. RTCP SRs allow
        receivers to correlate NTP time with RTP timestamps,
        which is essential for audio/video synchronization.
        """
        try:
            while True:
                await asyncio.sleep(5.0)
                if self._video_sender is not None and self._video_sender.active:
                    self._video_sender.send_rtcp_sender_report()
                    log.debug('Sent RTCP Sender Report')
        except asyncio.CancelledError:
            log.debug('RTCP SR loop cancelled')

    def stop(self) -> None:
        """Stop the current stream."""
        # Stop RTCP SR loop
        if self._rtcp_task is not None:
            self._rtcp_task.cancel()
            self._rtcp_task = None

        # Stop send loop
        if self._send_task is not None:
            self._send_task.cancel()
            self._send_task = None

        # Stop video sender
        if self._video_sender is not None:
            self._video_sender.stop()
            self._video_sender = None

        # Stop audio sender
        if self._audio_sender is not None:
            self._audio_sender.stop()
            self._audio_sender = None

        # Stop FFmpeg (await properly, not fire-and-forget)
        if self._ffmpeg is not None:
            asyncio.ensure_future(self._ffmpeg.stop())
            self._ffmpeg = None

        # Send STREAM_DELETE
        if self._stream_conn is not None and self._guild_id and self._channel_id and self._user_id:
            stream_key = generate_stream_key(
                self._stream_conn.type,
                self._guild_id,
                self._channel_id,
                self._user_id,
            )
            self._send_gateway(GatewayOpCodes.STREAM_DELETE, {
                'stream_key': stream_key,
            })
            log.info('Sent STREAM_DELETE')

        # Stop stream connection
        if self._stream_conn is not None:
            self._stream_conn.stop()
            self._stream_conn = None

        # Clean up pacers
        self._video_pacer = None
        self._audio_pacer = None
        self._demuxer = None

        log.info('Stream stopped')

    async def leave(self) -> None:
        """Stop streaming and leave the voice channel."""
        self.stop()

        if self._voice_client is not None:
            # Send VOICE_STATE_UPDATE to leave
            self._send_gateway(GatewayOpCodes.VOICE_STATE_UPDATE, {
                'guild_id': None,
                'channel_id': None,
                'self_mute': True,
                'self_deaf': False,
                'self_video': False,
            })

            # Disconnect
            await self._voice_client.disconnect()
            self._voice_client = None

        self._guild_id = None
        self._channel_id = None
        self._user_id = None
        self._session_id = None

        log.info('Left voice channel')

    def set_stream_preview(self, image_data: bytes) -> None:
        """Set the Go Live stream preview image.

        Parameters
        ----------
        image_data : bytes
            JPEG image data, resized to 1024x576.
        """
        import base64
        if self._stream_conn is None or self._guild_id is None:
            return

        data_uri = f'data:image/jpeg;base64,{base64.b64encode(image_data).decode()}'
        # This would require a REST API call to Discord
        # PUT /guilds/{guild.id}/voice-states/@me/preview
        # Implementation deferred to when REST access is available
        log.info('Stream preview set (REST call not yet implemented)')
