"""Browser demo server. Must never import agent.pipeline or agent.audio_io
(both pull in sounddevice, which needs PortAudio and fails on a headless
Linux host)."""
