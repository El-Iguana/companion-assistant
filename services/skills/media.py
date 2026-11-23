"""The DJ: Mira's music skill, on the Orpheus media server (SKILLS_PLAN.md, Skill 2).

What she can do depends on where the pairing stands, so the model is only offered the
tools that can work (and only pays prompt tokens for those):

  unpaired / pairing pending  ->  media_connect
  paired                      ->  media_play, media_control, media_find, media_status

Playback is Orpheus Phase 5 (remote control): Orpheus sends the command to a player (the
one built into a Cantinero resident) and answers with the player's ack. ``media_play`` takes
what the user said and plays it when the match is clear, so "play Sigur Rós" is one tool
pass; when it isn't, the choices are kept for five minutes (``pending.json``) and the next
turn's prompt names them. Where it plays (RESIDENT_PLAN.md R-6): a named player, else
the resident's own (a voice request from the speaker machine), else the user's default
player (Settings → Music), else the only one online.

"Pause", "next song", "turn it down" never reach the model: ``fast_command`` recognises
them and ``fast_control`` does them in well under a second (a tool turn costs ~25 s on the
local model). ``duck`` lowers the music while she speaks (pantheon_web/media.py).

Pairing needs a human: Orpheus gives a 6-digit code and someone approves it on the server
(``orpheus pair approve <code>``), so a prompt-injected card can start a request but never
finish one.
"""
from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timezone

from services.skills import SkillContext, Skill, Tool, error, register
from services.skills import orpheus_media as om

# Music only: Orpheus also holds films and shows, and an unrestricted search for a band it
# doesn't have came back with movies ("sigur ros" -> Shazam!). Playlists are the user's
# own and shared ones (Orpheus Phase 5 AD-5; the device's profile decides "own").
KINDS = ["artist", "album", "track", "playlist"]
# Below this a candidate only sounds a little alike ("sigur ros" -> "Sugar Mama", 0.34;
# "tom sawyer by rush" -> Tom Petty, 0.42), while real hits scored 0.54-1.0 on the live
# library (2026-11-23). Under it she says it isn't there rather than offer a lookalike.
MIN_SCORE = 0.5
PENDING_TTL_S = 300          # a choice she offered is remembered this long
VOLUME_STEP = 15             # "turn it up" / "down", in percent
DUCK_LEVEL = 25              # the music under her voice, % of the listener's volume


# --- choosing a player (RESIDENT_PLAN.md R-6: the players are the user's residents) ----------

def _residents(user_id: str) -> list[dict]:
    from services import residents
    from services.music_hub import HUB

    return [dict(r, online=HUB.online(r["id"]), state=HUB.state(r["id"]) or {})
            for r in residents.list_for(user_id)]


async def _target(ctx: SkillContext, st: dict, name: str = "") -> dict:
    """The resident a command goes to: the one named; else, for a voice request, the resident
    it came from; else the user's default; else the only one connected; else she asks. A
    chosen resident that isn't connected is an error, never a quiet switch to another room."""
    players = _residents(ctx.user_id)
    if not players:
        raise om.OrpheusError("NO_PLAYERS", "No Cantinero resident is paired yet, so there's nowhere to "
                              "play music. Pair one in Settings → Residents.")
    if name:
        named = [d for d in players if name.lower() in str(d.get("name") or "").lower()]
        if not named:
            raise om.OrpheusError("NO_SUCH_PLAYER", f"No player is called {name!r}. The players are: "
                                  + ", ".join(str(d.get("name")) for d in players) + ".")
        on = [d for d in named if d["online"]]
        if not on:
            raise om.OrpheusError("PLAYER_OFFLINE", f"{named[0].get('name')} isn't connected.")
        return on[0]
    for want in (ctx.player_id, st.get("default_player_id")):
        dev = next((d for d in players if d["id"] == want), None) if want else None
        if dev is not None:
            if not dev["online"]:
                raise om.OrpheusError("PLAYER_OFFLINE", f"The music player on {dev.get('name')} isn't "
                                      "connected. Is the Cantinero resident running there?")
            return dev
    online = [d for d in players if d["online"]]
    if len(online) == 1:
        return online[0]
    if len(online) > 1:
        raise om.OrpheusError("WHICH_PLAYER", "Several players are on: "
                              + ", ".join(str(d.get("name")) for d in online) + ". Ask which one.")
    raise om.OrpheusError("PLAYER_OFFLINE", "No music player is connected. Is the Cantinero "
                          "resident running on the speaker machine?")


