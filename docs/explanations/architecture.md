# Architecture and design

This extension implements voice receive without monkey-patching by providing a custom `VoiceProtocol` client: `voice_recv.VoiceRecvClient`.

Key components:
- `VoiceRecvClient`: voice client that enables receive, dispatches events, and orchestrates sinks
- `AudioSink` hierarchy: mirrors `AudioSource`, but for inbound audio
  - `BasicSink`, `WaveSink`, `FFmpegSink`, `PCMVolumeTransformer`, `ConditionalFilter`, `SilenceGeneratorSink` (WIP)
- RTP/RTCP parsing and router:
  - Modules: `rtp.py`, `router.py`, `reader.py`, `gateway.py`, `voice_client.py`
- Utility and types:
  - `types.py`, `utils.py`, `enums.py`, `opus.py`, `silence.py`, `video.py`
- Extras:
  - `extras.speechrecognition`, `extras.localplayback`

## Event model

New websocket events and sink-only virtual events provide context needed for voice receive:

- Connection lifecycle:
  - `on_voice_member_connect`, `on_voice_member_disconnect`
- Speaking modes and states:
  - `on_voice_member_speaking_state` (mode changes and initial SSRC mapping)
  - Virtual sink events synthesized from packet activity:
    - `on_voice_member_speaking_start`
    - `on_voice_member_speaking_stop`
- RTCP and video toggles:
  - `on_rtcp_packet` (sink-only)
  - `on_voice_member_video`
- Flags and platform:
  - `on_voice_member_flags`
  - `on_voice_member_platform`

## Cross-references

- See discord.py voice client and protocol: `discordpy:discord.VoiceClient`, `discordpy:discord.VoiceProtocol`
- See discord.py-self docs for user-account specifics: {dpyself}
- Discord Voice docs for protocol details: https://discord.com/developers/docs/topics/voice-connections
- Discord Gateway docs for intents and voice events: https://discord.com/developers/docs/topics/gateway

## Source layout

The implementation lives under `discord/ext/voice_recv/`. Notable entry points:

- `__init__.py` aggregates public symbols and types
- `voice_client.py` implements the receive-capable client
- `sinks.py` defines built-in sinks and helpers
- `router.py`/`reader.py` handle RTP/RTCP demux and packet flow

Use the API reference to locate classes and functions quickly; each item links back to source via the “View Source” button (enabled via `sphinx.ext.viewcode`).