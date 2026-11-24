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

import asyncio
import json
import random
import re
import time
from datetime import datetime, timezone

from services.skills import SkillContext, Skill, Tool, error, register
from services.skills import music_catalog
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


def _just_played(ctx: SkillContext, what: str, dev: dict) -> bool:
    """The same play to the same player moments ago, and it is still playing: a repeat, not a
    request. Paused or stopped since, it plays again (found live: "play" after "pause" was
    refused as already playing)."""
    if (dev.get("state") or {}).get("status") != "playing":
        return False
    try:
        p = json.loads((ctx.root / "media" / "last_play.json").read_text())
    except (OSError, ValueError):
        return False
    return (p.get("what") == what.strip().lower() and p.get("player") == dev.get("id")
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
                   "ambiguous": bool(out.get("needs_disambiguation")),
                   "top": max((float(c.get("score") or 0) for c in strong), default=0.0),
                   "raw": strong[:5]}


# --- settling a tie without her (#79) --------------------------------------------------------
# "play Back in Black" tied four tracks at 0.78: that album's tracks are tagged
# "Back in Black - <song>", so the album name matched every one, and the model took 66 s to
# pick (2026-11-24). The exact title settles most ties in code; Fortuna takes the rest.

TIE_GAP = 0.01               # scores this close count as a tie
TIE_FORTUNA_MIN = 0.8        # Fortuna must be this sure to settle what the title couldn't


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def _asked(query: str) -> tuple[str, str]:
    """"play Back in Black by ACDC" -> ("back in black", "acdc")."""
    what = _PLAY_WORDS.sub("", query).strip()
    title, sep, artist = what.rpartition(" by ")
    return (_norm(title), _norm(artist)) if sep and title else (_norm(what), "")


def exact_pick(query: str, raw: list[dict]) -> int | None:
    """The one tied candidate whose title is what was asked for, or None. A title counts when it
    is the request, or its last " - " part is (the "Album - Song" tag pattern); "by <artist>" in
    the request must then be in the candidate's display."""
    if not raw:
        return None
    title, artist = _asked(query)
    if not title:
        return None
    top = max(float(c.get("score") or 0) for c in raw)
    hits = []
    for i, c in enumerate(raw):
        if float(c.get("score") or 0) < top - TIE_GAP:
            continue
        t = c.get("title") or ""
        if title not in (_norm(t), _norm(t.rsplit(" - ", 1)[-1])):
            continue
        if artist and artist not in _norm(c.get("display") or ""):
            continue
        hits.append(i)
    return hits[0] if len(hits) == 1 else None


def artist_pick(raw: list[dict]) -> int | None:
    """An artist tied with their own albums and tracks: the artist ("some Hendrix" asked
    "Jimi Hendrix, Greatest Hits by Jimi Hendrix, or Purple Haze by Jimi Hendrix?", 2026-11-24)."""
    if not raw:
        return None
    top = max(float(c.get("score") or 0) for c in raw)
    tied = [(i, c) for i, c in enumerate(raw) if float(c.get("score") or 0) >= top - TIE_GAP]
    artists = [(i, c) for i, c in tied if c.get("kind") == "artist"]
    if len(artists) != 1:
        return None
    i, a = artists[0]
    name = _norm(a.get("title") or a.get("display") or "")
    by = lambda c: _norm((c.get("display") or "").rpartition(" — ")[2]).startswith(name)   # noqa: E731
    return i if name and all(j == i or by(c) for j, c in tied) else None