async def _send(resident_id: str, cmd: dict) -> None:
    """One command to a resident's player; raises OrpheusError with the player's refusal."""
    from services.music_hub import HUB, Offline, Timeout

    try:
        ack = await HUB.send(resident_id, cmd)
    except Offline:
        raise om.OrpheusError("PLAYER_OFFLINE", "The music player disconnected.") from None
    except Timeout:
        raise om.OrpheusError("PLAYER_TIMEOUT", "The music player didn't answer.") from None
    if not ack.get("ok"):
        code = (ack.get("error") or "refused").upper()
        raise om.OrpheusError(code, code.lower().replace("_", " "))


def _refusal(e: om.OrpheusError) -> dict:
    if e.code == "NOTHING_PLAYING":
        return error(e.code, "Nothing is playing.")
    return error(e.code, str(e))


# --- the pending choice ------------------------------------------------------------------

def _pending_path(ctx: SkillContext):
    return ctx.root / "media" / "pending.json"


def _load_pending(ctx: SkillContext) -> list[dict]:
    try:
        p = json.loads(_pending_path(ctx).read_text())
    except (OSError, ValueError):
        return []
    return p.get("candidates") or [] if time.time() - float(p.get("at") or 0) < PENDING_TTL_S else []


def _save_pending(ctx: SkillContext, cands: list[dict] | None, query: str = "") -> None:
    path = _pending_path(ctx)
    try:
        if not cands:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": time.time(), "candidates": cands, "query": query.strip().lower()}))
    except OSError:
        pass


def _pending_query(ctx: SkillContext) -> str:
    try:
        p = json.loads(_pending_path(ctx).read_text())
    except (OSError, ValueError):
        return ""
    return p.get("query", "") if time.time() - float(p.get("at") or 0) < PENDING_TTL_S else ""


def _choices(cands: list[dict], ask: str) -> dict:
    """An ambiguous play, worded so the model can't mistake it (it once repeated the same
    call three times, then told the user the player wasn't running)."""
    return {"ok": True, "played": False, "nothing_is_wrong": True,
            "say": ask or "Ask which one they meant.",
            "choices": [{"id": c["id"], "name": c["name"]} for c in cands],
            "next": "Ask the user which one, and stop. When they answer, call media_play with that "
                    "choice's id. Don't call media_play with the same query again."}


_RECENT_PLAY_S = 90


def _remember_play(ctx: SkillContext, what: str, player: str) -> None:
    try:
        p = ctx.root / "media" / "last_play.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"at": time.time(), "what": what.strip().lower(), "player": player}))
    except OSError:
        pass


def _just_played(ctx: SkillContext, what: str, player: str) -> bool:
    try:
        p = json.loads((ctx.root / "media" / "last_play.json").read_text())
    except (OSError, ValueError):
        return False
    return (p.get("what") == what.strip().lower() and p.get("player") == player
            and time.time() - float(p.get("at") or 0) < _RECENT_PLAY_S)


def _spoken(name: str) -> str:
    """A candidate's display ("Crash — Dave Matthews Band (1996)") as said aloud."""
    name = re.sub(r"\s*\(\d{4}\)$", "", name)
    title, _, rest = name.partition(" — ")
    if rest == "playlist":
        return f"the {title} playlist"                     # "the Road Trip playlist"
    if rest.endswith("'s playlist"):
        return f"{rest[:-len(' playlist')]} {title} playlist"   # "Sam's Road Trip playlist"
    return name.replace(" — ", " by ")


# --- searching -----------------------------------------------------------------------------

