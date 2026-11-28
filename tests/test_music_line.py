"""Cantinero's own music player (RESIDENT_PLAN.md R-4–R-7): the hub on the server, the
resident's relay, the queue expansion and the player's rules. No GStreamer, no network.

    .venv/bin/python -m pytest -q tests/test_music_line.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from services import music_hub
from services.skills import orpheus_media as om

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))
import musiccore as core  # noqa: E402


# --- the hub ------------------------------------------------------------------------------------

class FakeSocket:
    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.out: list[dict] = []
        self.closed = None

    async def recv(self):
        msg = await self.inbox.get()
        if msg is None:
            raise ConnectionError("closed")
        return msg

    async def send(self, text):
        self.out.append(json.loads(text))

    async def close(self, code, reason):
        self.closed = code
        await self.inbox.put(None)


def test_hub_sends_commands_and_waits_for_the_players_ack():
    async def go():
        hub, sock = music_hub.Hub(), FakeSocket()
        serving = asyncio.ensure_future(hub.serve("r1", "u1", sock.recv, sock.send, sock.close))
        await asyncio.sleep(0)
        assert hub.online("r1")

        async def player():                       # acks whatever it's sent, then reports state
            while not sock.out:
                await asyncio.sleep(0.01)
            cmd = sock.out[-1]
            await sock.inbox.put(json.dumps({"type": "ack", "id": cmd["id"], "ok": False, "error": "nothing_playing"}))
            await sock.inbox.put(json.dumps({"type": "state", "status": "idle", "volume": 70}))
        asyncio.ensure_future(player())
        ack = await hub.send("r1", {"type": "pause"})
        await asyncio.sleep(0.05)
        assert ack == {"ok": False, "error": "nothing_playing"} and sock.out[0]["type"] == "pause"
        assert hub.state("r1") == {"status": "idle", "volume": 70}
        with pytest.raises(music_hub.Timeout):
            await hub.send("r1", {"type": "next"}, wait=0.05)
        await sock.inbox.put(None)                # the resident goes away
        await asyncio.sleep(0.01)
        assert not hub.online("r1") and hub.state("r1") is None
        with pytest.raises(music_hub.Offline):
            await hub.send("r1", {"type": "pause"})
        serving.cancel()
    asyncio.run(go())


def test_a_reconnect_replaces_the_old_line():
    async def go():
        hub, old, new = music_hub.Hub(), FakeSocket(), FakeSocket()
        a = asyncio.ensure_future(hub.serve("r1", "u1", old.recv, old.send, old.close))
        await asyncio.sleep(0)
        b = asyncio.ensure_future(hub.serve("r1", "u1", new.recv, new.send, new.close))
        await asyncio.sleep(0.01)
        assert old.closed == 1001 and hub.online("r1")
        asyncio.ensure_future(hub.send("r1", {"type": "pause"}, wait=0.2))
        await asyncio.sleep(0.01)
        assert new.out and not old.out           # commands go to the newest line
        a.cancel(); b.cancel()
    asyncio.run(go())


# --- the resident's relay -------------------------------------------------------------------------

def test_relay_passes_commands_down_acks_up_and_ducks_locally(monkeypatch):
    from resident.relay import Relay

    async def go():
        relay, up = Relay(), []

        class Up:
            async def send(self, text):
                up.append(json.loads(text))
        relay.up = Up()
        await relay._from_server(json.dumps({"type": "pause", "id": "c1"}))
        assert up == [{"type": "ack", "id": "c1", "ok": False, "error": "player_closed"}]     # no player yet
        to_player, inbox = [], asyncio.Queue()

        async def psend(text):
            to_player.append(json.loads(text))
        task = asyncio.ensure_future(relay.attach_player(psend, inbox.get))
        await asyncio.sleep(0)
        await relay._from_server(json.dumps({"type": "next", "id": "c2"}))
        await relay._from_server(json.dumps({"type": "ping", "id": "p"}))
        await relay.duck_local(True)
        assert to_player == [{"type": "next", "id": "c2"}, {"type": "duck", "level": 25}]
        await inbox.put(json.dumps({"type": "ack", "id": "c2", "ok": True}))
        await inbox.put(json.dumps({"type": "state", "status": "playing"}))
        await asyncio.sleep(0.01)
        assert {"type": "pong", "id": "p"} in up and {"type": "ack", "id": "c2", "ok": True} in up
        assert relay.player_state["status"] == "playing"
        task.cancel()
    asyncio.run(go())


# --- expanding a play --------------------------------------------------------------------------

def test_expand_an_album_an_artist_a_track_and_a_playlist(monkeypatch):
    album = {"id": 10, "kind": "album", "title": "Moving Pictures", "album": {"album_artist": {"title": "Rush"}}}
    tracks = [{"id": 12, "kind": "track", "title": "Red Barchetta", "playable": True, "disc_number": 1, "track_number": 2},
              {"id": 11, "kind": "track", "title": "Tom Sawyer", "playable": True, "disc_number": 1, "track_number": 1,
               "artwork_id": 5, "duration_ms": 276000},
              {"id": 13, "kind": "track", "title": "Gone", "playable": False}]
    calls = []

    async def req(method, url, token="", **kw):
        path = url.split("http://o", 1)[1]
        calls.append(path)
        return {"/api/v1/items/10": album, "/api/v1/items/10/children": {"items": tracks},
                "/api/v1/items/1": {"id": 1, "kind": "artist", "title": "Rush"},
                "/api/v1/items/1/children": {"items": [album]},
                "/api/v1/items/11": {"id": 11, "kind": "track", "title": "Tom Sawyer", "playable": True,
                                     "parent": {"id": 10, "title": "Moving Pictures"}},
                "/api/v1/playlists/pl": {"entries": [
                    {"track": {"id": 11, "title": "Tom Sawyer", "playable": True}, "artist": "Rush", "album": "MP"},
                    {"track": None, "missing": True, "title": "Lost"}]}}[path]
    monkeypatch.setattr(om, "_request", req)
    got = asyncio.run(om.expand("http://o", "t", item_id=10))
    assert [e["title"] for e in got] == ["Tom Sawyer", "Red Barchetta"]                 # in order, playable only
    assert got[0] == {"id": 11, "identity_key": "", "title": "Tom Sawyer", "artist": "Rush",
                      "album": "Moving Pictures", "artwork_id": 5, "duration_ms": 276000}
    assert [e["title"] for e in asyncio.run(om.expand("http://o", "t", item_id=1))] == ["Tom Sawyer", "Red Barchetta"]
    one = asyncio.run(om.expand("http://o", "t", item_id=11))
    assert one[0]["artist"] == "Rush" and one[0]["album"] == "Moving Pictures"
    pl = asyncio.run(om.expand("http://o", "t", playlist_id="pl"))
    assert [(e["id"], e["artist"]) for e in pl] == [(11, "Rush")]                       # the missing entry skipped


# --- the player's rules ----------------------------------------------------------------------------

Q = [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}, {"id": 3, "title": "c"}]


def test_commands():
    q, v = core.Queue(), core.Volume()
    assert core.reduce({"type": "pause"}, q, v, 0, False).error == "nothing_playing"
    assert core.reduce({"type": "play", "queue": Q, "start_index": 1}, q, v, 0, False).kind == "load"
    assert q.current["id"] == 2
    assert core.reduce({"type": "seek", "position_ms": 90500}, q, v, 0, True).seconds == 90.5
    for bad in ({"type": "play", "queue": []}, {"type": "play", "queue": [{"id": "x"}]},
                {"type": "play", "queue": Q, "start_index": 3}, {"type": "seek", "position_ms": -1},
                {"type": "volume", "level": 101}, {"type": "dance"}):
        assert core.reduce(bad, q, v, 0, True).kind == "refuse", bad
    assert q.current["id"] == 2                                   # a refusal changes nothing


def test_volume_and_duck():
    v = core.Volume()
    core.reduce({"type": "volume", "level": 80}, core.Queue(), v, 0, False)
    core.reduce({"type": "duck", "level": 25}, core.Queue(), v, 0, False)
    assert abs(v.effective - 0.2) < 1e-9 and v.ducked
    v.set(40)                                                     # the listener's level moves under a duck
    assert abs(v.effective - 0.1) < 1e-9
    v.unduck()
    assert v.effective == 0.4
    v.duck(25, now=100)
    assert not v.expired(159) and v.expired(160) and not v.ducked  # a forgotten duck ends itself


def test_queue_moves():
    q = core.Queue()
    q.load(Q)
    assert q.advance() and q.advance() and not q.advance() and q.current["id"] == 3
    assert not q.back(position_s=10)                              # well into a track: restart it
    assert q.back(position_s=1) and q.current["id"] == 2          # at its start: the one before


def test_state_message_and_throttle():
    q, v = core.Queue(), core.Volume()
    q.load(Q)
    m = core.state_message("playing", q, v, 12.5, 200)
    assert m["now"] == {"id": 1, "title": "a", "artist": "", "album": ""}
    assert [e["id"] for e in m["upcoming"]] == [2, 3] and m["length"] == 3 and m["position_ms"] == 12500
    assert core.state_message("idle", core.Queue(), v, 0, 0)["now"] is None
    t = core.StateThrottle()
    assert t.due(m, 0) and not t.due(core.state_message("playing", q, v, 13, 200), 1)
    assert t.due(core.state_message("playing", q, v, 18, 200), 5)
    assert t.due(core.state_message("paused", q, v, 18, 200), 5.1)


def test_the_capability_report_asks_for_everything_direct():
    caps = core.CAPABILITIES
    assert caps["engine"] == "gstreamer" and {"mp3", "flac", "mp4", "ogg"} <= set(caps["containers"])
    assert {"mp3", "flac", "aac", "opus", "vorbis", "alac"} <= {a["codec"] for a in caps["audio"]}
