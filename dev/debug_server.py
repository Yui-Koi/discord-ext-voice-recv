"""
IPC debug server for runtime introspection of the running bot.

Connect via: echo "status" | socat - UNIX-CONNECT:/tmp/discord-debug.sock
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

COMMANDS = {
    "status": "Show streaming/connection status",
    "state": "Dump full connection state",
    "aspects": "List active aspects",
    "help": "Show available commands",
}


class DebugServer:
    """Unix socket IPC server for runtime debugging."""

    def __init__(self, path: str = "/tmp/discord-debug.sock"):
        self._path = path
        self._server = None
        self._streamer = None
        self._start_time = time.time()

    def attach(self, streamer) -> None:
        """Attach to a VideoStreamer instance."""
        self._streamer = streamer

    async def start(self) -> None:
        """Start the debug server."""
        if os.path.exists(self._path):
            os.unlink(self._path)
        self._server = await asyncio.start_unix_server(self._handle, self._path)
        os.chmod(self._path, 0o600)
        log.info("debug_server_started", path=self._path)

    async def stop(self) -> None:
        """Stop the debug server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if os.path.exists(self._path):
            os.unlink(self._path)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.readline(), timeout=30.0)
            cmd = data.decode().strip().lower()
            response = await self._dispatch(cmd)
            writer.write(response.encode() + b"\n")
            await writer.drain()
        except asyncio.TimeoutError:
            writer.write(b"timeout\n")
            await writer.drain()
        except Exception as e:
            writer.write(f"error: {e}\n".encode())
            await writer.drain()
        finally:
            writer.close()

    async def _dispatch(self, cmd: str) -> str:
        if cmd == "status":
            return self._status()
        elif cmd == "state":
            return self._state()
        elif cmd == "aspects":
            return self._aspects()
        elif cmd == "help":
            return self._help()
        else:
            return f"unknown command: {cmd}. Type 'help' for available commands."

    def _status(self) -> str:
        s = self._streamer
        if s is None:
            return json.dumps({"error": "no streamer attached"}, indent=2)

        return json.dumps({
            "streaming": s.is_streaming,
            "voice_connected": s._voice_client is not None,
            "stream_connection": s._stream_conn is not None,
            "uptime_seconds": round(time.time() - self._start_time, 1),
        }, indent=2)

    def _state(self) -> str:
        s = self._streamer
        if s is None:
            return json.dumps({"error": "no streamer attached"}, indent=2)

        state = {
            "streaming": s.is_streaming,
            "voice_connected": s._voice_client is not None,
            "stream_connection_active": s._stream_conn is not None,
        }

        if s._stream_conn:
            conn = s._stream_conn
            state["stream"] = {
                "guild_id": conn.guild_id,
                "channel_id": conn.channel_id,
                "server_id": conn.server_id,
                "audio_ssrc": conn.audio_ssrc,
                "video_ssrc": conn.video_ssrc,
                "rtx_ssrc": conn.rtx_ssrc,
                "has_secret_key": conn.secret_key is not None,
                "encryption_mode": conn.encryption_mode,
                "dave_ready": conn.dave_ready,
                "ws_connected": conn._ws is not None and hasattr(conn._ws, 'open'),
            }

        return json.dumps(state, indent=2, default=str)

    def _aspects(self) -> str:
        from dev.aspects import _woven_targets
        targets = [f"{t[0].__name__ if hasattr(t[0], '__name__') else str(t[0])}: {t[1].__name__ if hasattr(t[1], '__name__') else 'aspect'}" for t in _woven_targets]
        return json.dumps({"active_aspects": targets, "count": len(targets)}, indent=2)

    def _help(self) -> str:
        lines = ["Available commands:"]
        for cmd, desc in COMMANDS.items():
            lines.append(f"  {cmd:12s} - {desc}")
        return "\n".join(lines)
