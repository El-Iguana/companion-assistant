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
from services.skills import music_tempo
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


# --- an artist asked for: a random song of theirs, never a question --------------------------
# "a song by Stevie Nicks" tied five of her tracks at 1.00 and she asked which; Whisper's
# "Stevie Mix" scored 0.42 (under MIN_SCORE) and tied with a Dave Matthews "Radio Mix"
# (2026-11-24). The user: "one should be selected at random without a further question".
# So the artist is looked up on its own and matched to what was heard by spelling, and their
# songs play shuffled.

ARTIST_MATCH = 0.65          # "stevie mix" ~ "stevie nicks" 0.73, "stevie next" 0.70; "crash" ~ "crash test dummies" 0.43
_BY_ARTIST = re.compile(
    r"^(?:(?:a|an|any|one|some|another|a\s+few)\s+)?(?:random\s+)?"
    r"(?:(?:songs?|tracks?|tunes?|music|numbers?|hits?)\s+|something\s+|anything\s+)(?:from|by)\s+(.+)$", re.I)


def artist_asked(query: str) -> str:
    """"play a song by Stevie Nicks" -> "Stevie Nicks"; "" when it doesn't ask for an artist's
    music in so many words ("play Back in Black by ACDC" names a title)."""
    m = _BY_ARTIST.match(_PLAY_WORDS.sub("", query).strip())
    return m.group(1).strip().strip("\"'") if m else ""


def _like(heard: str, name: str) -> float:
    """How alike a heard artist and a library one are by spelling (1.0: one starts the other)."""
    a, n = _norm(heard), _norm(name)
    if len(a) < 3 or not n:
        return 0.0
    short, long_ = sorted((a, n), key=len)
    if long_.startswith(short + " ") and len(short) >= 0.6 * len(long_):
        return 1.0                                    # "Dave Matthews" for "Dave Matthews Band", not "Crash" for "Crash Test Dummies"
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, n).ratio()


async def find_artist(st: dict, heard: str) -> dict | None:
    """The library artist meant by what was heard, or None. Orpheus finds Stevie Nicks for
    "Stevie Mix" at 0.42; the spelling decides, not that score."""
    out = await om.resolve(st["url"], st["token"], heard, ["artist"])
    best = None
    for c in out.get("candidates") or []:
        if c.get("kind") != "artist" or not c.get("playable", True):
            continue
        r = _like(heard, c.get("title") or c.get("display") or "")
        if r >= ARTIST_MATCH and (best is None or r > best[0]):
            best = (r, c)
    return best[1] if best else None


def _artist_of(c: dict) -> str:
    """"Edge Of Seventeen — Stevie Nicks (2007)" -> "stevie nicks"; an artist's own name."""
    if c.get("kind") == "artist":
        return _norm(c.get("title") or c.get("display") or "")
    return _norm(re.sub(r"\s*\(\d{4}\)$", "", (c.get("display") or "").rpartition(" — ")[2]))


def one_artist_pick(raw: list[dict], rng: random.Random | None = None) -> int | None:
    """A tie of one artist's songs or albums ("a song by Stevie Nicks" tied five of hers):
    any one of them, at random. None when the tie spans artists."""
    if not raw:
        return None
    top = max(float(c.get("score") or 0) for c in raw)
    tied = [i for i, c in enumerate(raw) if float(c.get("score") or 0) >= top - TIE_GAP
            and c.get("playable", True)]
    who = {_artist_of(raw[i]) for i in tied}
    if len(tied) < 2 or len(who) != 1 or not next(iter(who)):
        return None
    return (rng or random).choice(tied)


async def _play_artist(ctx: SkillContext, st: dict, artist: dict, query: str) -> str | None:
    """Their songs, shuffled: a random one now, more of theirs after."""
    pick = {"id": str(artist.get("item_id")), "kind": "artist",
            "name": artist.get("title") or artist.get("display") or "", "playable": True}
    return await _play_pick(ctx, st, pick, query)


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
                fresh = {"server_id": info["server_id"], "server_name": info.get("name", "Orpheus"),
                         "url": base, "device_id": got["device_id"], "token": got["token"],
                         "paired_at": datetime.now(timezone.utc).isoformat()}
                om.save(ctx.root, fresh)
                done = {"ok": True, "status": "paired", "server": info.get("name", "Orpheus")}
                bound = await _ensure_profile(ctx, fresh)
                if bound.get("choose_profile"):
                    done["profiles"] = bound["choose_profile"]
                    done["how"] = ("the music server has several profiles: bind this device to the "
                                   "user's in the Orpheus dashboard, or their own playlists stay hidden")
                return done
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