async def _settle(query: str, raw: list[dict]) -> int | None:
    """Which tied candidate was meant: the exact title, the artist over their own work, else
    Fortuna when it is sure, else None (she asks)."""
    i = exact_pick(query, raw)
    if i is None:
        i = artist_pick(raw)
    if i is not None:
        return i
    from services import fortuna
    top = max((float(c.get("score") or 0) for c in raw), default=0.0)
    tied = [i for i, c in enumerate(raw) if float(c.get("score") or 0) >= top - TIE_GAP]
    if len(tied) < 2:
        return None
    labels = {}
    for i in tied:
        labels.setdefault(raw[i].get("display") or raw[i].get("title") or str(i), i)
    if len(labels) < 2:
        return None
    got = await fortuna.choose(query, "Which of these did the user ask to play?",
                               {d: d for d in labels})
    if got is None or got[1] < TIE_FORTUNA_MIN:
        return None
    return labels.get(got[0])


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
    song = args.get("album_of", "") or album_of(query)
    if song:
        try:
            said = await _play_album_of(ctx, st, song, query or song)
        except om.OrpheusError as e:
            return _refusal(e)
        played = isinstance(said, Done)
        return {"ok": played or said.startswith("Which one"), "played": played,
                "asked": not played and said.startswith("Which one"), "final": said}
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
            # One call does what the fast lane does, and its line ends the turn (#82): a model
            # pass after a tool costs ~18 s, and she used to take three or four per play.
            from services import fortuna
            (cands, how), kind = await asyncio.gather(
                _search(st, query, args.get("kinds")),
                fortuna.choose(query, "Does the user name what to play, or describe the kind of music they want?",
                               _PLAY_KINDS))
            clear = cands and not how["ambiguous"] and how["top"] >= NAMED_SCORE
            if kind and kind[0] == "described" and kind[1] >= DESCRIBED_MIN and not clear:
                said = await _play_described(ctx, st, query)
                if said:
                    return {"ok": True, "played": True, "final": said}
            if not cands:
                return dict(error("MEDIA_NOT_FOUND", f"Nothing in the music library matches {query!r}."),
                            final=f"I can't find {_PLAY_WORDS.sub('', query).strip() or query} in your music.")
            pick = cands[0]
            if how["ambiguous"] and len(cands) > 1:
                i = await _settle(query, how["raw"])
                if i is not None:
                    pick = cands[i]
                else:
                    opts = either_or(cands, how["raw"])
                    if not opts:
                        _save_pending(ctx, cands[:3], query)
                        return _choices(cands[:3], how["ask"])      # she words it
                    _save_pending(ctx, opts, query)                  # an id still works if she's asked
                    _pending[_ask_key(ctx)] = {"options": opts, "query": query, "at": time.monotonic()}
                    return {"ok": True, "played": False, "nothing_is_wrong": True, "final": question(opts),
                            "asked": True}
        dev = await _target(ctx, st, args.get("player", ""))
        if _just_played(ctx, query or choice_id, dev):
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
    # A bare "play" is a resume: after "pause" it went to the model (19 s) and restarted the
    # album (R5, 2026-11-24). "Continue" stays hers; it is as often about the conversation.
    (re.compile(rf"^(?:resume|unpause|keep playing|resume{_IT}|unpause{_IT}|continue the (?:music|song|track|playback)|"
                rf"start{_IT} again|play it again|play|play (?:the )?music)$"), {"type": "resume"}),
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

# The fast lane for "play …": Fortuna says it's a play at this confidence or more, and Orpheus
# finds one clear match, so it plays with no model turn (~1 s, not ~15 s). Anything less
# sure goes to her as before, where she can ask.
LANE_MIN = 0.8
# Below LANE_MIN, down to LANE_LOW, a play still takes the lane when Orpheus finds the name
# exactly: "play Before These Crowded Streets" scored 0.76 (the title doesn't sound like
# music) with one exact album match, and took 25 s through the model (R5, 2026-11-24).
LANE_LOW = 0.6
EXACT_SCORE = 0.95
_PLAY_WORDS = re.compile(r"^(?:play|put on|start)\b\s*(?:some|the|me|us)?\b\s*", re.I)
_LANE_INTENTS = {
    "play_music": "start playing a song, album, artist or playlist",
    "control_music": "pause, resume, skip, go back or change the volume of music already playing",
    "music_question": "ask what is playing, or what music there is",
    "other": "anything else: conversation, questions, tasks",
}
# A change of mind in front ("never mind, play Santana") isn't part of what to play: it used to
# send that request through the model (22 s, 2026-11-24).
_HEAD = re.compile(r"^\s*(?:(?:hey|ok|okay)\b[ ,]*)?(?:{name}\b[ ,!.]*)?"
                   r"(?:(?:never ?mind|actually|instead|no|nah|oh|wait)\b[ ,!.]*)*"
                   r"(?:(?:can|could|would|will) you\b[ ,]*)?(?:please\b[ ,]*)?", re.I)


class Done(str):
    """What she says after a music action that worked ("Playing Rush on Predator."). The page
    shows it and lets the music play rather than speaking over it (user, 2026-11-24: "She does
    not need to announce the song... just play it normally and show the text"). A failure
    stays a plain str, so it is still said aloud."""


_PLAY_KINDS = {"named": "names a specific song, album, artist or playlist",
               "described": "describes a kind, mood or style of music, or asks for any music"}
DESCRIBED_MIN = 0.8
# A name scores 1.00 in Orpheus ("some Dave Matthews Band", "Santana"); a description 0.78 at
# most ("some music" found a Doors track, not ambiguous), so above this a name wins.
NAMED_SCORE = 0.9


