# discord-ext-voice-recv

Voice receive extension for discord.py, with an integrated video streaming subsystem for Discord Go Live.

## Overview

This package provides two subsystems:

**Voice Receive** (`discord.ext.voice_recv`): A receive-only extension for discord.py that adds audio sink functionality. The API mirrors discord.py's AudioSource pattern in reverse -- where AudioSource produces audio data, AudioSink consumes it. Supports Opus decoding, sink pipelines, speaking state detection, and custom event listeners.

**Video Stream** (`discord_video_stream`): A Go Live video streaming implementation that sends H.264 video and Opus audio through Discord's voice infrastructure. Built on top of discord.py's VoiceClient, it manages a separate stream connection with independent SSRCs, secret keys, and DAVE sessions.

## Requirements

- Python 3.8 or higher (3.11+ recommended)
- discord.py-self with voice support
- pynacl (transport encryption)
- PyAV (NUT container demuxing)
- websockets (Go Live stream WebSocket)
- FFmpeg (video/audio transcoding)

## Installation

```
pip install discord-ext-voice-recv
```

For the video stream subsystem, additional dependencies are required:

```
pip install av websockets
```

Optional extras:

```
pip install "discord-ext-voice-recv[extras]"         # all extras
pip install "discord-ext-voice-recv[extras_speech]"   # speech recognition
pip install "discord-ext-voice-recv[extras_local]"    # local playback
```

## Voice Receive Usage

```python
import discord
from discord.ext import commands
from discord.ext import voice_recv

bot = commands.Bot(command_prefix="!")

@bot.command()
async def join(ctx):
    vc = await ctx.author.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
    sink = voice_recv.WaveSink("output.wav")
    vc.listen(sink)

@bot.command()
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()

bot.run("TOKEN")
```

For a more complete example, see [examples/recv.py](examples/recv.py).

### VoiceRecvClient

Use `voice_recv.VoiceRecvClient` as the `cls` parameter in `VoiceChannel.connect()`.

Methods:

- `listen(sink, *, after=None)` -- Receive audio into an AudioSink
- `is_listening()` -- Returns True if currently receiving audio
- `stop()` -- Stops both receiving and sending
- `stop_listening()` -- Stops receiving audio
- `stop_playing()` -- Stops playing audio
- `get_speaking(member)` -- Returns speaking state of a member, or None

### AudioSink

Sinks are the inverse of AudioSource. A source produces audio; a sink consumes it. Sinks can be composed into pipelines.

```python
class MySink(voice_recv.AudioSink):
    def __init__(self):
        super().__init__()

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData):
        # data.pcm contains decoded PCM audio (if wants_opus() is False)
        # data.opus contains raw Opus bytes (if wants_opus() is True)
        # data.packet contains the raw RTP packet
        ...

    def cleanup(self):
        ...
```

Built-in sinks:

- `AudioSink` -- Base class
- `MultiAudioSink` -- Dispatches to multiple child sinks
- `BasicSink` -- Simple callback-based sink
- `WaveSink` (alias: `WavSink`) -- Writes to WAV file
- `FFmpegSink` -- Pipes audio through FFmpeg
- `PCMVolumeTransformer` -- Controls volume
- `ConditionalFilter` -- Filters by predicate
  - `UserFilter` -- Filters by user
  - `TimedFilter` -- Filters by time duration
- `SilenceGeneratorSink` -- Generates silence during gaps (WIP)

Sink event listeners can be registered with `@AudioSink.listener()`:

```python
class MySink(voice_recv.AudioSink):
    @AudioSink.listener()
    def on_voice_member_disconnect(self, member, ssrc):
        print(f"{member} disconnected")
```

### New Events

- `on_voice_member_speaking_state(member, ssrc, state)` -- Speaking mode change (not the green circle)
- `on_voice_member_connect(member)` -- Member joins voice
- `on_voice_member_disconnect(member, ssrc)` -- Member leaves voice
- `on_voice_member_video(member, data)` -- Member toggles webcam
- `on_voice_member_flags(member, flags)` -- Member flags (clips, recording consent)
- `on_voice_member_platform(member, platform)` -- Member platform (desktop, mobile, etc.)
- `on_rtcp_packet(packet, guild)` -- RTCP packet received (sink only)
- `on_voice_member_speaking_start(member)` -- Speaking indicator on (sink only)
- `on_voice_member_speaking_stop(member)` -- Speaking indicator off (sink only)

### Extras

`voice_recv.extras.speechrecognition` -- SpeechRecognitionSink for speech-to-text. Requires `SpeechRecognition` package.