def _with_tempo(ctx: SkillContext, st: dict, queue: list[dict]) -> list[dict]:
    """Music tracks carry their ``bpm`` when it's known (#114); measuring the rest goes on in
    the background. Spoken word never has one."""
    music_tempo.refresh_soon(ctx.root, st)
    return music_tempo.annotate(ctx.root, queue)


async def _ensure_profile(ctx: SkillContext, st: dict) -> dict:
    """Bind this device to a profile, once (#112). A device Orpheus calls "shared" sees only
    shared playlists, so the user's own ("Pink Favorites", 2026-11-25) were never found.
    With one profile there's nothing to choose; with several, the user binds it in the
    Orpheus dashboard, and this looks again next time. Never fails a play: on an error the
    search goes ahead as before."""
    if st.get("profile"):
        return {"profile": st["profile"]}
    try:
        info = await om.me(st["url"], st["token"])
        if info.get("binding") != "bound":
            found = await om.profiles(st["url"], st["token"])
            if len(found) != 1:
                return {"choose_profile": [p.get("name", "") for p in found]}
            await om.bind_self(st["url"], st["token"], found[0]["id"])
            name = found[0].get("name", "")
        else:
            name = (info.get("profile") or {}).get("name", "")
    except (om.OrpheusError, KeyError, TypeError):
        return {}
    st["profile"] = name or "(bound)"
    om.save(ctx.root, st)
    return {"profile": st["profile"]}


def _paired_state(ctx: SkillContext) -> dict | None:
    st = om.load(ctx.root)
    return st if om.paired(st) else None


NOT_PAIRED = error("NOT_PAIRED", "Not connected to the music server yet.")


async def _find(ctx: SkillContext, args: dict) -> dict:
    st = _paired_state(ctx)
    if not st:
        return NOT_PAIRED
    await _ensure_profile(ctx, st)
    try:
        cands, how = await _search(st, args["query"], args.get("kinds"))
    except om.OrpheusError as e:
        return error(e.code, str(e))
    if not cands:
        return error("MEDIA_NOT_FOUND", f"Nothing in the music library matches {args['query']!r}.")
    res = {"ok": True, "candidates": cands, "ambiguous": how["ambiguous"]}
    _save_pending(ctx, cands if how["ambiguous"] else None)
    return res