async def _play_described(ctx: SkillContext, st: dict, query: str) -> str | None:
    """"play something upbeat": an album Fortuna picks from the catalog, shuffled (#80).
    ~1 s, against ~60 s through the model. None (hers) when there is no catalog yet or
    Fortuna can't say."""
    album = await music_catalog.pick(ctx.root, st, query)
    if album is None:
        return None
    dev = await _target(ctx, st)
    queue = await om.expand(st["url"], st["token"], item_id=int(album["id"]))
    if not queue:
        return None
    random.shuffle(queue)
    await _send(dev["id"], {"type": "play", "queue": queue, "start_index": 0})
    _remember_play(ctx, query, dev["id"])
    by = f" by {album['artist']}" if album.get("artist") else ""
    return Done(f"Playing {album['title']}{by}, shuffled, on {dev.get('name')}.")


async def fast_play(ctx: SkillContext, text: str, name: str = "") -> str | None:
    """A plain "play …" done without the model: what she says, or None to hand it to her."""
    from services import fortuna

    st = _paired_state(ctx)
    if not st or len(text) > 160:
        return None
    head = re.compile(_HEAD.pattern.replace("{name}", re.escape(name) if name else "(?!)"), re.I)
    query = head.sub("", text).strip().rstrip("?.!")
    if not query:
        return None
    if _ask_key(ctx) in _pending:                       # she just asked "which one?" (#81)
        answered = await _answer(ctx, st, query)
        if answered:
            return answered
    # Fortuna is asked about the request itself: "Mira, can you play some Dave Matthews
    # Band?" scored 0.77, under the bar, where "play some Dave Matthews Band" scored 0.87.
    got = await fortuna.choose(query, "What does the user want the assistant to do?", _LANE_INTENTS)
    if got is None or got[0] != "play_music" or got[1] < LANE_LOW:
        return None
    sure = got[1] >= LANE_MIN
    song = album_of(query)
    if song:
        try:
            return await _play_album_of(ctx, st, song, query)
        except om.OrpheusError:
            return None
    music_catalog.refresh_soon(ctx.root, st)          # kept warm for the next vague play (#80)
    try:
        # Named ("play Crash") or described ("play something upbeat")? Asked while Orpheus
        # searches, so a play by name waits no longer for it.
        (cands, how), kind = await asyncio.gather(
            _search(st, query, None),
            fortuna.choose(query, "Does the user name what to play, or describe the kind of music they want?",
                           _PLAY_KINDS))
        clear = cands and not how["ambiguous"] and how["top"] >= NAMED_SCORE
        if kind and kind[0] == "described" and kind[1] >= DESCRIBED_MIN and not clear:
            return await _play_described(ctx, st, query)
        if not cands and sure:
            # A plain play of something the library doesn't have: say so now. Through the
            # model this took three media_play calls and 68 s to reach the same answer (R5).
            what = _PLAY_WORDS.sub("", query).strip() or query
            return f"I can't find {what} in your music."
        if not cands:
            return None
        pick = cands[0]
        if how["ambiguous"]:
            i = await _settle(query, how["raw"])        # a tie: the exact title, else Fortuna
            if i is None:
                opts = either_or(cands, how["raw"])
                if not opts:
                    return None                       # she sorts it out
                _pending[_ask_key(ctx)] = {"options": opts, "query": query, "at": time.monotonic()}
                return question(opts)                 # said aloud: it's a question
            pick = cands[i]
        elif not sure and how["top"] < EXACT_SCORE:
            return None                               # unsure, and no exact name: hers
        if not pick.get("playable", True):
            return None
        return await _play_pick(ctx, st, pick, query)
    except om.OrpheusError:
        return None                                   # let her explain it


async def _play_pick(ctx: SkillContext, st: dict, pick: dict, query: str) -> str | None:
    """Play one candidate where she is; her line, or None. Orpheus errors go to the caller."""
    dev = await _target(ctx, st)
    # What is played, not just what was said: two answers to the same "which one?" share the
    # words, and the second used to get "It's playing." with nothing changed (2026-11-24).
    what = f"{query} #{pick.get('id')}:{pick.get('start_track') or ''}"
    if _just_played(ctx, what, dev):
        return Done("It's playing.")
    queue = await om.expand(st["url"], st["token"], item_id=int(pick["id"]) if pick["id"].isdigit() else None,
                            playlist_id="" if pick["id"].isdigit() else pick["id"])
    if not queue:
        return None
    # An album asked for by one of its songs starts at that song (#87).
    start = next((i for i, e in enumerate(queue) if e.get("id") == pick.get("start_track")), 0)
    await _send(dev["id"], {"type": "play", "queue": queue, "start_index": start})
    _remember_play(ctx, what, dev["id"])
    song = f" from {pick['start_title']}" if pick.get("start_title") and start else ""
    return Done(f"Playing {_spoken(pick['name'])}{song} on {dev.get('name')}.")


