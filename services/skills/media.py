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


# --- choosing a player ---------------------------------------------------------------------

def _players(devs: list[dict]) -> list[dict]:
    return [d for d in devs if (d.get("role") or "player") != "controller"]


async def _target(ctx: SkillContext, st: dict, name: str = "") -> dict:
    """The player a command goes to (RESIDENT_PLAN.md R-6): the one named; else, for a
    resident's request, that resident's own player; else the user's default player; else
    the only one online; else she asks. A chosen player that is closed is an error, never
    a quiet switch to another room. Raises OrpheusError with a code she can talk about."""
    players = _players(await om.devices(st["url"], st["token"]))
    if name:
        named = [d for d in players if name.lower() in str(d.get("name") or "").lower()]
        if not named:
            raise om.OrpheusError("NO_SUCH_PLAYER", f"No player is called {name!r}. The players are: "
                                  + ", ".join(str(d.get("name")) for d in players) + ".")
        on = [d for d in named if d.get("online")]
        if not on:
            raise om.OrpheusError("PLAYER_OFFLINE", f"{named[0].get('name')} isn't connected.")
        return on[0]
    for want in (ctx.player_id, st.get("default_player_id")):
        if want:
            dev = next((d for d in players if d.get("id") == want), None)
            if dev is None:
                continue                             # forgotten by Orpheus: fall through
            if not dev.get("online"):
                raise om.OrpheusError("PLAYER_OFFLINE", f"The player on {dev.get('name')} isn't "
                                      "connected. Is the Cantinero resident running there?")
            return dev
    online = [d for d in players if d.get("online")]
    if len(online) == 1:
        return online[0]
    if len(online) > 1:
        raise om.OrpheusError("WHICH_PLAYER", "Several players are open: "
                              + ", ".join(str(d.get("name")) for d in online) + ". Ask which one.")
    raise om.OrpheusError("PLAYER_OFFLINE", "No music player is connected. Is the Cantinero "
                          "resident running on the speaker machine?")


def _refusal(e: om.OrpheusError) -> dict:
    if e.code == "AUTOPLAY_BLOCKED":
        return error(e.code, "The player has the music loaded but the browser won't start sound "
                     "until someone clicks the player window once.")
    if e.code == "NOTHING_PLAYING":
        return error(e.code, "Nothing is playing.")
    if e.code == "OFFLINE":
        return error("PLAYER_OFFLINE", "The player closed.")
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


def _save_pending(ctx: SkillContext, cands: list[dict] | None) -> None:
    path = _pending_path(ctx)
    try:
        if not cands:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": time.time(), "candidates": cands}))
    except OSError:
        pass


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
            cands, how = await _search(st, query, args.get("kinds"))
            if not cands:
                return error("MEDIA_NOT_FOUND", f"Nothing in the music library matches {query!r}.")
            if how["ambiguous"] and len(cands) > 1:
                _save_pending(ctx, cands[:3])
                return {"ok": True, "played": False, "candidates": cands[:3],
                        "ask": how["ask"] or "Ask which one they meant."}
            pick = cands[0]
        body: dict = {"type": "play"}
        if pick["id"].isdigit():
            body["item_id"] = int(pick["id"])
        else:
            body["playlist_id"] = pick["id"]
        if args.get("shuffle"):
            body["shuffle"] = True
        dev = await _target(ctx, st, args.get("player", ""))
        await om.command(st["url"], st["token"], dev["id"], body)
    except om.OrpheusError as e:
        return _refusal(e)
    _save_pending(ctx, None)
    return {"ok": True, "played": True, "playing": pick.get("name") or "it", "player": dev.get("name")}


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
        await om.command(st["url"], st["token"], dev["id"], body)
    except om.OrpheusError as e:
        return _refusal(e)
    return {"ok": True, "done": action, "player": dev.get("name")}


async def _status(ctx: SkillContext, args: dict) -> dict:
    st = _paired_state(ctx)
    if not st:
        return NOT_PAIRED
    try:
        devs = await om.devices(st["url"], st["token"])
    except om.OrpheusError as e:
        return error(e.code, str(e))
    out = []
    for d in _players(devs):
        s = d.get("state") or {}
        out.append({"name": d.get("name"), "online": bool(d.get("online")),
                    "status": s.get("status") if d.get("online") else "closed",
                    "volume": s.get("volume"), "track": (s.get("queue_index") or 0) + 1 if s else None,
                    "of": s.get("queue_length")})
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
        await om.command(st["url"], st["token"], dev["id"], body)
    except om.OrpheusError as e:
        return {"NOT_PAIRED": "The music server doesn't know me any more; it needs pairing again.",
                "PLAYER_OFFLINE": "The music player isn't open.",
                "OFFLINE": "The music player isn't open.",
                "NOTHING_PLAYING": "Nothing's playing.",
                "WHICH_PLAYER": "More than one player is open, so tell me which.",
                "AUTOPLAY_BLOCKED": "The player needs one click before it can make sound.",
                "SERVER_UNREACHABLE": "I can't reach the music server."}.get(e.code, "That didn't work.")
    return f"Volume {body['level']}." if cmd["type"] == "volume" else _SAID.get(cmd["type"], "Done.")


async def duck(root, on: bool, player_id: str = "") -> None:
    """Lower the music while she speaks or listens (Orpheus AD-8: relative, restored exactly,
    and undone by the player after a minute if this never arrives). A resident ducks its own
    player, the web page the default one (R-7). Best effort, silent."""
    st = om.load(root)
    target = player_id or st.get("default_player_id")
    if not om.paired(st) or not target:
        return
    body = {"type": "duck", "level": DUCK_LEVEL} if on else {"type": "unduck"}
    try:
        await om.command(st["url"], st["token"], target, body)
    except om.OrpheusError:
        pass


# --- Settings → Music ----------------------------------------------------------------------

async def players(root) -> dict:
    """The Orpheus players and which is the default, for Settings → Music."""
    st = om.load(root)
    if not om.paired(st):
        return {"paired": False, "players": [], "default_player_id": ""}
    try:
        devs = _players(await om.devices(st["url"], st["token"]))
    except om.OrpheusError as e:
        return {"paired": True, "error": str(e), "players": [],
                "default_player_id": st.get("default_player_id", "")}
    return {"paired": True, "server": st.get("server_name", "Orpheus"),
            "default_player_id": st.get("default_player_id", ""),
            "players": [{"id": d.get("id"), "name": d.get("name"), "online": bool(d.get("online"))}
                        for d in devs]}


def set_default_player(root, player_id: str, name: str = "") -> None:
    st = om.load(root)
    if not om.paired(st):
        return
    st["default_player_id"], st["default_player_name"] = player_id or "", name or ""
    st.pop("player_id", None)                       # #36's "used last" is not a rule any more
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
            "To play something, call media_play with what they said; don't search first.")
    pending = _load_pending(ctx)
    if pending:
        note += (" You offered these and are waiting for the user's pick (call media_play with its id): "
                 + "; ".join(f"{i}) {c['name']} [id {c['id']}]" for i, c in enumerate(pending, 1)))
    return note


register(Skill("dj", "DJ", "Play music from your Orpheus library and control it by voice.",
               _tools, note=_note))