async def _search(st: dict, query: str, kinds: list[str] | None) -> tuple[list[dict], dict]:
    """Strong candidates as the model sees them, and the server's whole answer."""
    out = await om.resolve(st["url"], st["token"], query, kinds or KINDS)
    found = out.get("candidates") or []
    strong = [c for c in found if float(c.get("score") or 0) >= MIN_SCORE and c.get("kind") in KINDS]
    cands = [{"id": str(c.get("playlist_id") or c.get("item_id")), "kind": c.get("kind"),
              "name": c.get("display") or c.get("title"), "playable": c.get("playable", True)}
             for c in strong[:5]]
    return cands, {"ask": out.get("disambiguation_prompt") if len(strong) == len(found) else "",
                   "ambiguous": bool(out.get("needs_disambiguation"))}


async def _connect(ctx: SkillContext, args: dict) -> dict:
    state = om.load(ctx.root)
    if om.paired(state):
        return {"ok": True, "status": "paired", "server": state.get("server_name", "Orpheus")}
    try:
        base, info = await om.discover((state.get("pairing") or {}).get("url") or state.get("url", ""))
        pending = state.get("pairing") or {}
        if pending.get("pairing_id") and pending.get("server_id") == info.get("server_id"):
            got = await om.poll_pairing(base, pending["pairing_id"])
            if got.get("token") and got.get("device_id"):
                om.save(ctx.root, {"server_id": info["server_id"], "server_name": info.get("name", "Orpheus"),
                                   "url": base, "device_id": got["device_id"], "token": got["token"],
                                   "paired_at": datetime.now(timezone.utc).isoformat()})
                return {"ok": True, "status": "paired", "server": info.get("name", "Orpheus")}
            if str(got.get("status", "pending")) in ("pending", "waiting", ""):
                return {"ok": True, "status": "waiting_for_approval", "code": pending.get("code"),
                        "how": f"approve it on the server: orpheus pair approve {pending.get('code')}"}
        started = await om.start_pairing(base)
    except om.OrpheusError as e:
        if e.code == "NOT_FOUND" and state.get("pairing"):
            state.pop("pairing", None)              # expired or denied: start over next time
            om.save(ctx.root, state)
            return error("PAIRING_EXPIRED", "That pairing code expired. Ask me to connect again.")
        return error(e.code, str(e))
    state["pairing"] = {"pairing_id": started.get("pairing_id"), "code": started.get("code"),
                        "expires_at": started.get("expires_at"), "server_id": info.get("server_id"),
                        "url": base}
    om.save(ctx.root, state)
    return {"ok": True, "status": "waiting_for_approval", "code": started.get("code"),
            "how": f"approve it on the server: orpheus pair approve {started.get('code')}"}


def _paired_state(ctx: SkillContext) -> dict | None:
    st = om.load(ctx.root)
    return st if om.paired(st) else None


NOT_PAIRED = error("NOT_PAIRED", "Not connected to the music server yet.")


async def _find(ctx: SkillContext, args: dict) -> dict:
    st = _paired_state(ctx)
    if not st:
        return NOT_PAIRED
    try:
        cands, how = await _search(st, args["query"], args.get("kinds"))
    except om.OrpheusError as e:
        return error(e.code, str(e))
    if not cands:
        return error("MEDIA_NOT_FOUND", f"Nothing in the music library matches {args['query']!r}.")
    res = {"ok": True, "candidates": cands, "ambiguous": how["ambiguous"]}
    if how["ask"]:
        res["ask"] = how["ask"]
    _save_pending(ctx, cands if how["ambiguous"] else None)
    return res