`voice_recv.extras.localplayback` -- LocalPlaybackSink and SimpleLocalPlaybackSink for playing audio through local output devices. Requires `pyaudio` package.

## Video Stream Usage

```python
import discord
from discord_video_stream import VideoStreamer

client = discord.Client(...)

@client.event
async def on_ready():
    streamer = VideoStreamer(client)
    await streamer.join_voice(guild_id, channel_id)
    await streamer.start_go_live()
    await streamer.play("input.mp4")
```

### VideoStreamer

Main API for Go Live streaming.

Methods:

- `join_voice(guild_id, channel_id)` -- Join a voice channel
- `start_go_live()` -- Initiate Go Live stream (sends STREAM_CREATE, connects to stream server)
- `play(url, options=None)` -- Start streaming media (FFmpeg transcoding + RTP sending)
- `stop()` -- Stop the current stream (sends STREAM_DELETE)
- `leave()` -- Stop streaming and leave voice channel

### StreamOptions

Configuration for the FFmpeg transcoding pipeline:

- `url` -- Input URL or file path
- `width` / `height` -- Output dimensions (default: -2, maintains aspect ratio)
- `frame_rate` -- Output frame rate (None preserves input)
- `bitrate_video` -- Target video bitrate in kbps (default: 5000)
- `bitrate_video_max` -- Max video bitrate in kbps (default: 7000)
- `bitrate_audio` -- Audio bitrate in kbps (default: 128)
- `include_audio` -- Whether to include audio (default: True)
- `no_transcoding` -- Passthrough mode, copy video codec (default: False)

### Compatibility Module

When both packages are installed, `discord_video_stream.compat.voice_recv` provides `VoiceSendRecvClient` -- a unified client extending `VoiceRecvClient` with video send capability:

```python
from discord_video_stream.compat.voice_recv import VoiceSendRecvClient

vc = await channel.connect(cls=VoiceSendRecvClient)
# vc.listen(sink)  -- receive audio (inherited from VoiceRecvClient)
# vc.send_video_packet(packet, ip, port)  -- send video RTP
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed technical documentation covering protocol implementation, data flow, and subsystem design.

## Module Inventory

### discord.ext.voice_recv

- `__init__.py` -- Package exports, applies DM voice patch
- `voice_client.py` -- VoiceRecvClient extending discord.VoiceClient
- `reader.py` -- AudioReader, PacketDecryptor, SpeakingTimer, UDPKeepAlive
- `sinks.py` -- AudioSink hierarchy (AudioSink, BasicSink, WaveSink, FFmpegSink, etc.)
- `router.py` -- PacketRouter, SinkEventRouter (threaded dispatch)
- `rtp.py` -- RTPPacket, RTCPPacket, FakePacket, SilencePacket parsing
- `opus.py` -- VoiceData container, PacketDecoder with jitter buffering
- `buffer.py` -- HeapJitterBuffer for packet reordering
- `gateway.py` -- Voice WebSocket opcode handling (hook function)
- `enums.py` -- VoiceFlags, VoicePlatform
- `types.py` -- TypedDicts for voice/video payloads
- `utils.py` -- gap_wrapped, add_wrapped, Bidict, LoopTimer, MultiDataEvent
- `silence.py` -- SilenceGenerator for gap filling
- `video.py` -- VoiceVideoStreams, VideoStreamInfo, VideoStreamResolution
- `patches.py` -- Context.voice_client patch for DM/group voice support
- `extras/` -- Optional speechrecognition and localplayback modules

### discord_video_stream

- `__init__.py` -- Package exports
- `streamer.py` -- VideoStreamer main API (orchestrator)
- `stream_connection.py` -- StreamConnection managing Go Live voice WebSocket
- `voice_send.py` -- VideoSender, AudioSender for RTP packet sending
- `rtp/serialize.py` -- RTP header construction, RTCP Sender Report builder
- `rtp/h264.py` -- H.264 NALU splitting, FU-A fragmentation, H264Packetizer
- `rtp/crypto.py` -- TransportEncryptor (AEAD encrypt, mirrors voice-recv's decrypt)
- `media/ffmpeg.py` -- FFmpegProcess subprocess management, StreamOptions
- `media/demux.py` -- NUT container demuxing via PyAV
- `media/pacer.py` -- FramePacer for timing and A/V synchronization
- `protocol/types.py` -- CodecConfig, stream key generation, gateway opcodes
- `protocol/vui.py` -- SPS VUI rewriter (BitstreamReader, BitstreamWriter)
- `compat/voice_recv.py` -- VoiceSendRecvClient for bidirectional voice/video

## License

MIT License. Copyright (c) 2015-present Imayhaveborkedit.
