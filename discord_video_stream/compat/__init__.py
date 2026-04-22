# compat - optional integration with discord-ext-voice-recv
from .voice_recv import VoiceSendRecvClient, HAS_VOICE_RECV

__all__ = [
    'VoiceSendRecvClient',
    'HAS_VOICE_RECV',
]
