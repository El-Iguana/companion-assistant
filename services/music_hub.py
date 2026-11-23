"""The residents' command line (RESIDENT_PLAN.md R-7): one WebSocket per resident, held by the
resident (``/resident/connect``), commands down it with ids, acks and state back up.

In memory, one process: a restart drops every connection and the residents reconnect
(1 s -> 30 s). Nothing here is written to disk.

Resident -> server: ``hello {version}``, ``ack {id, ok, error?}``,
``state {status, index, position_ms, duration_ms, volume, ducked, queue: [{id, title, artist}]}``,
``pong``. Server -> resident: any command dict with an ``id``, and ``ping``.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field

PING_EVERY_S = 30
SILENCE_MAX_S = 90
ACK_WAIT_S = 6.0           # a play waits for its first sound on the resident (up to ~4 s)


class Offline(Exception):
    pass


class Timeout(Exception):
    pass


@dataclass
class Conn:
    resident_id: str
    user_id: str
    send: object                          # async (str) -> None
    close: object                         # async (code, reason) -> None
    state: dict = field(default_factory=dict)
    state_at: float = 0.0
    seen: float = field(default_factory=time.monotonic)
    waiting: dict = field(default_factory=dict)   # command id -> Future


class Hub:
    def __init__(self) -> None:
        self.conns: dict[str, Conn] = {}

    def online(self, resident_id: str) -> bool:
        return resident_id in self.conns

    def state(self, resident_id: str) -> dict | None:
        c = self.conns.get(resident_id)
        return dict(c.state) if c and c.state else None

    async def serve(self, resident_id: str, user_id: str, recv, send, close) -> None:
        """Run one resident's connection until it ends. ``recv()`` returns the next text
        message or raises when the socket closes."""
        old = self.conns.get(resident_id)
        if old is not None:                               # a reconnect replaces the old socket
            self._drop(old)
            try:
                await old.close(1001, "replaced by a newer connection")
            except Exception:  # noqa: BLE001
                pass
        conn = Conn(resident_id, user_id, send, close)
        self.conns[resident_id] = conn
        pinger = asyncio.ensure_future(self._ping(conn))
        try:
            while True:
                raw = await recv()
                conn.seen = time.monotonic()
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                kind = msg.get("type")
                if kind == "ack":
                    fut = conn.waiting.pop(str(msg.get("id") or ""), None)
                    if fut and not fut.done():
                        fut.set_result({"ok": bool(msg.get("ok")), "error": str(msg.get("error") or "")})
                elif kind == "state" and isinstance(msg, dict):
                    conn.state = {k: v for k, v in msg.items() if k != "type"}
                    conn.state_at = time.time()
        finally:
            pinger.cancel()
            if self.conns.get(resident_id) is conn:
                self._drop(conn)

    def _drop(self, conn: Conn) -> None:
        self.conns.pop(conn.resident_id, None)
        for fut in conn.waiting.values():
            if not fut.done():
                fut.set_exception(Offline())
        conn.waiting.clear()

    async def _ping(self, conn: Conn) -> None:
        while True:
            await asyncio.sleep(PING_EVERY_S)
            if time.monotonic() - conn.seen > SILENCE_MAX_S:
                self._drop(conn)
                try:
                    await conn.close(1008, "silent too long")
                except Exception:  # noqa: BLE001
                    pass
                return
            try:
                await conn.send(json.dumps({"type": "ping", "id": uuid.uuid4().hex[:8]}))
            except Exception:  # noqa: BLE001
                return

    async def send(self, resident_id: str, cmd: dict, wait: float = ACK_WAIT_S) -> dict:
        """Send one command and wait for its ack: {"ok", "error"}. Raises Offline or Timeout."""
        conn = self.conns.get(resident_id)
        if conn is None:
            raise Offline()
        cid = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        conn.waiting[cid] = fut
        try:
            await conn.send(json.dumps({**cmd, "id": cid}))
            return await asyncio.wait_for(fut, wait)
        except asyncio.TimeoutError:
            raise Timeout() from None
        except Offline:
            raise
        except Exception as e:  # noqa: BLE001 — a dead socket mid-send
            raise Offline() from e
        finally:
            conn.waiting.pop(cid, None)


HUB = Hub()
