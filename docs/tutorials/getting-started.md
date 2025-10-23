# Getting Started

This tutorial walks you through installing and using the extension to capture voice.

## Prerequisites

- Python 3.8+
- A Discord application and token
- A working installation of discord.py with voice support (`pynacl`) or discord.py-self with voice

Links:
- {dpy}
- {dpyself}
- Discord Voice docs: https://discord.com/developers/docs/topics/voice-connections

## Install

```bash
python -m pip install discord-ext-voice-recv
```

Optionally install extras:

```bash
# all extras
python -m pip install "discord-ext-voice-recv[extras]"
# speech recognition
python -m pip install "discord-ext-voice-recv[extras_speech]"
# local audio playback
python -m pip install "discord-ext-voice-recv[extras_local]"
```

## Minimal example

```python
import discord
from discord.ext import commands
from discord.ext import voice_recv

bot = commands.Bot(command_prefix="!")

@bot.command()
async def join(ctx: commands.Context):
    vc = await ctx.author.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
    sink = voice_recv.WaveSink("output.wav")  # alias: WavSink
    vc.listen(sink)

@bot.command()
async def leave(ctx: commands.Context):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()

bot.run("YOUR_TOKEN")
```

More examples: [examples/recv.py](../../examples/recv.py)

## Next steps

- Learn the event model and speaking state
- Explore built-in sinks and extras
- Compose sinks to build processing pipelines