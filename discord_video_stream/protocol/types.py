"""
Protocol data structures for Discord voice/video streaming.

Defines codec configurations, stream key format, VIDEO opcode payload
structures, and gateway opcodes for stream lifecycle management.

Reference: discord-video-stream/src/client/voice/CodecPayloadType.ts
           discord-video-stream/src/utils.ts
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Dict, List, Optional, Any

__all__ = [
    'CodecConfig',
    'CODEC_OPUS',
    'CODEC_H264',
    'CODEC_H265',
    'CODEC_VP8',
    'CODEC_VP9',
    'CODEC_AV1',
    'STREAMS_SIMULCAST',
    'SUPPORTED_ENCRYPTION_MODES',
    'GatewayOpCodes',
    'generate_stream_key',
    'parse_stream_key',
    'build_video_payload',
    'build_video_off_payload',
]


@dataclass(frozen=True)
class CodecConfig:
    """RTP codec configuration sent in SELECT_PROTOCOL."""
    name: str
    type: str           # "audio" or "video"
    clock_rate: int
    priority: int
    payload_type: int
    rtx_payload_type: Optional[int] = None
    encode: bool = False
    decode: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            'name': self.name,
            'type': self.type,
            'clockRate': self.clock_rate,
            'priority': self.priority,
            'payload_type': self.payload_type,
        }
        if self.rtx_payload_type is not None:
            d['rtx_payload_type'] = self.rtx_payload_type
        if self.encode:
            d['encode'] = True
        if self.decode:
            d['decode'] = True
        return d


# Codec payload types (from Node.js reference, hardcoded values that work)
CODEC_OPUS = CodecConfig(
    name='opus', type='audio', clock_rate=48000, priority=1000, payload_type=120,
)
CODEC_H264 = CodecConfig(
    name='H264', type='video', clock_rate=90000, priority=1000,
    payload_type=101, rtx_payload_type=102, encode=True, decode=True,
)
CODEC_H265 = CodecConfig(
    name='H265', type='video', clock_rate=90000, priority=1000,
    payload_type=103, rtx_payload_type=104, encode=True, decode=True,
)
CODEC_VP8 = CodecConfig(
    name='VP8', type='video', clock_rate=90000, priority=1000,
    payload_type=105, rtx_payload_type=106, encode=True, decode=True,
)
CODEC_VP9 = CodecConfig(
    name='VP9', type='video', clock_rate=90000, priority=1000,
    payload_type=107, rtx_payload_type=108, encode=True, decode=True,
)
CODEC_AV1 = CodecConfig(
    name='AV1', type='video', clock_rate=90000, priority=1000,
    payload_type=109, rtx_payload_type=110, encode=True, decode=True,
)

# All codecs to send in SELECT_PROTOCOL
ALL_CODECS = [CODEC_OPUS, CODEC_H264, CODEC_H265, CODEC_VP8, CODEC_VP9, CODEC_AV1]

# Simulcast streams (single quality, from Node.js reference)
STREAMS_SIMULCAST = [{'type': 'screen', 'rid': '100', 'quality': 100}]

# Supported encryption modes (in preference order)
SUPPORTED_ENCRYPTION_MODES = [
    'aead_aes256_gcm_rtpsize',
    'aead_xchacha20_poly1305_rtpsize',
]


class GatewayOpCodes:
    """Discord gateway opcodes for stream lifecycle."""
    VOICE_STATE_UPDATE = 4
    STREAM_CREATE = 18
    STREAM_DELETE = 19
    STREAM_SET_PAUSED = 22


def generate_stream_key(
    type: str,
    guild_id: Optional[str],
    channel_id: str,
    user_id: str,
) -> str:
    """Generate a stream key for Go Live.

    Format:
    - Guild: "guild:{guild_id}:{channel_id}:{user_id}"
    - Call/DM: "call:{channel_id}:{user_id}"
    """
    if type == 'guild':
        return f'guild:{guild_id}:{channel_id}:{user_id}'
    return f'call:{channel_id}:{user_id}'


def parse_stream_key(stream_key: str) -> Dict[str, Optional[str]]:
    """Parse a stream key into its components."""
    parts = stream_key.split(':')
    key_type = parts[0]

    if key_type not in ('guild', 'call'):
        raise ValueError(f'Invalid stream key type: {key_type}')

    guild_id = None
    if key_type == 'guild':
        guild_id = parts[1] if len(parts) > 1 else None
        channel_id = parts[2] if len(parts) > 2 else None
        user_id = parts[3] if len(parts) > 3 else None
    else:
        channel_id = parts[1] if len(parts) > 1 else None
        user_id = parts[2] if len(parts) > 2 else None

    if not channel_id or not user_id:
        raise ValueError(f'Invalid stream key: {stream_key}')

    return {
        'type': key_type,
        'channel_id': channel_id,
        'guild_id': guild_id,
        'user_id': user_id,
    }


@dataclass
class VideoAttributes:
    """Video stream attributes sent via VIDEO opcode."""
    width: int
    height: int
    fps: int


def build_video_payload(
    audio_ssrc: int,
    video_ssrc: int,
    rtx_ssrc: int,
    attrs: VideoAttributes,
    max_bitrate: int = 10_000_000,
) -> Dict[str, Any]:
    """Build VIDEO opcode (12) payload for enabling video."""
    return {
        'audio_ssrc': audio_ssrc,
        'video_ssrc': video_ssrc,
        'rtx_ssrc': rtx_ssrc,
        'streams': [
            {
                'type': 'video',
                'rid': '100',
                'ssrc': video_ssrc,
                'active': True,
                'quality': 100,
                'rtx_ssrc': rtx_ssrc,
                'max_bitrate': max_bitrate,
                'max_framerate': attrs.fps,
                'max_resolution': {
                    'type': 'fixed',
                    'width': attrs.width,
                    'height': attrs.height,
                },
            },
        ],
    }


def build_video_off_payload(audio_ssrc: int) -> Dict[str, Any]:
    """Build VIDEO opcode (12) payload for disabling video."""
    return {
        'audio_ssrc': audio_ssrc,
        'video_ssrc': 0,
        'rtx_ssrc': 0,
        'streams': [],
    }
