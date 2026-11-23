"""The DJ: Mira's music skill, on the Orpheus media server (SKILLS_PLAN.md, Skill 2).

What she can do depends on where the pairing stands, so the model is only offered the
tools that can work (and only pays prompt tokens for those):

  unpaired / pairing pending  ->  media_connect
  paired                      ->  media_find, media_status

``media_play`` / ``media_control`` arrive with Orpheus Phase 5 (remote control), which the
server doesn't have yet; until then she can search the library and say what's there,
but not start music. Pairing needs a human: Orpheus gives a 6-digit code and someone
approves it on the server (``orpheus pair approve <code>``), so a prompt-injected card
can start a request but never finish one.
"""
from __future__ import annotations

from datetime import datetime, timezone

from services.skills import SkillContext, Skill, Tool, error, register
from services.skills import orpheus_media as om

# Music only: Orpheus also holds films and shows, and an unrestricted search for a band it
# doesn't have came back with movies ("sigur ros" -> Shazam!). Playlists join when Orpheus
# Phase 5 adds them to resolve; today the server refuses the kind (400).
KINDS = ["artist", "album", "track"]
# Below this a candidate only sounds a little alike ("sigur ros" -> "Sugar Mama", 0.34;
# "tom sawyer by rush" -> Tom Petty, 0.42), while real hits scored 0.54-1.0 on the live
# library (2026-11-23). Under it she says it isn't there rather than offer a lookalike.
MIN_SCORE = 0.5


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


async def _find(ctx: SkillContext, args: dict) -> dict:
    state = om.load(ctx.root)
    if not om.paired(state):
        return error("NOT_PAIRED", "Not connected to the music server yet.")
    try:
        out = await om.resolve(state["url"], state["token"], args["query"], args.get("kinds") or KINDS)
    except om.OrpheusError as e:
        return error(e.code, str(e))
    found = out.get("candidates") or []
    strong = [c for c in found if float(c.get("score") or 0) >= MIN_SCORE
              and c.get("kind") in KINDS]
    cands = [{"id": c.get("item_id"), "kind": c.get("kind"), "name": c.get("display") or c.get("title"),
              "playable": c.get("playable", True)} for c in strong[:5]]
    if not cands:
        return error("MEDIA_NOT_FOUND", f"Nothing in the music library matches {args['query']!r}.")
    res = {"ok": True, "candidates": cands, "ambiguous": bool(out.get("needs_disambiguation"))}
    # The server's own question names its candidates; ask it only if they all survived.
    if out.get("disambiguation_prompt") and len(strong) == len(found):
        res["ask"] = out["disambiguation_prompt"]
    res["can_play"] = False                        # until Orpheus Phase 5: say so, don't pretend
    return res


async def _status(ctx: SkillContext, args: dict) -> dict:
    state = om.load(ctx.root)
    if not om.paired(state):
        return error("NOT_PAIRED", "Not connected to the music server yet.")
    try:
        devs = await om.devices(state["url"], state["token"])
    except om.OrpheusError as e:
        return error(e.code, str(e))
    players = [{"name": d.get("name"), "online": d.get("online"), "playing": d.get("now_playing")}
               for d in devs if (d.get("role") or "player") == "player"]
    return {"ok": True, "players": players, "can_play": False}


CONNECT = Tool(
    "media_connect",
    "Connect to the user's music server (Orpheus). Use when the user asks to set up or connect "
    "the music, or when another music tool says it isn't connected. Read the code it returns to the user.",
    {"type": "object", "properties": {}}, _connect, status="Connecting to the music server…")
FIND = Tool(
    "media_find",
    "Search the user's music library for an artist, album, track or playlist, as they said it. "
    "If the result is ambiguous, ask which one they meant.",
    {"type": "object", "properties": {
        "query": {"type": "string", "maxLength": 200, "description": "What to look for, as the user said it"},
        "kinds": {"type": "array", "maxItems": 4, "items": {"type": "string", "enum": KINDS}}},
     "required": ["query"]}, _find, status="Searching the library…")
STATUS = Tool(
    "media_status",
    "Which music players exist and what each is playing now.",
    {"type": "object", "properties": {}}, _status, status="Checking the players…")


def _tools(ctx: SkillContext) -> list[Tool]:
    return [FIND, STATUS] if om.paired(om.load(ctx.root)) else [CONNECT]


def _note(ctx: SkillContext) -> str:
    if om.paired(om.load(ctx.root)):
        return ("Music: you can search the user's library and see the players, but you can't start "
                "or control playback yet. Say so plainly if asked to play something.")
    return ""


register(Skill("dj", "DJ", "Find music in your Orpheus library (playback comes with Orpheus Phase 5).",
               _tools, note=_note))
