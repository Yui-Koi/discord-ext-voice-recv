# -*- coding: utf-8 -*-
# Minimal Go Live controller for a single self account (non-sharded).
# Focus: send gateway ops without relying on unavailable ConnectionState internals.
# No security/validation concerns handled here per request.

import asyncio
import logging
from typing import Dict, Any, Optional, Tuple

import aiohttp
import discord
from discord.ext import commands

log = logging.getLogger("golive")

VOICE_OP_STREAM_CREATE = 18
VOICE_OP_STREAM_SET_PAUSED = 22


class ControlClient:
    def __init__(self, base_url: str = "http://localhost:3000"):
        self.base_url = base_url
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def start_go_live(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        async with self.session.post(f"{self.base_url}/go-live/start", json=payload) as resp:
            data = await resp.json()
            return data

    async def stop_go_live(self, stream_key: str) -> Dict[str, Any]:
        async with self.session.post(f"{self.base_url}/go-live/stop", json={"stream_key": stream_key}) as resp:
            data = await resp.json()
            return data

    async def status(self, stream_key: str) -> Dict[str, Any]:
        async with self.session.get(f"{self.base_url}/go-live/status", params={"stream_key": stream_key}) as resp:
            return await resp.json()

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


class _SingleShardWSSender:
    """Resolves a send_json-capable websocket for single-user selfbot."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._ws = None

    def resolve(self):
        # Prefer top-level client.ws if present (common in dpy-self forks for single shard)
        ws = getattr(self.bot, "ws", None)
        if ws is None:
            # Some forks expose _connection.ws
            conn = getattr(self.bot, "_connection", None)
            if conn is not None:
                ws = getattr(conn, "ws", None)

        if ws is None:
            raise RuntimeError("No websocket found on client (expected single-shard .ws)")

        # Basic capability check
        if not hasattr(ws, "send_json"):
            raise RuntimeError("Resolved websocket has no send_json method")

        self._ws = ws

    async def send_json(self, payload: Dict[str, Any]):
        if self._ws is None:
            self.resolve()
        await self._ws.send_json(payload)


class GoLiveController:
    def __init__(
        self,
        bot: commands.Bot,
        control: ControlClient,
        *,
        encryption_preference: Optional[str] = None,  # "AES256" | "XCHACHA20"
        ffmpeg_input: str = "",
        ffmpeg_options: Optional[Dict[str, Any]] = None,
        video_attrs: Optional[Dict[str, int]] = None,
    ):
        self.bot = bot
        self.control = control
        self.encryption_preference = encryption_preference
        self.ffmpeg_input = ffmpeg_input
        self.ffmpeg_options = ffmpeg_options or {}
        self.video_attrs = video_attrs or {"width": 1280, "height": 720, "fps": 30}

        self._ws_sender = _SingleShardWSSender(bot)

        self._session_id_by_guild: Dict[Optional[int], str] = {}
        self._pending_golive: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self._active_stream_key: Optional[str] = None

        # Listen to raw socket events; discord.py-self usually emits 'socket_response'
        bot.add_listener(self._on_raw_socket_response, "on_socket_response")

    async def send_gateway_opcode(self, op: int, d: Dict[str, Any]):
        await self._ws_sender.send_json({"op": op, "d": d})

    async def start_golive(self, guild_id: int, channel_id: int, preferred_region: Optional[str] = None):
        user = self.bot.user
        if not user:
            raise RuntimeError("Bot not logged in")

        await self.send_gateway_opcode(
            VOICE_OP_STREAM_CREATE,
            {
                "type": "guild",
                "guild_id": str(guild_id),
                "channel_id": str(channel_id),
                "preferred_region": preferred_region,
            },
        )

        self._pending_golive[(guild_id, channel_id)] = {
            "user_id": str(user.id),
            "guild_id": str(guild_id),
            "channel_id": str(channel_id),
            "type": "guild",
            "session_id": None,
            "stream_key": None,
            "rtc_server_id": None,
            "endpoint": None,
            "token": None,
        }

    async def stop_golive(self):
        if not self._active_stream_key:
            return
        try:
            await self.control.stop_go_live(self._active_stream_key)
        finally:
            self._active_stream_key = None

    async def _on_raw_socket_response(self, payload: Dict[str, Any]):
        t = payload.get("t")
        d = payload.get("d") or {}

        # VOICE_STATE_UPDATE -> capture our session_id
        if t == "VOICE_STATE_UPDATE":
            user_id = d.get("user_id")
            sess = d.get("session_id")
            guild_id = d.get("guild_id")
            if self.bot.user and str(self.bot.user.id) == str(user_id) and isinstance(sess, str):
                key = int(guild_id) if guild_id is not None else None
                self._session_id_by_guild[key] = sess
                # Backfill pending
                for (g_id, ch_id), info in list(self._pending_golive.items()):
                    if str(g_id) == str(guild_id):
                        info["session_id"] = sess

        # STREAM_CREATE -> stream_key, rtc_server_id
        if t == "STREAM_CREATE":
            sk = d.get("stream_key")
            rtc_server_id = d.get("rtc_server_id")
            parts = str(sk).split(":")
            if len(parts) >= 4 and parts[0] == "guild":
                g_id = int(parts[1])
                ch_id = int(parts[2])
                user_id = parts[3]
                info = self._pending_golive.get((g_id, ch_id))
                if info and info["user_id"] == user_id:
                    info["stream_key"] = sk
                    info["rtc_server_id"] = rtc_server_id
                    # Unpause stream
                    asyncio.create_task(self._stream_set_paused(sk, paused=False))

        # STREAM_SERVER_UPDATE -> endpoint, token -> start Node when session exists
        if t == "STREAM_SERVER_UPDATE":
            sk = d.get("stream_key")
            endpoint = d.get("endpoint")
            token = d.get("token")
            parts = str(sk).split(":")
            if len(parts) >= 4 and parts[0] == "guild":
                g_id = int(parts[1])
                ch_id = int(parts[2])
                user_id = parts[3]
                info = self._pending_golive.get((g_id, ch_id))
                if info and info.get("stream_key") == sk and info["user_id"] == user_id:
                    info["endpoint"] = endpoint
                    info["token"] = token

                    sess = self._session_id_by_guild.get(g_id) or info.get("session_id")
                    if not sess:
                        log.warning("No session_id yet; will start once VOICE_STATE_UPDATE arrives")
                        return

                    payload = {
                        "guild_id": info["guild_id"],
                        "channel_id": info["channel_id"],
                        "user_id": info["user_id"],
                        "session_id": sess,
                        "stream_key": info["stream_key"],
                        "rtc_server_id": info["rtc_server_id"],
                        "endpoint": info["endpoint"],
                        "token": info["token"],
                        "video": self.video_attrs,
                        "encryptionPreference": self.encryption_preference,
                        "ffmpeg": {
                            "input": self.ffmpeg_input,
                            "options": self.ffmpeg_options,
                        },
                    }
                    asyncio.create_task(self._start_node(payload))
                    self._active_stream_key = sk
                    self._pending_golive.pop((g_id, ch_id), None)

    async def _stream_set_paused(self, stream_key: str, paused: bool):
        await self.send_gateway_opcode(
            VOICE_OP_STREAM_SET_PAUSED,
            {"stream_key": stream_key, "paused": paused},
        )

    async def _start_node(self, payload: Dict[str, Any]):
        try:
            await self.control.start_go_live(payload)
            log.info("Go Live started via Node")
        except Exception as e:
            log.error("Failed to start Node Go Live: %s", e)