# --- "the album with <song>" (#87) -------------------------------------------------------------
# It took the model 67 s and three searches (2026-11-24). A song can be on several albums (a
# live set, a best-of): one plays from that song, several get the either-or question.

_ALBUM_OF = re.compile(r"^(?:the\s+)?(?:album|record|cd)\s+(?:with|that has|that's got|that has got|containing|"
                       r"which has|featuring|that contains)\s+(?:(?:the\s+)?song\s+)?(.+)$", re.I)


def album_of(query: str) -> str:
    """"put on the album with Ants Marching" -> "Ants Marching"; "" when it isn't that ask."""
    m = _ALBUM_OF.match(_PLAY_WORDS.sub("", query).strip())
    return m.group(1).strip().strip("\"'") if m else ""


async def _albums_with(st: dict, song: str) -> list[dict]:
    """The albums the best-matching song is on, as candidates that start at it."""
    cands, how = await _search(st, song, ["track"])
    raw = how["raw"]
    if not cands or not raw:
        return []
    top = max(float(c.get("score") or 0) for c in raw)
    tied = [r for r in raw if float(r.get("score") or 0) >= top - TIE_GAP and r.get("playable", True)]
    full = await asyncio.gather(*[om._request("GET", f"{st['url']}/api/v1/items/{r['item_id']}", token=st["token"])
                                  for r in tied], return_exceptions=True)
    out, seen = [], set()
    for r, it in zip(tied, full):
        album = (it.get("parent") or {}) if isinstance(it, dict) else {}
        if not album.get("id") or album["id"] in seen:
            continue
        seen.add(album["id"])
        artist = (r.get("display") or "").rpartition(" — ")[2]
        year = re.search(r"\((\d{4})\)$", artist)
        artist = re.sub(r"\s*\(\d{4}\)$", "", artist)
        name = f"{album.get('title', '')} — {artist}" if artist else album.get("title", "")
        out.append({"id": str(album["id"]), "kind": "album", "playable": True,
                    "name": f"{name} ({year.group(1)})" if year else name,   # the song's year: the album's
                    "start_track": int(r["item_id"]), "start_title": r.get("title") or song})
    return out[:ASK_MAX]


async def _play_album_of(ctx: SkillContext, st: dict, song: str, query: str) -> str | None:
    """Play the album with this song from it, ask which album, or say it isn't there."""
    albums = await _albums_with(st, song)
    if not albums:
        return f"I can't find a song called {song} in your music."
    if len(albums) == 1:
        return await _play_pick(ctx, st, albums[0], query)
    _pending[_ask_key(ctx)] = {"options": albums, "query": query, "at": time.monotonic()}
    return question(albums)


# --- either-or, without her (#81) -------------------------------------------------------------
# A tie neither the title nor Fortuna can settle: a fixed question at once ("Which one: Crash by
# Dave Matthews Band, or Crash by Someone Else?"), and the answer mapped to the options by
# Fortuna (~0.2 s). Both used to be model passes (~18 s each). Anything else said next clears it.

ASK_FOR_S = 90.0
CHOICE_MIN = 0.7
ASK_MAX = 5                  # matched against the answer; the first SAY_MAX are said
SAY_MAX = 3
_ORDINALS = ("first", "second", "third", "fourth", "fifth")
_pending: dict[str, dict] = {}


def _ask_key(ctx: SkillContext) -> str:
    return ctx.session_id or ctx.user_id


def either_or(cands: list[dict], raw: list[dict]) -> list[dict]:
    """The tied, playable candidates worth a question: up to three, each said differently."""
    if not raw:
        return []
    top = max(float(c.get("score") or 0) for c in raw)
    out, said = [], set()
    for c, r in zip(cands, raw):
        if float(r.get("score") or 0) < top - TIE_GAP or not c.get("playable", True):
            continue
        name = _spoken(c["name"])
        if name.lower() in said:
            continue                                  # two the same aloud: a question can't tell them apart
        said.add(name.lower())
        out.append(c)
        if len(out) == ASK_MAX:
            break
    return out if len(out) >= 2 else []


def question(options: list[dict]) -> str:
    """"Which one: A, B, or C?"; past three, "Which one: A, B, C, or one of 2 more?" (an answer
    naming one of those still counts: _answer matches against them all)."""
    names = [_spoken(o["name"]) for o in options]
    if len(names) > SAY_MAX:
        return "Which one: " + ", ".join(names[:SAY_MAX]) + f", or one of {len(names) - SAY_MAX} more?"
    return "Which one: " + ", ".join(names[:-1]) + ", or " + names[-1] + "?"