async def _play(ctx: SkillContext, args: dict) -> dict:
    st = _paired_state(ctx)
    if not st:
        return NOT_PAIRED
    await _ensure_profile(ctx, st)
    choice_id, query = args.get("id", ""), args.get("query", "")
    song = args.get("album_of", "") or album_of(query)
    if song:
        try:
            said = await _play_album_of(ctx, st, song, query or song)
        except om.OrpheusError as e:
            return _refusal(e)
        played = isinstance(said, Done)
        return {"ok": played, "played": played, "final": said}
    if not choice_id and not query:
        return error("BAD_ARGUMENTS", "Give the query the user said, or the id of a choice.")
    try:
        if choice_id:
            known = {c["id"]: c for c in _load_pending(ctx)}
            pick = known.get(choice_id) or {"id": choice_id, "kind": "playlist" if not choice_id.isdigit()
                                            else "", "name": ""}
        else:
            # One call does what the fast lane does, and its line ends the turn (#82): a model
            # pass after a tool costs ~18 s, and she used to take three or four per play.
            from services import fortuna
            who = artist_asked(query)                       # "a song by Stevie Nicks": any of hers
            artist = await find_artist(st, who) if who else None
            if artist:
                said = await _play_artist(ctx, st, artist, query)
                if said:
                    return {"ok": True, "played": True, "final": said}
            (cands, how), kind = await asyncio.gather(
                _search(st, query, args.get("kinds")),
                fortuna.choose(query, "Does the user name what to play, or describe the kind of music they want?",
                               _PLAY_KINDS))
            clear = cands and not how["ambiguous"] and how["top"] >= NAMED_SCORE
            if kind and kind[0] == "described" and kind[1] >= DESCRIBED_MIN and not clear:
                said = await _play_described(ctx, st, query)
                if said:
                    return {"ok": True, "played": True, "final": said}
            if not who and (not cands or (how["ambiguous"] and artist_pick(how["raw"]) is None)):
                # Nothing, or a tie: maybe an artist Whisper misspelt ("Stevie Mix").
                heard = _PLAY_WORDS.sub("", query).strip()
                artist = await find_artist(st, heard) if heard else None
                if artist:
                    said = await _play_artist(ctx, st, artist, query)
                    if said:
                        return {"ok": True, "played": True, "final": said}
            if not cands:
                return dict(error("MEDIA_NOT_FOUND", f"Nothing in the music library matches {query!r}."),
                            final=f"I can't find {_PLAY_WORDS.sub('', query).strip() or query} in your music.")
            pick = cands[0]
            if how["ambiguous"] and len(cands) > 1:
                i = await _settle(query, how["raw"])
                if i is None:
                    i = one_artist_pick(how["raw"])          # several of one artist's: any of them
                if i is None:
                    i = any_pick(cands, how["raw"])          # nothing settles it: one at random
                if i is not None:
                    pick = cands[i]
        dev = await _target(ctx, st, args.get("player", ""))
        if _just_played(ctx, query or choice_id, dev):
            # The same play to the same player again in the same breath: it's on; don't restart it.
            return {"ok": True, "played": True, "already_playing": True, "player": dev.get("name"),
                    "final": "It's playing."}
        queue = await om.expand(st["url"], st["token"], item_id=int(pick["id"]) if pick["id"].isdigit() else None,
                                playlist_id="" if pick["id"].isdigit() else pick["id"])
        queue = _with_tempo(ctx, st, queue)
        if not queue:
            return error("NOTHING_TO_PLAY", "Nothing in that can be played (its files may be missing).")
        if args.get("shuffle") or pick.get("kind") == "artist":
            random.shuffle(queue)                       # an artist: any of their songs first
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
    (re.compile(rf"^(?:pause|pause{_IT})$"), {"type": "pause"}),
    # "Mira, stop." went to the model (18.8 s, 2026-11-24), and "stop the music" only paused:
    # the player has had a real stop since R7.
    (re.compile(rf"^(?:stop|stop{_IT}|stop playing|that's enough|enough|turn{_IT} off|turn off{_IT}|"
                rf"(?:the )?music off|kill{_IT}|shut{_IT} off|shut off{_IT})$"), {"type": "stop"}),
    # A bare "play" is a resume: after "pause" it went to the model (19 s) and restarted the
    # album (R5, 2026-11-24). "Continue" stays hers; it is as often about the conversation.
    (re.compile(rf"^(?:resume|unpause|keep playing|resume{_IT}|unpause{_IT}|continue the (?:music|song|track|playback)|"
                rf"start{_IT} again|play it again|play|play (?:the )?music)$"), {"type": "resume"}),
    # "Next" said any plain way (user, 2026-11-24: fast-track "Next"): "play next", "go to the
    # next song", "skip to the next one", "the next song", "next song please".
    (re.compile(r"^(?:(?:skip|next|move on|skip ahead)(?: (?:the |this |that )?(?:music|song|track|tune|one|it))?|"
                r"(?:(?:play|go to|skip to|on to|onto|move on to|jump to)(?: the)? |the )?next(?: (?:song|track|tune|one))?)$"),
     {"type": "next"}),
    (re.compile(rf"^(?:previous|go back|previous{_IT}|go back a (?:song|track)|"
                rf"(?:play )?the (?:previous|last) (?:song|track|one))$"), {"type": "previous"}),
    (re.compile(r"^(?:turn (?:it|the music|the volume|this) up|turn up the (?:music|volume)|louder|"
                r"volume up|raise the volume|a bit louder|crank it up)$"), {"type": "volume", "step": VOLUME_STEP}),
    (re.compile(r"^(?:turn (?:it|the music|the volume|this) down|turn down the (?:music|volume)|quieter|"
                r"softer|volume down|lower the volume|a bit quieter)$"), {"type": "volume", "step": -VOLUME_STEP}),
]
_SET_VOLUME = re.compile(r"^(?:set )?(?:the )?volume (?:to |at )?(\d{1,3})(?: ?%| percent)?$")


