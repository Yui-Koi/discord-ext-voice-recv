import logging
from typing import Optional, TYPE_CHECKING
from discord.ext import commands
from .voice_client import VoiceRecvClient

_log = logging.getLogger(__name__)
_patch_applied = False

def apply_patch() -> None:
    global _patch_applied
    if _patch_applied:
        _log.debug("Context.voice_client patch already applied.")
        return

    original_prop = getattr(commands.Context, 'voice_client', None)
    if not isinstance(original_prop, property):
         _log.error("Original 'voice_client' property not found.")
         return

    def _patched_voice_client_getter(ctx: commands.Context) -> Optional:
        if ctx.guild:
            return ctx.guild.voice_client
        if not hasattr(ctx.bot, 'voice_clients'):
            return None
        for vc in ctx.bot.voice_clients:
            if vc.channel and vc.channel.id == ctx.channel.id:
                return vc
        return None

    new_prop = property(
        fget=_patched_voice_client_getter,
        fset=original_prop.fset,
        fdel=original_prop.fdel,
        doc=original_prop.__doc__ or ""
    )

    setattr(commands.Context, 'voice_client', new_prop)
    _patch_applied = True
    _log.info("Successfully patched Context.voice_client")
