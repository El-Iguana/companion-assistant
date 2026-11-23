"""The resident: Cantinero on a machine with speakers (RESIDENT_PLAN.md).

A small local service that serves the desktop page on 127.0.0.1, does STT and TTS here,
and forwards everything else to the Cantinero server (amd-halo) with this resident's
paired token, over HTTPS with the server's CA pinned. ``python -m resident --help``.
"""