def fast_command(text: str, name: str = "") -> dict | None:
    """A music command said plainly enough to do without the model, else None. Whole
    utterances only ("pause the music"), so "I paused the movie earlier" goes to her. Her name
    in front ("Mira, stop the music", typed) is dropped like "can you"."""
    if name:
        text = re.sub(rf"^\s*(?:(?:hey|ok|okay)\b[ ,]*)?{re.escape(name)}\b[ ,!.]*", "", text or "", flags=re.I)
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


_SAID = {"pause": "Paused.", "resume": "Playing.", "stop": "Stopped.", "next": "Skipping.", "previous": "Going back."}

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
    queue = _with_tempo(ctx, st, queue)
    if not queue:
        return None
    random.shuffle(queue)
    await _send(dev["id"], {"type": "play", "queue": queue, "start_index": 0})
    _remember_play(ctx, query, dev["id"])
    by = f" by {album['artist']}" if album.get("artist") else ""
    return Done(f"Playing {album['title']}{by}, shuffled, on {dev.get('name')}.")


# Words that really start with "play": anything else Whisper wrote that way is "play" glued to
# a name ("Mira, playpink." for "play Pink", predator 2026-11-24).
_PLAY_WORDS_REAL = {"playback", "player", "players", "playlist", "playlists", "playing", "played", "plays",
                    "playful", "playground", "playoff", "playoffs", "playtime", "playhouse", "playmate",
                    "playwright", "playable", "playboy", "playbook", "playdate", "playroom", "playthrough"}


def _unglue_play(query: str) -> str:
    """"played Neil Young" -> "play Neil Young"; "playpink" -> "play pink"."""
    m = re.match(r"^(?:played|plays)\s+(.+)$", query, flags=re.I)
    if m:
        return "play " + m.group(1)
    m = re.match(r"^(play)([a-z]{2,})\b(.*)$", query, flags=re.I)
    if m and (m.group(1) + m.group(2)).lower() not in _PLAY_WORDS_REAL:
        return f"play {m.group(2)}{m.group(3)}"
    return query