async def _play(ctx: SkillContext, args: dict) -> dict:
    st = _paired_state(ctx)
    if not st:
        return NOT_PAIRED
    choice_id, query = args.get("id", ""), args.get("query", "")
    if not choice_id and not query:
        return error("BAD_ARGUMENTS", "Give the query the user said, or the id of a choice.")
    try:
        if choice_id:
            known = {c["id"]: c for c in _load_pending(ctx)}
            pick = known.get(choice_id) or {"id": choice_id, "kind": "playlist" if not choice_id.isdigit()
                                            else "", "name": ""}
        else:
            pending = _load_pending(ctx)
            if pending and _pending_query(ctx) == query.strip().lower():
                # The same question again in the same turn: answer with the choices, not a search.
                return dict(_choices(pending, ""), ok=False, error="ASK_FIRST")
            cands, how = await _search(st, query, args.get("kinds"))
            if not cands:
                return error("MEDIA_NOT_FOUND", f"Nothing in the music library matches {query!r}.")
            if how["ambiguous"] and len(cands) > 1:
                _save_pending(ctx, cands[:3], query)
                return _choices(cands[:3], how["ask"])
            pick = cands[0]
        dev = await _target(ctx, st, args.get("player", ""))
        if _just_played(ctx, query or choice_id, dev["id"]):
            # The same play to the same player again in the same breath: it's on; don't restart it.
            return {"ok": True, "played": True, "already_playing": True, "player": dev.get("name"),
                    "final": "It's playing."}
        queue = await om.expand(st["url"], st["token"], item_id=int(pick["id"]) if pick["id"].isdigit() else None,
                                playlist_id="" if pick["id"].isdigit() else pick["id"])
        if not queue:
            return error("NOTHING_TO_PLAY", "Nothing in that can be played (its files may be missing).")
        if args.get("shuffle"):
            random.shuffle(queue)
        await _send(dev["id"], {"type": "play", "queue": queue, "start_index": 0})
    except om.OrpheusError as e:
        return _refusal(e)
    _save_pending(ctx, None)
    _remember_play(ctx, query or choice_id, dev["id"])
    name = pick.get("name") or "it"
    return {"ok": True, "played": True, "playing": name, "player": dev.get("name"),
            "final": f"Playing {_spoken(name)} on {dev.get('name')}."}


async def _control(ctx: SkillContext, args: dict) -> dict:
    st = _paired_state(ctx)
    if not st:
        return NOT_PAIRED
    action = args["action"]
    body: dict = {"type": action}
    if action == "seek":
        if "position_s" not in args:
            return error("BAD_ARGUMENTS", "seek needs position_s")
        body["position_ms"] = int(args["position_s"] * 1000)
    if action == "volume":
        if "level" not in args:
            return error("BAD_ARGUMENTS", "volume needs level (0-100)")
        body["level"] = int(args["level"])
    try:
        dev = await _target(ctx, st, args.get("player", ""))
        await _send(dev["id"], body)
    except om.OrpheusError as e:
        return _refusal(e)
    said = {"pause": "Paused.", "resume": "Playing.", "stop": "Stopped.", "next": "Skipping.",
            "previous": "Going back.", "seek": "Done.", "volume": f"Volume {body.get('level')}."}
    return {"ok": True, "done": action, "player": dev.get("name"), "final": said.get(action, "Done.")}


async def _status(ctx: SkillContext, args: dict) -> dict:
    def song(e):
        return (e.get("title") or "") + (" by " + e["artist"] if e.get("artist") else "")
    out = []
    for d in _residents(ctx.user_id):
        st = d["state"] if d["online"] else {}
        now = st.get("now")
        out.append({"name": d.get("name"), "on": d["online"], "status": st.get("status") or "off",
                    "playing": song(now) if now else None,
                    "up_next": [song(e) for e in (st.get("upcoming") or [])[:3]],
                    "volume": st.get("volume"),
                    "track": (st.get("index") or 0) + 1 if st.get("length") else None, "of": st.get("length")})
    return {"ok": True, "players": out}


# --- the fast path -------------------------------------------------------------------------

