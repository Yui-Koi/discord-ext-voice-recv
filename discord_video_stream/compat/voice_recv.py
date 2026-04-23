"""
Optional compatibility module for discord-ext-voice-recv.

Provides VoiceSendRecvClient that extends VoiceRecvClient with video
send capability. When both discord-video-stream and discord-ext-voice-recv
are installed, this module creates a unified client for bidirectional
voice/video.

The shared surface between send and receive:
- UDP socket (via discord.py's VoiceClient._connection.socket)
- DAVE session (via discord.py's VoiceClient._connection.dave_session)
- SSRC <-> user ID mapping (via VoiceRecvClient._ssrc_to_id)
- Gateway hooks (extended to handle both send and receive events)

Usage:
    from discord_video_stream.compat.voice_recv import VoiceSendRecvClient

    client = discord.Client(...)
    # VoiceSendRecvClient works exactly like VoiceRecvClient for receiving,
    # plus adds send_video(), start_go_live(), etc.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

try:
    from discord.ext.voice_recv import VoiceRecvClient
    HAS_VOICE_RECV = True
except ImportError:
    HAS_VOICE_RECV = False

if TYPE_CHECKING:
    from typing import Optional
    import discord

__all__ = [
    'VoiceSendRecvClient',
    'HAS_VOICE_RECV',
]

log = logging.getLogger(__name__)


if HAS_VOICE_RECV:
    class VoiceSendRecvClient(VoiceRecvClient):
        """Unified client for bidirectional voice/video.

        Extends VoiceRecvClient with:
        - Video sending via Go Live
        - Stream connection management
        - SPS VUI rewriting
        - DAVE video frame encryption

        Receive functionality is inherited from VoiceRecvClient
        (AudioSink, PacketRouter, SpeakingTimer, etc.).

        The UDP socket and DAVE session are shared between send and
        receive paths. The send side uses separate SSRCs, sequence
        numbers, timestamps, and nonce counters from the receive side.
        """

        def __init__(self, client: discord.Client, channel: discord.abc.Connectable):
            super().__init__(client, channel)

            # Send-side state (populated by VideoStreamer)
            self._stream_conn = None
            self._video_sender = None

        @property
        def stream_connection(self):
            """The active Go Live stream connection, or None."""
            return self._stream_conn

        @property
        def video_sender(self):
            """The active video sender, or None."""
            return self._video_sender

        def set_stream_components(self, stream_conn, video_sender) -> None:
            """Attach stream connection and video sender.

            Called by VideoStreamer after establishing the Go Live
            connection. The stream components use the shared socket
            and DAVE session from this VoiceClient.
            """
            self._stream_conn = stream_conn
            self._video_sender = video_sender

        def clear_stream_components(self) -> None:
            """Detach stream components (called when stream stops)."""
            self._stream_conn = None
            self._video_sender = None

        def send_video_packet(self, packet: bytes, ip: str = None, port: int = None) -> None:
            """Send a video RTP packet over the shared UDP socket.

            Parameters
            ----------
            packet : bytes
                Complete wire packet (RTP header + encrypted payload + nonce).
            ip : str, optional
                Target IP. If None, uses the stream connection's endpoint.
            port : int, optional
                Target port. If None, uses the stream connection's endpoint.
            """
            if self._connection and self._connection.socket:
                try:
                    # Use stream connection endpoint if not specified
                    if ip is None or port is None:
                        if self._stream_conn is not None and self._stream_conn.ready_params is not None:
                            ready = self._stream_conn.ready_params
                            ip = ready.ip
                            port = ready.port
                        else:
                            log.warning('No stream endpoint available for video send')
                            return
                    self._connection.socket.sendto(packet, (ip, port))
                except Exception as e:
                    log.warning('Video packet send error: %s', e)

        def cleanup(self) -> None:
            """Clean up both send and receive resources."""
            self.clear_stream_components()
            super().cleanup()

else:
    # voice-recv not installed; provide a stub that raises on use
    class VoiceSendRecvClient:
        """Stub when discord-ext-voice-recv is not installed.

        Raises ImportError on instantiation.
        """

        def __init__(self, *args, **kwargs):
            raise ImportError(
                'discord-ext-voice-recv is required for VoiceSendRecvClient. '
                'Install it with: pip install discord-ext-voice-recv'
            )
