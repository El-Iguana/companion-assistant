"""Companion — FastAPI app.

A character-card AI companion. Talks to KoboldCPP (text) and optionally ComfyUI
(portraits/expressions) on the shared `sillytavern-net` Podman network.

No database: cards, personas, and sessions are JSON files under data/.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, StreamingResponse, Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from models.schema import (
    CharacterCard, Persona, ChatSession, ChatMessage, ChatRequest, Relationship, RelationshipPreset,
    AppSettings,
)
from services.kobold import KoboldClient
from services.comfyui import ComfyUIClient
from services import card_io, prompt_builder, memory, expressions, relationship, tts, stt

# --------------------------------------------------------------------------
BASE = Path(__file__).parent
DATA = BASE / "data"
CARDS, PERSONAS, SESSIONS = DATA / "characters", DATA / "personas", DATA / "sessions"
RELATIONSHIPS = DATA / "relationships"
PRESETS = DATA / "presets"
SETTINGS = DATA / "settings"
for d in (CARDS, PERSONAS, SESSIONS, RELATIONSHIPS, PRESETS, DATA / "expressions", SETTINGS):
    d.mkdir(parents=True, exist_ok=True)

KOBOLD_URL = os.environ.get("KOBOLD_URL", "http://koboldcpp:5001")
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://host.containers.internal:8188")
kobold = KoboldClient(KOBOLD_URL)
comfy = ComfyUIClient(COMFYUI_URL)

app = FastAPI(title="Companion")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


# --------------------------------------------------------------------------
# Tiny JSON file store
# --------------------------------------------------------------------------
def _save(model, folder: Path) -> None:
    (folder / f"{model.id}.json").write_text(model.model_dump_json(indent=2))


def _load(folder: Path, id_: str, cls):
    p = folder / f"{id_}.json"
    if not p.exists():
        return None
    return cls.model_validate_json(p.read_text())


def _list(folder: Path, cls):
    out = []
    for p in sorted(folder.glob("*.json")):
        try:
            out.append(cls.model_validate_json(p.read_text()))
        except Exception:
            pass
    return out


def _load_settings() -> AppSettings:
    p = SETTINGS / "app.json"
    if p.exists():
        try:
            return AppSettings.model_validate_json(p.read_text())
        except Exception:
            pass
    return AppSettings()


def _save_settings(s: AppSettings) -> None:
    (SETTINGS / "app.json").write_text(s.model_dump_json(indent=2))


def _load_relationship(character_id: str, persona_id: str) -> Relationship:
    p = RELATIONSHIPS / f"{character_id}__{persona_id}.json"
    if p.exists():
        return Relationship.model_validate_json(p.read_text())
    return Relationship(character_id=character_id, persona_id=persona_id)


def _save_relationship(rel: Relationship) -> None:
    (RELATIONSHIPS / f"{rel.character_id}__{rel.persona_id}.json").write_text(
        rel.model_dump_json(indent=2))


def _clear_other_default_personas(keep_id: str) -> None:
    for p in _list(PERSONAS, Persona):
        if p.id != keep_id and p.is_default:
            p.is_default = False
            _save(p, PERSONAS)


def _default_persona() -> Persona:
    for p in _list(PERSONAS, Persona):
        if p.is_default:
            return p
    p = Persona(name="User", is_default=True)
    _save(p, PERSONAS)
    return p


@app.on_event("startup")
async def _startup():
    _default_persona()


@app.on_event("shutdown")
async def _shutdown():
    await kobold.close()
    await comfy.close()


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
def _asset_ver(rel: str) -> int:
    """mtime of a static asset — appended as ?v= so rebuilds bust the browser cache."""
    try:
        return int((BASE / "static" / rel).stat().st_mtime)
    except OSError:
        return 0


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "css_v": _asset_ver("css/style.css"),
        "js_v": _asset_ver("js/app.js"),
    })


@app.get("/api/status")
async def status():
    return await kobold.check_status()


@app.post("/api/tts")
async def tts_synth(body: dict):
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    try:
        wav = await asyncio.to_thread(tts.synth_wav, text,
                                      body.get("voice", "af_sarah"), float(body.get("speed", 1.0)))
    except Exception as e:
        raise HTTPException(500, f"tts failed: {e}")
    return Response(content=wav, media_type="audio/wav")


@app.get("/api/tts/voices")
async def tts_voices():
    return {"voices": await asyncio.to_thread(tts.voices)}


@app.post("/api/stt")
async def stt_transcribe(file: UploadFile = File(...)):
    audio = await file.read()
    if not audio:
        raise HTTPException(400, "empty audio")
    try:
        text = await asyncio.to_thread(stt.transcribe, audio)
    except Exception as e:
        raise HTTPException(500, f"stt failed: {e}")
    return {"text": text}


@app.get("/api/settings")
async def get_settings():
    return _load_settings().model_dump()


@app.put("/api/settings")
async def put_settings(body: dict):
    data = _load_settings().model_dump()
    data.update({k: v for k, v in body.items() if k in data})   # ignore unknown keys
    s = AppSettings(**data)
    _save_settings(s)
    return s.model_dump()


# --------------------------------------------------------------------------
# Character cards
# --------------------------------------------------------------------------
@app.get("/api/cards")
async def list_cards():
    # Trim the heavy expression sprite blobs from the list; expose a flag instead.
    out = []
    for c in _list(CARDS, CharacterCard):
        d = c.model_dump(exclude={"expressions"})
        d["has_expressions"] = bool(c.expressions)
        out.append(d)
    return out


@app.get("/api/cards/{card_id}")
async def get_card(card_id: str):
    c = _load(CARDS, card_id, CharacterCard)
    if not c:
        raise HTTPException(404, "card not found")
    return c.model_dump()


@app.post("/api/cards/import")
async def import_card(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        card = card_io.import_card_bytes(raw, file.filename or "")
    except Exception as e:
        raise HTTPException(400, f"could not parse card: {e}")
    _save(card, CARDS)
    return card.model_dump()


@app.post("/api/cards")
async def create_card(card: CharacterCard):
    _save(card, CARDS)
    return card.model_dump()


@app.put("/api/cards/{card_id}")
async def update_card(card_id: str, card: CharacterCard):
    card.id = card_id
    _save(card, CARDS)
    return card.model_dump()


@app.delete("/api/cards/{card_id}")
async def delete_card(card_id: str):
    p = CARDS / f"{card_id}.json"
    if p.exists():
        p.unlink()
    return {"ok": True}


@app.get("/api/cards/{card_id}/export")
async def export_card(card_id: str, version: int = 2):
    c = _load(CARDS, card_id, CharacterCard)
    if not c:
        raise HTTPException(404, "card not found")
    png = card_io.export_card_png(c, version=version)
    fname = (c.name or "card").replace(" ", "_") + ".png"
    return Response(content=png, media_type="image/png",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# --------------------------------------------------------------------------
# ComfyUI + expression sprites
# --------------------------------------------------------------------------
@app.get("/api/comfyui/status")
async def comfyui_status():
    return await comfy.check_status()


@app.get("/api/comfyui/checkpoints")
async def comfyui_checkpoints():
    try:
        return {"checkpoints": await comfy.list_checkpoints()}
    except Exception as e:
        raise HTTPException(502, f"ComfyUI unreachable: {e}")


@app.get("/api/expressions/emotions")
async def expression_emotions():
    return {"core": expressions.CORE_EMOTIONS, "full": expressions.FULL_EMOTIONS}


@app.post("/api/cards/{card_id}/expressions")
async def generate_expressions(card_id: str, body: dict):
    """Generate an expression sprite per emotion via ComfyUI and store them on the card."""
    card = _load(CARDS, card_id, CharacterCard)
    if not card:
        raise HTTPException(404, "card not found")
    checkpoint = body.get("checkpoint")
    if not checkpoint:
        raise HTTPException(400, "checkpoint is required")
    if "appearance" in body:
        card.appearance = body["appearance"]

    # Base source: the uploaded portrait (img2img) vs. txt2img. Persist the choice so
    # single-sprite regen from the gallery stays consistent with the batch.
    from_upload = bool(body.get("from_upload")) and bool(card.image)
    card.expr_from_upload = from_upload
    denoise = min(0.9, max(0.2, float(body.get("denoise", 0.55))))
    card.expr_denoise = denoise

    # Resolve which emotions to generate: explicit list > preset ("core"|"full").
    emotions = body.get("emotions")
    if not emotions:
        emotions = (expressions.FULL_EMOTIONS if body.get("preset") == "full"
                    else expressions.CORE_EMOTIONS)

    try:
        sprites = await expressions.generate_expressions(
            comfy, card, checkpoint=checkpoint,
            style=body.get("style", "anime"),
            emotions=emotions,
            transparent=body.get("transparent", True),
            from_upload=from_upload,
            denoise=denoise,
        )
    except Exception as e:
        raise HTTPException(502, f"generation failed: {e}")

    # Bulk generate REPLACES the whole set (clears old sprites first) so switching
    # between Core/Full starts fresh — no stale sprites left over. Single-sprite
    # regen (below) still merges.
    card.expressions = sprites
    _save(card, CARDS)
    return {"generated": list(sprites.keys()), "total": len(card.expressions)}


@app.get("/api/cards/{card_id}/expression/{emotion}")
async def get_expression(card_id: str, emotion: str):
    """Return one expression sprite as a PNG (falls back to neutral, then card art)."""
    card = _load(CARDS, card_id, CharacterCard)
    if not card:
        raise HTTPException(404, "card not found")
    b64 = card.expressions.get(emotion) or card.expressions.get("neutral") or card.image
    if not b64:
        raise HTTPException(404, "no sprite")
    import base64
    return Response(content=base64.b64decode(b64), media_type="image/png")


@app.get("/api/cards/{card_id}/expressions")
async def list_expressions(card_id: str):
    """Which emotions this card currently has sprites for (names only, no blobs)."""
    card = _load(CARDS, card_id, CharacterCard)
    if not card:
        raise HTTPException(404, "card not found")
    return {"emotions": sorted(card.expressions.keys())}


@app.post("/api/cards/{card_id}/expression/{emotion}")
async def regenerate_expression(card_id: str, emotion: str, body: dict):
    """(Re)generate a single expression sprite."""
    card = _load(CARDS, card_id, CharacterCard)
    if not card:
        raise HTTPException(404, "card not found")
    checkpoint = body.get("checkpoint")
    if not checkpoint:
        raise HTTPException(400, "checkpoint is required")
    if "appearance" in body:
        card.appearance = body["appearance"]
    try:
        sprites = await expressions.generate_expressions(
            comfy, card, checkpoint=checkpoint, style=body.get("style", "anime"),
            emotions=[emotion], transparent=body.get("transparent", True),
            from_upload=card.expr_from_upload, denoise=card.expr_denoise,   # match the batch
        )
    except Exception as e:
        raise HTTPException(502, f"generation failed: {e}")
    card.expressions.update(sprites)
    _save(card, CARDS)
    return {"ok": True, "emotion": emotion}


@app.delete("/api/cards/{card_id}/expression/{emotion}")
async def delete_expression(card_id: str, emotion: str):
    card = _load(CARDS, card_id, CharacterCard)
    if not card:
        raise HTTPException(404, "card not found")
    card.expressions.pop(emotion, None)
    _save(card, CARDS)
    return {"ok": True, "total": len(card.expressions)}


# --------------------------------------------------------------------------
# Personas
# --------------------------------------------------------------------------
@app.get("/api/personas")
async def list_personas():
    return [p.model_dump() for p in _list(PERSONAS, Persona)]


@app.post("/api/personas")
async def create_persona(persona: Persona):
    _save(persona, PERSONAS)
    if persona.is_default:
        _clear_other_default_personas(persona.id)
    return persona.model_dump()


@app.put("/api/personas/{persona_id}")
async def update_persona(persona_id: str, persona: Persona):
    persona.id = persona_id
    _save(persona, PERSONAS)
    if persona.is_default:
        _clear_other_default_personas(persona.id)
    return persona.model_dump()


@app.delete("/api/personas/{persona_id}")
async def delete_persona(persona_id: str):
    p = PERSONAS / f"{persona_id}.json"
    if p.exists():
        p.unlink()
    return {"ok": True}


# --------------------------------------------------------------------------
# Relationship
# --------------------------------------------------------------------------
@app.get("/api/relationship")
async def get_relationship(character_id: str, persona_id: str):
    rel = _load_relationship(character_id, persona_id)
    card = _load(CARDS, character_id, CharacterCard)
    s = relationship.summary(rel)
    s["power_dynamic"] = rel.power_dynamic if rel.power_dynamic is not None else (card.power_dynamic if card else 0.0)
    s["social_acceptance"] = rel.social_acceptance if rel.social_acceptance is not None else (card.social_acceptance if card else 50.0)
    s["context_overridden"] = rel.power_dynamic is not None or rel.social_acceptance is not None
    return s


@app.put("/api/relationship")
async def set_relationship(body: dict):
    """Per-relationship override of context (power_dynamic / social_acceptance). Null clears -> card default."""
    cid, pid = body.get("character_id"), body.get("persona_id")
    if not cid or not pid:
        raise HTTPException(400, "character_id and persona_id required")
    rel = _load_relationship(cid, pid)
    if "power_dynamic" in body:
        rel.power_dynamic = body["power_dynamic"]
    if "social_acceptance" in body:
        rel.social_acceptance = body["social_acceptance"]
    _save_relationship(rel)
    return relationship.summary(rel)


# --------------------------------------------------------------------------
# Relationship presets (reusable scenario seeds)
# --------------------------------------------------------------------------
@app.get("/api/presets")
async def list_presets():
    return [p.model_dump() for p in _list(PRESETS, RelationshipPreset)]


@app.post("/api/presets")
async def create_preset(preset: RelationshipPreset):
    _save(preset, PRESETS)
    return preset.model_dump()


@app.get("/api/presets/{preset_id}")
async def get_preset(preset_id: str):
    p = _load(PRESETS, preset_id, RelationshipPreset)
    if not p:
        raise HTTPException(404, "preset not found")
    return p.model_dump()


@app.put("/api/presets/{preset_id}")
async def update_preset(preset_id: str, preset: RelationshipPreset):
    preset.id = preset_id
    _save(preset, PRESETS)
    return preset.model_dump()


@app.delete("/api/presets/{preset_id}")
async def delete_preset(preset_id: str):
    p = PRESETS / f"{preset_id}.json"
    if p.exists():
        p.unlink()
    return {"ok": True}


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
@app.get("/api/sessions")
async def list_sessions():
    return [
        {"id": s.id, "title": s.title, "character_id": s.character_id,
         "updated_at": s.updated_at, "messages": len(s.messages)}
        for s in _list(SESSIONS, ChatSession)
    ]


@app.post("/api/sessions")
async def create_session(body: dict):
    card = _load(CARDS, body.get("character_id", ""), CharacterCard)
    if not card:
        raise HTTPException(400, "character_id required and must exist")
    persona = _load(PERSONAS, body.get("persona_id", ""), Persona) or _default_persona()

    session = ChatSession(
        title=card.name, character_id=card.id, persona_id=persona.id,
    )

    # Scenario preset: seed a session-scoped relationship + optional opening note.
    scenario_note = ""
    preset_id = body.get("preset_id")
    if preset_id and body.get("mode") == "scenario":
        preset = _load(PRESETS, preset_id, RelationshipPreset)
        if preset:
            session.relationship = relationship.from_preset(preset, card.id, persona.id)
            scenario_note = preset.scenario_note or ""

    # Seed the opening greeting (macros substituted for display).
    greeting_idx = int(body.get("greeting_index", 0))
    greetings = [card.first_mes] + list(card.alternate_greetings)
    greeting = greetings[greeting_idx] if 0 <= greeting_idx < len(greetings) else card.first_mes
    if not (greeting and greeting.strip()):
        # Fall back to the first non-empty greeting, then to a generic opener,
        # so a card that ships without a first_mes still opens the chat.
        greeting = next((g for g in greetings if g and g.strip()), "")
    if not (greeting and greeting.strip()):
        greeting = f"*{card.name} looks up as you arrive, ready to talk.*"
    if scenario_note:
        greeting = f"*{scenario_note}*\n\n{greeting}"
    session.messages.append(ChatMessage(
        role="char",
        content=prompt_builder.substitute_macros(greeting, card.name, persona.name),
    ))
    _save(session, SESSIONS)
    return session.model_dump()


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    s = _load(SESSIONS, session_id, ChatSession)
    if not s:
        raise HTTPException(404, "session not found")
    d = s.model_dump()
    if s.relationship is not None:                 # scenario session -> its own summary
        d["relationship_summary"] = relationship.summary(s.relationship)
    return d


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    p = SESSIONS / f"{session_id}.json"
    if p.exists():
        p.unlink()
    return {"ok": True}


@app.put("/api/sessions/{session_id}/swipe")
async def set_swipe(session_id: str, body: dict):
    """Select which stored alternate (swipe) of the last char message is active."""
    s = _load(SESSIONS, session_id, ChatSession)
    if not s:
        raise HTTPException(404, "session not found")
    idx = int(body.get("swipe_index", 0))
    expr = None
    for m in reversed(s.messages):
        if m.role == "char":
            if 0 <= idx < len(m.swipes):
                m.swipe_index = idx
                m.content = m.swipes[idx]   # keep content in sync with shown swipe
                while len(m.swipe_expressions) < len(m.swipes):
                    m.swipe_expressions.append("")
                # Reclassify this swipe if not already cached (and the card has sprites).
                if not m.swipe_expressions[idx]:
                    card = _load(CARDS, s.character_id, CharacterCard)
                    if card and card.expressions:
                        try:
                            m.swipe_expressions[idx] = await expressions.classify(
                                kobold, m.content, labels=list(card.expressions))
                        except Exception:
                            m.swipe_expressions[idx] = "neutral"
                expr = m.swipe_expressions[idx] or m.expression
                m.expression = expr
                _save(s, SESSIONS)
            break
    return {"ok": True, "expression": expr}


@app.delete("/api/sessions/{session_id}/messages")
async def clear_messages(session_id: str):
    s = _load(SESSIONS, session_id, ChatSession)
    if not s:
        raise HTTPException(404, "session not found")
    # Keep the opening greeting; drop the rest and reset memory.
    s.messages = s.messages[:1]
    s.memory.summary = ""
    s.memory.last_summarized_index = 0
    _save(s, SESSIONS)
    return {"ok": True}


# --------------------------------------------------------------------------
# Chat — SSE streaming
# --------------------------------------------------------------------------
def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@app.post("/api/chat")
async def chat(req: ChatRequest):
    session = _load(SESSIONS, req.session_id, ChatSession)
    if not session:
        raise HTTPException(404, "session not found")
    card = _load(CARDS, session.character_id, CharacterCard)
    persona = _load(PERSONAS, session.persona_id, Persona) or _default_persona()
    if not card:
        raise HTTPException(400, "session has no character")

    if req.regenerate:
        # Re-roll: pop the last char turn (keep it + its cached emotions as swipes).
        prev_swipes, prev_exprs = [], []
        if session.messages and session.messages[-1].role == "char":
            last = session.messages.pop()
            prev_swipes = last.swipes or [last.content]
            prev_exprs = list(last.swipe_expressions)
        _pending_swipes, _pending_exprs = prev_swipes, prev_exprs
    else:
        _pending_swipes, _pending_exprs = [], []
        session.messages.append(ChatMessage(role="user", content=req.input_text))

    scenario = session.relationship is not None
    rel = session.relationship if scenario else _load_relationship(session.character_id, persona.id)
    power = rel.power_dynamic if rel.power_dynamic is not None else card.power_dynamic
    social = rel.social_acceptance if rel.social_acceptance is not None else card.social_acceptance
    rel_note = relationship.descriptor(rel, card.name, persona.name, power, social)
    if not req.regenerate and session.pending_milestone:
        rel_note += " MILESTONE MOMENT — " + session.pending_milestone
        session.pending_milestone = ""
    messages = prompt_builder.build_messages(card, persona, session, relationship_note=rel_note,
                                             stage_rank=relationship._stage_rank(rel))
    params = prompt_builder.kobold_params(session)
    # Stop if the model starts writing {{user}}'s turn (prevents multi-turn / speaking for the user).
    params["stop"] = list(params.get("stop") or []) + [f"\n{persona.name}:", f"\n{persona.name} —"]

    async def gen():
        acc = ""
        try:
            async for tok in kobold.chat_generate_stream(messages, params):
                acc += tok
                yield _sse({"token": tok})
        except Exception as e:
            yield _sse({"error": str(e)})
            return

        reply = acc.strip()
        swipes = (_pending_swipes + [reply]) if _pending_swipes else [reply]
        msg = ChatMessage(role="char", content=reply, swipes=swipes,
                          swipe_index=len(swipes) - 1)

        # Classify emotion for the expression sprite — only if this card has sprites.
        new_expr = ""
        if card.expressions:
            try:
                new_expr = await expressions.classify(kobold, reply, labels=list(card.expressions))
            except Exception:
                new_expr = "neutral"
        msg.expression = new_expr or None

        # Update the running valence/mood (inertia): neutral holds; a clear emotion
        # advances the mood and nudges valence via EMA. Foundation for a relationship score.
        if new_expr and new_expr != "neutral":
            a = 0.35
            v = expressions.valence_of(new_expr)
            session.mood.valence = round(max(-1.0, min(1.0, session.mood.valence * (1 - a) + v * a)), 3)
            session.mood.mood = new_expr

        # Advance the persistent relationship (skip on re-rolls to avoid double-counting).
        milestone_toasts = []
        if not req.regenerate:
            relationship.update(rel, new_expr, power, social)
            # Escalation: once physical intimacy has begun and she's positively engaged, actually
            # doing acts nudges willingness toward the next rung (social acceptance modulates the pace).
            if relationship.physical_tier(rel)[0] >= 1 and expressions.valence_of(new_expr) >= 0:
                try:
                    act = await relationship.classify_activity(kobold, reply)
                except Exception:
                    act = 0
                relationship.escalate(rel, act, social)
            for m in relationship.newly_reached(rel):
                rel.milestones.append(m["id"])
                milestone_toasts.append(m["toast"].replace("{char}", card.name).replace("{user}", persona.name))
                beat = m["beat"].replace("{char}", card.name).replace("{user}", persona.name)
                session.pending_milestone = (session.pending_milestone + " " + beat).strip()
            if scenario:
                session.relationship = rel      # persisted with the session
            else:
                _save_relationship(rel)

        # Keep per-swipe emotions aligned with swipes (older swipes cached from before).
        exprs = list(_pending_exprs)
        while len(exprs) < len(_pending_swipes):
            exprs.append("")
        exprs.append(new_expr)
        msg.swipe_expressions = exprs

        session.messages.append(msg)

        # Best-effort long-term memory maintenance.
        try:
            await memory.summarize(kobold, session, card, persona)
        except Exception:
            pass

        _save(session, SESSIONS)
        done_evt = {"done": True, "message_id": msg.id, "expression": msg.expression,
                    "mood": session.mood.mood, "valence": session.mood.valence,
                    "relationship": relationship.summary(rel)}
        if milestone_toasts:
            done_evt["milestones"] = milestone_toasts
        yield _sse(done_evt)

    return StreamingResponse(gen(), media_type="text/event-stream")


# --------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=7861)