_POLITE_HEAD = re.compile(r"^(?:(?:hey|ok|okay|please|can you|could you|would you|will you|just)\b[ ,]*)+")
_POLITE_TAIL = re.compile(r"(?:[ ,]*\b(?:please|thanks|thank you|for me|now|right now))+$")
_IT = r"(?: (?:the|this|that))?(?: (?:music|song|track|tune|playback|it|one))?"
_FAST = [
    (re.compile(rf"^(?:pause|pause{_IT}|stop the (?:music|song|track|playback)|stop playing)$"), {"type": "pause"}),
    (re.compile(rf"^(?:resume|unpause|keep playing|resume{_IT}|unpause{_IT}|continue the (?:music|song|track|playback)|"
                rf"start{_IT} again|play it again)$"), {"type": "resume"}),
    (re.compile(rf"^(?:skip|next|skip{_IT}|next{_IT}|play the next(?: (?:song|track|one))?)$"), {"type": "next"}),
    (re.compile(rf"^(?:previous|go back|previous{_IT}|go back a (?:song|track)|"
                rf"(?:play )?the (?:previous|last) (?:song|track|one))$"), {"type": "previous"}),
    (re.compile(r"^(?:turn (?:it|the music|the volume|this) up|turn up the (?:music|volume)|louder|"
                r"volume up|raise the volume|a bit louder|crank it up)$"), {"type": "volume", "step": VOLUME_STEP}),
    (re.compile(r"^(?:turn (?:it|the music|the volume|this) down|turn down the (?:music|volume)|quieter|"
                r"softer|volume down|lower the volume|a bit quieter)$"), {"type": "volume", "step": -VOLUME_STEP}),
]
_SET_VOLUME = re.compile(r"^(?:set )?(?:the )?volume (?:to |at )?(\d{1,3})(?: ?%| percent)?$")


def fast_command(text: str) -> dict | None:
    """A music command said plainly enough to do without the model, else None. Whole
    utterances only ("pause the music"), so "I paused the movie earlier" goes to her."""
    t = re.sub(r"[^\w%' ]+", " ", (text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    if not t or len(t) > 60:
        return None
    t = _POLITE_TAIL.sub("", _POLITE_HEAD.sub("", t)).strip()
    m = _SET_VOLUME.match(t)
    if m:
        return {"type": "volume", "level": min(100, int(m.group(1)))}
    for pattern, cmd in _FAST:
        if pattern.match(t):
            return dict(cmd)
    return None


_SAID = {"pause": "Paused.", "resume": "Playing.", "next": "Skipping.", "previous": "Going back."}


async def fast_control(ctx: SkillContext, cmd: dict) -> str:
    """Do a fast_command; what she says back, never an exception."""
    st = _paired_state(ctx)
    if not st:
        return "I'm not connected to the music server yet."
    body = {"type": cmd["type"]}
    try:
        dev = await _target(ctx, st)
        if cmd["type"] == "volume":
            level = cmd.get("level")
            if level is None:
                now = (dev.get("state") or {}).get("volume")
                level = max(0, min(100, int(now if now is not None else 100) + int(cmd["step"])))
            body["level"] = int(level)
        await _send(dev["id"], body)
    except om.OrpheusError as e:
        return {"NOT_PAIRED": "The music server doesn't know me any more; it needs pairing again.",
                "PLAYER_OFFLINE": "The music player isn't on.",
                "PLAYER_TIMEOUT": "The music player didn't answer.",
                "NO_PLAYERS": "There's no music player paired yet.",
                "NOTHING_PLAYING": "Nothing's playing.",
                "WHICH_PLAYER": "More than one player is on, so tell me which.",
                "SERVER_UNREACHABLE": "I can't reach the music server."}.get(e.code, "That didn't work.")
    return f"Volume {body['level']}." if cmd["type"] == "volume" else _SAID.get(cmd["type"], "Done.")


async def duck(user_id: str, root, on: bool) -> None:
    """The web chat's page speaking: lower the default resident's music under her voice. A
    resident's own page ducks its player locally and never comes here (R-7). Best effort."""
    st = om.load(root)
    target = st.get("default_player_id")
    if not target:
        return
    body = {"type": "duck", "level": DUCK_LEVEL} if on else {"type": "unduck"}
    try:
        await _send(target, body)
    except om.OrpheusError:
        pass


# --- Settings → Music ----------------------------------------------------------------------

async def players(user_id: str, root) -> dict:
    """The user's residents (the players) and which is the default, for Settings → Music."""
    st = om.load(root)
    return {"paired": om.paired(st), "server": st.get("server_name", "Orpheus"),
            "default_player_id": st.get("default_player_id", ""),
            "players": [{"id": d["id"], "name": d["name"], "online": d["online"]} for d in _residents(user_id)]}


def set_default_player(root, player_id: str, name: str = "") -> None:
    st = om.load(root)
    st["default_player_id"], st["default_player_name"] = player_id or "", name or ""
    st.pop("player_id", None)
    st.pop("player_name", None)
    om.save(root, st)


# --- tools ---------------------------------------------------------------------------------

CONNECT = Tool(
    "media_connect",
    "Connect to the user's music server (Orpheus). Use when the user asks to set up or connect "
    "the music, or when another music tool says it isn't connected. Read the code it returns to the user.",
    {"type": "object", "properties": {}}, _connect, status="Connecting to the music server…")
PLAY = Tool(
    "media_play",
    "Play music: an artist, album, track or playlist, as the user said it (query), or one of the "
    "choices you offered (id). Plays at once when the match is clear; otherwise returns choices to ask about.",
    {"type": "object", "properties": {
        "query": {"type": "string", "maxLength": 200, "description": "What to play, as the user said it"},
        "id": {"type": "string", "maxLength": 64, "description": "The id of a choice you offered"},
        "kinds": {"type": "array", "maxItems": 4, "items": {"type": "string", "enum": KINDS}},
        "shuffle": {"type": "boolean"},
        "player": {"type": "string", "maxLength": 60, "description": "Only if the user named a player"}}},
    _play, status="Finding the music…", timeout=40)
CONTROL = Tool(
    "media_control",
    "Control the music playing now: pause, resume, stop, next, previous, seek (position_s) or volume (level 0-100).",
    {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["pause", "resume", "stop", "next", "previous", "seek", "volume"]},
        "position_s": {"type": "integer", "minimum": 0, "maximum": 86400},
        "level": {"type": "integer", "minimum": 0, "maximum": 100},
        "player": {"type": "string", "maxLength": 60}},
     "required": ["action"]}, _control, status="Working the player…")
