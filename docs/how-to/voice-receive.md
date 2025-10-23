# Receive and process voice

Goal: capture voice from a channel, filter or transform it, and write to a destination sink.

## Steps

1) Connect with the custom voice client:

```python
from discord.ext import voice_recv
voice_client = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
```

2) Choose a sink:
- `WaveSink`/`WavSink`: write PCM to a WAV file
- `FFmpegSink`: convert/process with ffmpeg
- `PCMVolumeTransformer`: adjust volume
- `ConditionalFilter`, `UserFilter`, `TimedFilter`: filter packets
- `SilenceGeneratorSink`: fill gaps (WIP/buggy)

3) Start listening:

```python
sink = voice_recv.WaveSink("output.wav")
voice_client.listen(sink)
```

4) Stop when done:

```python
voice_client.stop_listening()   # or voice_client.stop() to stop both send/receive
```

## Handling speaking state

- `VoiceRecvClient.get_speaking(member)` → bool | None
- Sink-only virtual events:
  - `on_voice_member_speaking_start(member)`
  - `on_voice_member_speaking_stop(member)`

## New events you can subscribe to

```python
async def on_voice_member_connect(member: discord.Member)
async def on_voice_member_disconnect(member: discord.Member, ssrc: int | None)
async def on_voice_member_speaking_state(member: discord.Member, ssrc: int, state: SpeakingState | int)
async def on_voice_member_video(member: discord.Member, data: voice_recv.VoiceVideoStreams)
async def on_voice_member_flags(member: discord.Member, flags: voice_recv.VoiceFlags)
async def on_voice_member_platform(member: discord.Member, platform: voice_recv.VoicePlatform | None)
```

In sinks, you can also listen to RTCP and synthesized speaking events:

```python
class MySink(voice_recv.AudioSink):
    @voice_recv.AudioSink.listener()
    def on_rtcp_packet(self, packet: voice_recv.rtp.RTCPPacket, guild: discord.Guild):
        ...

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_start(self, member: discord.Member):
        ...
```

## Related references

- Intersphinx cross-reference to discord.py voice classes:
  - `discordpy:discord.VoiceClient`
  - `discordpy:discord.VoiceProtocol`
- Discord Developer Docs:
  - `discord_voice:voice-connections`
  - `discord_gateway:gateway-intents`