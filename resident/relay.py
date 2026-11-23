"""The resident's music relay (RESIDENT_PLAN.md R-7).

- **Up:** one WebSocket to the Cantinero server (``/resident/connect``, the paired token, pinned
  TLS), reconnecting 1 s -> 30 s.
- **Down:** the player in the desktop shell connects to ``ws://127.0.0.1:<port>/player/connect``
  with the local player key from ``resident.json``.

Commands from the server go to the player and its acks go back. The player's state goes up as
it changes. With no player connected, a command is acked ``player_closed``.

Ducking never leaves this machine: the page's duck goes straight to the player (``duck_local``).
"""
from __future__ import annotations

import asyncio
import json
import ssl

from resident import config, mdns

RECONNECT_FIRST, RECONNECT_MAX = 1.0, 30.0
DUCK_LEVEL = 25


class Relay:
    def __init__(self) -> None:
        self.up = None                      # websockets connection to the server
        self.player = None                  # (send coroutine function) of the local player
        self.player_state: dict = {}
        self.connected = False
        self._task: asyncio.Task | None = None

    # -- the local player ------------------------------------------------------------------

    async def attach_player(self, send, recv) -> None:
        """Run the shell player's connection until it closes."""
        self.player = send
        try:
            while True:
                raw = await recv()
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if msg.get("type") == "state":
                    self.player_state = msg
                await self._up(msg)          # acks and state go to the server as they are
        finally:
            if self.player is send:
                self.player = None
                self.player_state = {}
                await self._up({"type": "state", "status": "off"})

    async def to_player(self, msg: dict) -> bool:
        if self.player is None:
            return False
        try:
            await self.player(json.dumps(msg))
            return True
        except Exception:  # noqa: BLE001
            return False

    async def duck_local(self, on: bool) -> None:
        await self.to_player({"type": "duck", "level": DUCK_LEVEL} if on else {"type": "unduck"})

    # -- the server ------------------------------------------------------------------------

    async def _up(self, msg: dict) -> None:
        if self.up is not None:
            try:
                await self.up.send(json.dumps(msg))
            except Exception:  # noqa: BLE001
                pass

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        import websockets

        failures = 0
        while True:
            cfg = config.load()
            if not config.paired(cfg):
                await asyncio.sleep(10)
                continue
            addr = mdns.resolve(cfg["host"])
            if cfg.get("insecure_http"):
                url, ctx = f"ws://{addr}:{cfg.get('http_port', 7867)}/resident/connect", None
            else:
                url = f"wss://{addr}:{cfg.get('https_port', 7868)}/resident/connect"
                ctx = config.tls_context(cfg["ca_pem"])
            try:
                async with websockets.connect(url, ssl=ctx, open_timeout=10, ping_interval=None,
                                              additional_headers={"Authorization": f"Bearer {cfg['token']}"}) as ws:
                    self.up, self.connected, failures = ws, True, 0
                    await ws.send(json.dumps({"type": "hello", "version": "resident"}))
                    if self.player_state:
                        await ws.send(json.dumps(self.player_state))
                    async for raw in ws:
                        await self._from_server(raw)
            except (OSError, ssl.SSLError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                if getattr(e, "code", None) == 1008 or "1008" in str(e):
                    print("[relay] the server refused this resident (removed?)", flush=True)
            finally:
                self.up, self.connected = None, False
            await asyncio.sleep(min(RECONNECT_MAX, RECONNECT_FIRST * 2 ** failures))
            failures += 1

    async def _from_server(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        if msg.get("type") == "ping":
            await self._up({"type": "pong", "id": msg.get("id")})
            return
        if not await self.to_player(msg):
            await self._up({"type": "ack", "id": msg.get("id"), "ok": False, "error": "player_closed"})


RELAY = Relay()