FIND = Tool(
    "media_find",
    "Search the music library without playing: to answer what's there.",
    {"type": "object", "properties": {
        "query": {"type": "string", "maxLength": 200, "description": "What to look for, as the user said it"},
        "kinds": {"type": "array", "maxItems": 4, "items": {"type": "string", "enum": KINDS}}},
     "required": ["query"]}, _find, status="Searching the library…")
STATUS = Tool(
    "media_status",
    "Which music players are open and what each is doing.",
    {"type": "object", "properties": {}}, _status, status="Checking the players…")


def _tools(ctx: SkillContext) -> list[Tool]:
    return [PLAY, CONTROL, FIND, STATUS] if om.paired(om.load(ctx.root)) else [CONNECT]


def _note(ctx: SkillContext) -> str:
    if not om.paired(om.load(ctx.root)):
        return ""
    note = ("Music: you can play the user's music and control it (media_play, media_control). "
            "To play something, call media_play with what they said; don't search or check status first.")
    # What is true now, so an old line in the chat ("the player isn't running") can't win.
    try:
        live = _residents(ctx.user_id)
    except Exception:  # noqa: BLE001
        live = []
    if live:
        def one(d):
            st = d["state"] or {}
            now = st.get("now") or {}
            what = st.get("status") or "idle"
            if what in ("playing", "paused") and now.get("title"):
                what += f" ({now['title']}" + (f" by {now['artist']}" if now.get("artist") else "") + ")"
            return f"{d['name']}: " + (what if d["online"] else "not connected")
        note += " The players right now: " + "; ".join(one(d) for d in live) + "."
    pending = _load_pending(ctx)
    if pending:
        note += (" You offered these and are waiting for the user's pick (call media_play with its id): "
                 + "; ".join(f"{i}) {c['name']} [id {c['id']}]" for i, c in enumerate(pending, 1)))
    return note


register(Skill("dj", "DJ", "Play music from your Orpheus library and control it by voice.",
               _tools, note=_note))