# Ordinals only: "one" ends half the answers ("the nineties one") and "two" is in names
# ("Remember Two Things"), so a bare number counts only after "number".
_ORDINAL_WORDS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
                  "fourth": 3, "4th": 3, "fifth": 4, "5th": 4}
_NUMBERS = {"one": 0, "1": 0, "two": 1, "2": 1, "three": 2, "3": 2, "four": 3, "4": 3, "five": 4, "5": 4}
_DECADES = {"sixties": 196, "seventies": 197, "eighties": 198, "nineties": 199, "noughties": 200,
            "sixty": 196, "seventy": 197, "eighty": 198, "ninety": 199}
_FILLER = {"the", "one", "a", "an", "album", "record", "please", "that", "i", "want", "play", "uh", "um",
           "hmm", "by", "of", "on", "s", "it", "version", "with", "number"}


def answer_pick(text: str, opts: list[dict]) -> int | None:
    """An answer code can place without Fortuna: an ordinal ("the first", "the last one"), a decade
    or year ("the nineties one", "the 2014 one"), or words that are in one option's name only."""
    words = _norm(text).split()
    if not words or not opts:
        return None
    if "last" in words:
        return len(opts) - 1
    ords = {_ORDINAL_WORDS[w] for w in words if w in _ORDINAL_WORDS}
    ords |= {_NUMBERS[b] for a, b in zip(words, words[1:]) if a == "number" and b in _NUMBERS}
    if len(ords) == 1 and (i := ords.pop()) < len(opts):
        return i
    years = [re.search(r"\((\d{4})\)$", o.get("name") or "") for o in opts]
    for w in words:
        dec = _DECADES.get(w) or (int(w[:3]) if re.fullmatch(r"(19|20)\d0s", w) else None)
        hits = [i for i, y in enumerate(years) if y and (int(y.group(1)) // 10 == dec if dec else y.group(1) == w)]
        if (dec or re.fullmatch(r"(19|20)\d\d", w)) and len(hits) == 1:
            return hits[0]
    key = [w for w in words if w not in _FILLER]
    if key:
        hits = [i for i, o in enumerate(opts) if all(w in _norm(o.get("name") or "").split() for w in key)]
        if len(hits) == 1:
            return hits[0]
    return None


async def _answer(ctx: SkillContext, st: dict, text: str) -> str | None:
    """The answer to her either-or, played; None when it isn't one (the turn goes on as usual)."""
    from services import fortuna

    key = _ask_key(ctx)
    pend = _pending.pop(key, None)
    if not pend or time.monotonic() - pend["at"] > ASK_FOR_S:
        return None
    opts = pend["options"]
    i = answer_pick(text, opts)
    if i is not None:
        try:
            return await _play_pick(ctx, st, opts[i], pend["query"])
        except om.OrpheusError:
            return None
    # The year stays in what Fortuna reads ("the nineties one"), though not in what she says.
    labels = {str(i + 1): f"the {_ORDINALS[i]} one: {o['name'].replace(' — ', ' by ')}" for i, o in enumerate(opts)}
    labels["none"] = "none of these: something else, or a new request"
    got = await fortuna.choose(text, f'She asked: "{question(opts)}" Which did the user choose?', labels)
    if got is None or got[0] not in labels or got[0] == "none":
        return None                                   # something else: the turn goes on as usual
    if got[1] < CHOICE_MIN:
        # An answer, but not one Fortuna can place ("the live one" read 0.40): ask once more in
        # fixed words rather than hand it to her, who once played the wrong thing (42 s).
        if pend.get("reasked"):
            return None
        _pending[key] = dict(pend, at=time.monotonic(), reasked=True)
        ords = [f"the {_ORDINALS[i]}" for i in range(len(opts))]
        return "Sorry, which one: " + ", ".join(ords[:-1]) + ", or " + ords[-1] + "?"
    try:
        return await _play_pick(ctx, st, opts[int(got[0]) - 1], pend["query"])
    except om.OrpheusError:
        return None


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
    return Done(f"Volume {body['level']}." if cmd["type"] == "volume" else _SAID.get(cmd["type"], "Done."))


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
        "album_of": {"type": "string", "maxLength": 200,
                     "description": "A song, when the user wants the album that has it"},
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
    "Search the music library without playing, only to answer what's there. Never before playing: "
    "media_play searches itself.",
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