async def fast_play(ctx: SkillContext, text: str, name: str = "") -> str | None:
    """A plain "play …" done without the model: what she says, or None to hand it to her."""
    from services import fortuna

    st = _paired_state(ctx)
    if not st or len(text) > 160:
        return None
    head = re.compile(_HEAD.pattern.replace("{name}", re.escape(name) if name else "(?!)"), re.I)
    query = head.sub("", text).strip().rstrip("?.!")
    # Whisper joins "Play Back in Black" into the word "playback" (predator, 2026-11-24), and
    # nobody starts a request to her with "playback": it's "play" and a title starting "Back".
    # Title case, because Fortuna read "play back in black" as a player control (rewind, 0.56)
    # and "play Back In Black" as a play.
    m = re.match(r"^playback\b\s*(.*)$", query, flags=re.I)
    if m:
        query = "play " + ("Back " + m.group(1)).strip().title()
    else:
        query = _unglue_play(query)
    if not query:
        return None
    # Fortuna is asked about the request itself: "Mira, can you play some Dave Matthews
    # Band?" scored 0.77, under the bar, where "play some Dave Matthews Band" scored 0.87.
    got = await fortuna.choose(query, "What does the user want the assistant to do?", _LANE_INTENTS)
    music = got is not None and got[0] == "play_music" and got[1] >= LANE_LOW
    # Short titles read as anything: "Play Crash" was "other" (0.52) and "Play Back in Black" a
    # play at only 0.59 (2026-11-24), so both went to the model (~20 s). Said with "play" in
    # front, it's a play when Orpheus has that exact name (below); Fortuna rules it out only
    # when it's sure this is a player control or a question.
    elsewhere = got is not None and got[0] in ("control_music", "music_question") and got[1] >= LANE_MIN
    by_name = not music and not elsewhere and bool(_PLAY_WORDS.match(query))
    if not music and not by_name:
        return None
    await _ensure_profile(ctx, st)                    # before any search: her own playlists (#112)
    sure = music and got[1] >= LANE_MIN
    song = album_of(query)
    if song:
        try:
            return await _play_album_of(ctx, st, song, query)
        except om.OrpheusError:
            return None
    music_catalog.refresh_soon(ctx.root, st)          # kept warm for the next vague play (#80)
    who = artist_asked(query)
    if who:                                           # "a song by Stevie Nicks": theirs, at random
        try:
            artist = await find_artist(st, who)
            if artist:
                return await _play_artist(ctx, st, artist, query)
        except om.OrpheusError:
            return None
    try:
        # Named ("play Crash") or described ("play something upbeat")? Asked while Orpheus
        # searches, so a play by name waits no longer for it.
        (cands, how), kind = await asyncio.gather(
            _search(st, query, None),
            fortuna.choose(query, "Does the user name what to play, or describe the kind of music they want?",
                           _PLAY_KINDS))
        clear = cands and not how["ambiguous"] and how["top"] >= NAMED_SCORE
        if kind and kind[0] == "described" and kind[1] >= DESCRIBED_MIN and not clear and not by_name:
            return await _play_described(ctx, st, query)
        if not cands or (how["ambiguous"] and artist_pick(how["raw"]) is None):
            # Nothing, or a tie: maybe an artist Whisper misspelt ("play Stevie Mix" scored 0.42
            # and tied with a "Radio Mix"). Theirs, at random, before "I can't find" or a question.
            heard = _PLAY_WORDS.sub("", query).strip()
            artist = await find_artist(st, heard) if not who and heard else None
            if artist:
                return await _play_artist(ctx, st, artist, query)
        if not cands and sure:
            # A plain play of something the library doesn't have: say so now. Through the
            # model this took three media_play calls and 68 s to reach the same answer (R5).
            what = _PLAY_WORDS.sub("", query).strip() or query
            return f"I can't find {what} in your music."
        if not cands:
            return None
        pick = cands[0]
        if how["ambiguous"] and by_name:
            # Only an exact name proves an unsure "play …" is music.
            i = exact_pick(query, how["raw"])
            if i is None:
                i = artist_pick(how["raw"])
            if i is None:
                return None
            pick = cands[i]
        elif how["ambiguous"]:
            i = await _settle(query, how["raw"])        # a tie: the exact title, else Fortuna
            if i is None:
                i = one_artist_pick(how["raw"])         # several of one artist's: any of them
            if i is None:
                i = any_pick(cands, how["raw"])         # nothing settles it: one at random, never a question
            if i is None:
                return None
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
    queue = _with_tempo(ctx, st, queue)
    if not queue:
        return None
    if pick.get("kind") == "artist":
        # An artist means any of their songs: shuffled, said by the one that starts (user,
        # 2026-11-24: "one should be selected at random without a further question").
        random.shuffle(queue)
        await _send(dev["id"], {"type": "play", "queue": queue, "start_index": 0})
        _remember_play(ctx, what, dev["id"])
        first = queue[0].get("title") or ""
        name = _spoken(pick["name"])
        return Done(f"Playing {first} by {name}, then more of {name}, on {dev.get('name')}."
                    if first else f"Playing {name}, shuffled, on {dev.get('name')}.")
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
    """Play the album with this song from it (one at random when it is on several), or say it
    isn't there."""
    albums = await _albums_with(st, song)
    if not albums:
        return f"I can't find a song called {song} in your music."
    return await _play_pick(ctx, st, random.choice(albums), query)   # several: any of them, never a question


# --- a tie nothing settles: one at random, never a question --------------------------------
# #81 asked "Which one: A, B, or C?" and read the answer; the user (2026-11-24): "always pick
# one. never ask".

ASK_MAX = 5                  # albums a song is on, considered for "the album with <song>"


def any_pick(cands: list[dict], raw: list[dict]) -> int | None:
    """A tie nothing settles ("Crash" by two bands): one of the tied, at random. The user
    (2026-11-24): "always pick one. never ask"."""
    if not cands:
        return None
    top = max((float(r.get("score") or 0) for r in raw), default=0.0)
    tied = [i for i, (c, r) in enumerate(zip(cands, raw))
            if float(r.get("score") or 0) >= top - TIE_GAP and c.get("playable", True)]
    tied = tied or [i for i, c in enumerate(cands) if c.get("playable", True)]
    return random.choice(tied) if tied else None


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
    "Play music: an artist, album, track or playlist, as the user said it (query), or something "
    "media_find returned (id). Always plays something: when several match, it picks one. Never ask which.",
    {"type": "object", "properties": {
        "query": {"type": "string", "maxLength": 200, "description": "What to play, as the user said it"},
        "id": {"type": "string", "maxLength": 64, "description": "The id of something media_find returned"},
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
    return note


register(Skill("dj", "DJ", "Play music from your Orpheus library and control it by voice.",
               _tools, note=_note))
