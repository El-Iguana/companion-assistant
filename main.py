"""Pantheon — FastAPI app.

A character-card AI companion. Talks to KoboldCPP (text) and optionally ComfyUI
(portraits/expressions) on the shared `ai-net` Podman network.

No database: cards, personas, and sessions are JSON files under data/. Multi-user:
each account has its own isolated library under data/u/<user_id>/… ; auth is a
signed HttpOnly cookie (see services/auth.py). Only admins manage users.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Depends
from fastapi.responses import (
    StreamingResponse, Response, FileResponse,
)

from models.schema import (
    CharacterCard, Persona, ChatSession, ChatMessage, ChatRequest, Relationship, RelationshipPreset,
    SceneRequest,
    AppSettings, AssistantMemory, CharacterMemory, MemoryFact, User,
)
from services.kobold import KoboldClient, ExprEvent
from services.comfyui import ComfyUIClient
from services import card_io, context, prompt_builder, memory, assistant_memory, expressions, relationship, tts, stt, auth, vrm

# --------------------------------------------------------------------------
BASE = Path(__file__).parent
# PANTHEON_DATA_DIR points a dev or test instance at a throwaway library. Unset in the
# container, where data/ is the mounted volume.
DATA = Path(os.environ.get("PANTHEON_DATA_DIR") or (BASE / "data"))
AUTH = DATA / "auth"
USERS = AUTH / "users"
UROOT = DATA / "u"                       # per-user library roots: data/u/<uid>/<subdir>/
SUBDIRS = ("characters", "personas", "sessions", "relationships", "presets", "settings",
           "memories", "assistant")
# Old flat single-user dirs — migrated into the first admin on first run.
LEGACY = {name: DATA / name for name in SUBDIRS}
for d in (AUTH, USERS, UROOT):
    d.mkdir(parents=True, exist_ok=True)

KOBOLD_URL = os.environ.get("KOBOLD_URL", "http://koboldcpp:5001")
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://comfyui:8188")
kobold = KoboldClient(KOBOLD_URL)
comfy = ComfyUIClient(COMFYUI_URL)

# The route table is no longer served (2026-10-26, pytincture switch-over): service.py
# serves the page, whose BFF classes (appcode/bff/) and plain routes (pantheon_web/) call
# these handlers directly with the signed-in user's Ctx. The decorators remain as the
# handlers' contract and for the unit tests; the classic page and its cookie login are gone.
app = FastAPI(title="Pantheon")


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


# --------------------------------------------------------------------------
# Auth: secret, user store, per-user context, dependencies
# --------------------------------------------------------------------------
_SECRET: Optional[bytes] = None


def _secret() -> bytes:
    global _SECRET
    if _SECRET is None:
        f = AUTH / "secret"
        if not f.exists():
            f.write_text(secrets.token_hex(32))
        _SECRET = bytes.fromhex(f.read_text().strip())
    return _SECRET


def _save_user(u: User) -> None:
    (USERS / f"{u.id}.json").write_text(u.model_dump_json(indent=2))


def _all_users() -> list[User]:
    return _list(USERS, User)


def _user_by_id(uid: str) -> Optional[User]:
    return _load(USERS, uid, User) if uid else None


def _user_by_name(name: str) -> Optional[User]:
    name = (name or "").strip().lower()
    return next((u for u in _all_users() if u.username.lower() == name), None) if name else None


@dataclass
class Ctx:
    """Per-request handle to the authenticated user and their private library dirs."""
    user: User
    cards: Path
    personas: Path
    sessions: Path
    relationships: Path
    presets: Path
    settings: Path
    memories: Path
    assistant: Path


def _user_root(uid: str) -> Path:
    root = UROOT / uid
    for s in SUBDIRS:
        (root / s).mkdir(parents=True, exist_ok=True)
    return root


def _ctx_for(user: User) -> Ctx:
    r = _user_root(user.id)
    return Ctx(user, r / "characters", r / "personas", r / "sessions",
               r / "relationships", r / "presets", r / "settings", r / "memories",
               r / "assistant")


async def current_user(request: Request) -> User:
    tok = request.cookies.get("session")
    uid = auth.read_token(_secret(), tok) if tok else None
    u = _user_by_id(uid) if uid else None
    if not u:
        raise HTTPException(401, "authentication required")
    return u


async def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    return user


async def get_ctx(user: User = Depends(current_user)) -> Ctx:
    return _ctx_for(user)


def _seed_password() -> tuple[str, bool]:
    """The first-run admin password: given, or minted. Never a constant.

    A literal here would be the known default admin credential of every install of
    a shared repo — and the container publishes 7861 on all interfaces, so that is
    not only a local concern. Returns (password, generated).
    """
    given = os.environ.get("COMPANION_ADMIN_PASSWORD", "").strip()
    if given:
        return given, False
    return secrets.token_urlsafe(12), True


def _announce_seed_password(password: str) -> None:
    """Put a generated password where a first run can actually find it.

    The container starts detached, so stdout alone means `podman logs`; write it
    to a file too, owner-only, so it survives log rotation and a scrolled console.
    """
    f = AUTH / "INITIAL_PASSWORD"
    try:
        f.write_text(
            f"{password}\n\n"
            "First-run password for the ADMIN account, generated because\n"
            "COMPANION_ADMIN_PASSWORD was not set. Change it in the app\n"
            "(userbar -> your account), then delete this file.\n"
        )
        os.chmod(f, 0o600)
        where = f" (also written to {f})"
    except OSError:
        where = ""
    banner = "=" * 62
    print(f"\n{banner}\n  First run: created admin account ADMIN\n"
          f"  password: {password}\n"
          f"  Change it after logging in{where}.\n{banner}\n", flush=True)


def _seed_and_migrate() -> None:
    """First run: create the ADMIN account and move any legacy flat data into it."""
    if _all_users():
        return
    password, generated = _seed_password()
    salt = auth.new_salt()
    admin = User(username="ADMIN", role="admin", salt=salt,
                 password_hash=auth.hash_password(password, salt))
    _save_user(admin)
    if generated:
        _announce_seed_password(password)
    root = _user_root(admin.id)
    for name, src in LEGACY.items():
        if not src.exists():
            continue
        for f in src.glob("*.json"):
            target = root / name / f.name
            if not target.exists():
                try:
                    shutil.move(str(f), str(target))
                except Exception:
                    pass


_seed_and_migrate()


# --------------------------------------------------------------------------
# Per-user settings / relationship / persona helpers (take the request Ctx)
# --------------------------------------------------------------------------
def _load_settings(cur: Ctx) -> AppSettings:
    p = cur.settings / "app.json"
    if p.exists():
        try:
            return AppSettings.model_validate_json(p.read_text())
        except Exception:
            pass
    return AppSettings()


def _save_settings(cur: Ctx, s: AppSettings) -> None:
    (cur.settings / "app.json").write_text(s.model_dump_json(indent=2))


async def _context_tokens(st: AppSettings) -> int:
    """The window to budget against: Settings' choice, capped at what KoboldCPP really has."""
    return context.effective(st.context_limit, await kobold.context_limit())


def _load_relationship(cur: Ctx, character_id: str, persona_id: str) -> Relationship:
    p = cur.relationships / f"{character_id}__{persona_id}.json"
    if p.exists():
        return Relationship.model_validate_json(p.read_text())
    return Relationship(character_id=character_id, persona_id=persona_id)


def _save_relationship(cur: Ctx, rel: Relationship) -> None:
    (cur.relationships / f"{rel.character_id}__{rel.persona_id}.json").write_text(
        rel.model_dump_json(indent=2))


def _load_character_memory(cur: Ctx, character_id: str, persona_id: str) -> CharacterMemory:
    """Cross-session memory for a pairing. Scoped and named exactly like the
    relationship pair above — same {cid}__{pid}.json convention on purpose."""
    p = cur.memories / f"{character_id}__{persona_id}.json"
    if p.exists():
        try:
            return CharacterMemory.model_validate_json(p.read_text())
        except Exception:
            pass          # unreadable/older shape -> start fresh rather than 500 a chat turn
    return CharacterMemory(character_id=character_id, persona_id=persona_id)


def _memory_index_path(cur: Ctx, character_id: str, persona_id: str):
    """Vector index for a pairing (Phase 4). Sits beside the CharacterMemory
    JSON with a non-.json suffix on purpose, so any future `glob("*.json")` over
    this directory can't mistake it for a model file."""
    return cur.memories / f"{character_id}__{persona_id}.vec.npz"


def _save_character_memory(cur: Ctx, mem: CharacterMemory) -> None:
    (cur.memories / f"{mem.character_id}__{mem.persona_id}.json").write_text(
        mem.model_dump_json(indent=2))


def _load_assistant_memory(cur: Ctx, character_id: str, persona_id: str) -> AssistantMemory:
    """Assistant memory for a pairing. Same {cid}__{pid} convention as the others,
    a different directory — an assistant's store must not be reachable by anything
    reading the companion one, which is the whole reason the pipeline is split."""
    p = cur.assistant / f"{character_id}__{persona_id}.json"
    if p.exists():
        try:
            return AssistantMemory.model_validate_json(p.read_text())
        except Exception:
            pass          # unreadable/older shape -> start fresh rather than 500 a chat turn
    return AssistantMemory(character_id=character_id, persona_id=persona_id)


def _save_assistant_memory(cur: Ctx, mem: AssistantMemory) -> None:
    (cur.assistant / f"{mem.character_id}__{mem.persona_id}.json").write_text(
        mem.model_dump_json(indent=2))


def _forget_swept_session(cur: Ctx, session: ChatSession) -> None:
    """Drop `session`'s fold high-water mark from its pairing's long-term store.

    `swept_sessions[id]` is an INDEX into session.messages, so it only means
    anything while that list is intact. Clearing a chat leaves a mark pointing
    past the new end: every later fold sees `upto <= start`, silently records
    nothing, and once the chat grows back past the old length it resumes
    mid-conversation, permanently skipping everything before that point.
    Deleting a session leaves the mark behind forever instead.

    What was already folded into `summary` stays — long-term memory is durable
    across chats by design, and clearing one chat is not a request to forget it.
    """
    if session.relationship is not None or not session.character_id:
        return                    # scenario sessions are never swept (see chat())
    persona = _load(cur.personas, session.persona_id, Persona) or _default_persona(cur)
    card = _load(cur.cards, session.character_id, CharacterCard)
    if card and getattr(card, "chat_mode", "roleplay") == "assistant":
        am = _load_assistant_memory(cur, session.character_id, persona.id)
        if am.swept_sessions.pop(session.id, None) is not None:
            am.updated_at = datetime.now().isoformat()
            _save_assistant_memory(cur, am)
        return
    mem = _load_character_memory(cur, session.character_id, persona.id)
    if mem.swept_sessions.pop(session.id, None) is not None:
        mem.updated_at = datetime.now().isoformat()
        _save_character_memory(cur, mem)


def _clear_other_default_personas(cur: Ctx, keep_id: str) -> None:
    for p in _list(cur.personas, Persona):
        if p.id != keep_id and p.is_default:
            p.is_default = False
            _save(p, cur.personas)


def _default_persona(cur: Ctx) -> Persona:
    for p in _list(cur.personas, Persona):
        if p.is_default:
            return p
    p = Persona(name="User", is_default=True)
    _save(p, cur.personas)
    return p


@app.on_event("shutdown")
async def _shutdown():
    await kobold.close()
    await comfy.close()


# --------------------------------------------------------------------------
# Accounts: password change + admin user management (sign-in is pytincture's, via
# pantheon_web/auth.py)
# --------------------------------------------------------------------------
def _safe_user(u: User) -> dict:
    return {"id": u.id, "username": u.username, "role": u.role, "created_at": u.created_at}


@app.post("/api/me/password")
async def change_my_password(body: dict, user: User = Depends(current_user)):
    if not auth.verify_password(body.get("current", ""), user.salt, user.password_hash):
        raise HTTPException(403, "current password incorrect")
    pw = body.get("password") or ""
    if len(pw) < 4:
        raise HTTPException(400, "password too short (min 4)")
    user.salt = auth.new_salt()
    user.password_hash = auth.hash_password(pw, user.salt)
    _save_user(user)
    return {"ok": True}


@app.get("/api/users")
async def list_users(_: User = Depends(require_admin)):
    return [_safe_user(u) for u in sorted(_all_users(), key=lambda x: x.username.lower())]


@app.post("/api/users")
async def create_user(body: dict, _: User = Depends(require_admin)):
    name = (body.get("username") or "").strip()
    pw = body.get("password") or ""
    role = body.get("role") if body.get("role") in ("admin", "user") else "user"
    if not name:
        raise HTTPException(400, "username required")
    if len(pw) < 4:
        raise HTTPException(400, "password too short (min 4)")
    if _user_by_name(name):
        raise HTTPException(409, "username already exists")
    salt = auth.new_salt()
    u = User(username=name, role=role, salt=salt, password_hash=auth.hash_password(pw, salt))
    _save_user(u)
    _user_root(u.id)            # create their empty private library
    return _safe_user(u)


@app.delete("/api/users/{uid}")
async def delete_user(uid: str, admin: User = Depends(require_admin)):
    u = _user_by_id(uid)
    if not u:
        raise HTTPException(404, "user not found")
    if u.id == admin.id:
        raise HTTPException(400, "cannot delete yourself")
    if u.role == "admin" and sum(1 for x in _all_users() if x.role == "admin") <= 1:
        raise HTTPException(400, "cannot delete the last admin")
    (USERS / f"{uid}.json").unlink(missing_ok=True)
    shutil.rmtree(UROOT / uid, ignore_errors=True)   # remove their private library
    return {"ok": True}


@app.post("/api/users/{uid}/password")
async def reset_password(uid: str, body: dict, _: User = Depends(require_admin)):
    u = _user_by_id(uid)
    if not u:
        raise HTTPException(404, "user not found")
    pw = body.get("password") or ""
    if len(pw) < 4:
        raise HTTPException(400, "password too short (min 4)")
    u.salt = auth.new_salt()
    u.password_hash = auth.hash_password(pw, u.salt)
    _save_user(u)
    return {"ok": True}


COPY_KINDS = ("character", "persona", "preset")


@app.post("/api/users/{uid}/copy")
async def copy_to_user(uid: str, body: dict, admin: User = Depends(require_admin)):
    """Copy characters, personas and presets from the admin's library into another user's.

    ``body.items`` is ``[{"kind": "character"|"persona"|"preset", "id": …}]``. Each copy is
    a clone under a new id, so it carries everything stored on the record: portrait,
    sprites, voice and speed, wake word, VRM assignment and its still. VRM files are
    shared by every user in ``data/vrm/``, so the model needs no copying.

    A preset's character/persona pairing points at ids in the admin's library. When the
    paired item is copied in the same call, the pairing follows the new copy; otherwise
    it is cleared, since the old id means nothing in the other library.
    Copied personas are never the target's default, so their active persona stays put.
    """
    target = _user_by_id(uid)
    if not target:
        raise HTTPException(404, "user not found")
    if target.id == admin.id:
        raise HTTPException(400, "that is your own library — use Duplicate instead")
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "nothing selected")
    src, dst = _ctx_for(admin), _ctx_for(target)
    folders = {"character": (src.cards, dst.cards, CharacterCard),
               "persona": (src.personas, dst.personas, Persona),
               "preset": (src.presets, dst.presets, RelationshipPreset)}

    # Load everything first so a bad id fails the call before anything is written.
    loaded = []
    for it in items:
        kind, id_ = (it or {}).get("kind"), (it or {}).get("id") or ""
        if kind not in COPY_KINDS:
            raise HTTPException(400, f"unknown kind: {kind}")
        obj = _load(folders[kind][0], id_, folders[kind][2])
        if obj is None:
            raise HTTPException(404, f"{kind} not found: {id_}")
        loaded.append((kind, obj))

    new_ids: dict[tuple[str, str], str] = {}
    copies = []
    for kind, obj in loaded:
        data = obj.model_dump(exclude={"id", "created_at", "updated_at"})
        if kind == "persona":
            data["is_default"] = False
        copy = folders[kind][2](**data)
        new_ids[(kind, obj.id)] = copy.id
        copies.append((kind, copy))

    out = []
    for kind, copy in copies:
        note = ""
        if kind == "preset":
            for field, k in (("character_id", "character"), ("persona_id", "persona")):
                old = getattr(copy, field)
                if old:
                    setattr(copy, field, new_ids.get((k, old), ""))
                    if not getattr(copy, field):
                        note = "pairing cleared (paired item not copied)"
        if kind == "character" and copy.vrm and not (_vrm_dir() / Path(copy.vrm).name).is_file():
            note = f"VRM model {copy.vrm} is missing"
        _save(copy, folders[kind][1])
        out.append({"kind": kind, "id": copy.id, "name": copy.name, "note": note})
    return {"ok": True, "user": target.username, "copied": out}


# --------------------------------------------------------------------------
# Status / voice (auth required)
# --------------------------------------------------------------------------
@app.get("/api/status")
async def status(_: User = Depends(current_user)):
    return await kobold.check_status()


@app.post("/api/tts")
async def tts_synth(body: dict, _: User = Depends(current_user)):
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    try:
        wav = await asyncio.to_thread(tts.synth_wav, text,
                                      body.get("voice", "af_sarah"), float(body.get("speed", 1.0)))
    except Exception as e:
        raise HTTPException(500, f"tts failed: {e}")
    return Response(content=wav, media_type="audio/wav")


@app.post("/api/tts/stream")
async def tts_stream(body: dict, _: User = Depends(current_user)):
    """One sentence in an Orpheus voice, streamed as it is generated: raw PCM s16le,
    24 kHz, mono (X-Sample-Rate). voice.js uses this for orpheus: voices, so playback
    starts ~0.3 s in instead of after the whole sentence has been generated."""
    from fastapi.responses import StreamingResponse
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    return StreamingResponse(tts.stream_pcm(text, str(body.get("voice") or "orpheus:tara")),
                             media_type="application/octet-stream",
                             headers={"X-Sample-Rate": "24000", "Cache-Control": "no-store"})


@app.post("/api/tts/engine")
async def tts_engine(body: dict, _: User = Depends(current_user)):
    """Orpheus is loaded only while a voice that uses it is on: {on: true} loads it and
    answers once it is ready or has failed (3–15 s; voice.js does not wait on it),
    {on: false} unloads it and frees ≈3 GB of VRAM.
    Returns {state: ready|released|unavailable}."""
    from services import orpheus
    try:
        if body.get("on"):
            return {"state": await asyncio.to_thread(orpheus.warm, True)}
        await asyncio.to_thread(orpheus.release)
        return {"state": "released"}
    except orpheus.OrpheusUnavailable as e:
        return {"state": "unavailable", "detail": str(e)}


@app.get("/api/tts/voices")
async def tts_voices(_: User = Depends(current_user)):
    return {"voices": await asyncio.to_thread(tts.voices)}


@app.post("/api/stt")
async def stt_transcribe(file: UploadFile = File(...), _: User = Depends(current_user),
                         strict: bool = False):
    audio = await file.read()
    if not audio:
        raise HTTPException(400, "empty audio")
    try:
        text = await asyncio.to_thread(stt.transcribe, audio, None, strict)
    except Exception as e:
        raise HTTPException(500, f"stt failed: {e}")
    return {"text": text}


@app.get("/api/settings")
async def get_settings(cur: Ctx = Depends(get_ctx)):
    return _load_settings(cur).model_dump()


@app.put("/api/settings")
async def put_settings(body: dict, cur: Ctx = Depends(get_ctx)):
    data = _load_settings(cur).model_dump()
    data.update({k: v for k, v in body.items() if k in data})   # ignore unknown keys
    s = AppSettings(**data)
    _save_settings(cur, s)
    return s.model_dump()


# --------------------------------------------------------------------------
# Character cards
# --------------------------------------------------------------------------
@app.get("/api/cards")
async def list_cards(cur: Ctx = Depends(get_ctx)):
    # Trim the heavy expression sprite blobs from the list; expose a flag instead.
    out = []
    for c in _list(cur.cards, CharacterCard):
        d = c.model_dump(exclude={"expressions", "vrm_still"})
        d["has_expressions"] = bool(c.expressions)
        # Labels only — the sprite blobs stay trimmed. The client needs these to know
        # which inline [expr:x] beats it actually has a sprite for.
        d["expression_labels"] = sorted(c.expressions.keys())
        # Avatar tier: "" here means the client stays on the sprite portrait.
        # An avatar needs BOTH a model and the mode set to use it, so the
        # renderer must not infer the tier from the filename alone.
        d["has_vrm"] = bool(c.vrm) and c.avatar_mode == "vrm"
        out.append(d)
    return out


@app.get("/api/cards/{card_id}")
async def get_card(card_id: str, cur: Ctx = Depends(get_ctx)):
    c = _load(cur.cards, card_id, CharacterCard)
    if not c:
        raise HTTPException(404, "card not found")
    return c.model_dump()


@app.post("/api/cards/import")
async def import_card(file: UploadFile = File(...), cur: Ctx = Depends(get_ctx)):
    raw = await file.read()
    try:
        card = card_io.import_card_bytes(raw, file.filename or "")
    except Exception as e:
        raise HTTPException(400, f"could not parse card: {e}")
    _save(card, cur.cards)
    return card.model_dump()


@app.post("/api/cards")
async def create_card(card: CharacterCard, cur: Ctx = Depends(get_ctx)):
    _save(card, cur.cards)
    return card.model_dump()


@app.put("/api/cards/{card_id}")
async def update_card(card_id: str, card: CharacterCard, cur: Ctx = Depends(get_ctx)):
    card.id = card_id
    _save(card, cur.cards)
    return card.model_dump()


@app.delete("/api/cards/{card_id}")
async def delete_card(card_id: str, cur: Ctx = Depends(get_ctx)):
    p = cur.cards / f"{card_id}.json"
    if p.exists():
        p.unlink()
    return {"ok": True}


@app.get("/api/cards/{card_id}/export")
async def export_card(card_id: str, version: int = 2, cur: Ctx = Depends(get_ctx)):
    c = _load(cur.cards, card_id, CharacterCard)
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
async def comfyui_status(_: User = Depends(current_user)):
    return await comfy.check_status()


@app.get("/api/comfyui/checkpoints")
async def comfyui_checkpoints(_: User = Depends(current_user)):
    try:
        return {"checkpoints": await comfy.list_checkpoints()}
    except Exception as e:
        raise HTTPException(502, f"ComfyUI unreachable: {e}")


@app.get("/api/expressions/emotions")
async def expression_emotions(_: User = Depends(current_user)):
    return {"core": expressions.CORE_EMOTIONS, "full": expressions.FULL_EMOTIONS}


@app.post("/api/cards/{card_id}/expressions")
async def generate_expressions(card_id: str, body: dict, cur: Ctx = Depends(get_ctx)):
    """Generate an expression sprite per emotion via ComfyUI and store them on the card."""
    card = _load(cur.cards, card_id, CharacterCard)
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
    card.expr_checkpoint = checkpoint
    card.expr_kontext = bool(body.get("kontext", card.expr_kontext))
    use_kontext = card.expr_kontext and await comfy.has_kontext()

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
            use_kontext=use_kontext,
        )
    except Exception as e:
        raise HTTPException(502, f"generation failed: {e}")

    # Bulk generate REPLACES the whole set (clears old sprites first) so switching
    # between Core/Full starts fresh — no stale sprites left over. Single-sprite
    # regen (below) still merges.
    #
    # Re-read the card before saving: generation takes minutes, and saving the copy
    # loaded at the start would silently undo any edit made meanwhile (in either
    # page). Only the sprite fields this request owns are written onto the fresh copy.
    fresh = _load(cur.cards, card_id, CharacterCard)
    if not fresh:
        raise HTTPException(404, "card was deleted during generation")
    fresh.expressions = sprites
    fresh.appearance = card.appearance
    fresh.expr_from_upload = card.expr_from_upload
    fresh.expr_denoise = card.expr_denoise
    fresh.expr_checkpoint = card.expr_checkpoint
    fresh.expr_kontext = card.expr_kontext
    _save(fresh, cur.cards)
    return {"generated": list(sprites.keys()), "total": len(fresh.expressions)}


@app.get("/api/cards/{card_id}/expression/{emotion}")
async def get_expression(card_id: str, emotion: str, cur: Ctx = Depends(get_ctx)):
    """Return one expression sprite as a PNG (falls back to neutral, then card art)."""
    card = _load(cur.cards, card_id, CharacterCard)
    if not card:
        raise HTTPException(404, "card not found")
    b64 = card.expressions.get(emotion) or card.expressions.get("neutral") or card.image
    if not b64:
        raise HTTPException(404, "no sprite")
    import base64
    return Response(content=base64.b64decode(b64), media_type="image/png")


def _vrm_dir() -> Path:
    d = DATA / "vrm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _vrm_path(name: str) -> Path:
    """Resolve a model name to a path inside data/vrm, or 404.

    Basename only, then resolved and prefix-checked: a name is user input and
    must not be able to address anything outside the models directory.
    """
    base = _vrm_dir().resolve()
    path = (base / Path(name).name).resolve()
    if not str(path).startswith(str(base)) or not path.is_file():
        raise HTTPException(404, "model not found")
    return path


@app.get("/api/vrm")
async def list_vrm(_: User = Depends(current_user)):
    """Avatar models available to assign to a card, each with its audit."""
    out = []
    for f in sorted(_vrm_dir().glob("*.vrm")):
        try:
            out.append(vrm.audit_file(f))
        except Exception as e:                      # noqa: BLE001
            # A corrupt file in the folder must not break the whole listing.
            out.append({"name": f.name, "size": f.stat().st_size,
                        "error": str(e), "usable": False})
    return out


@app.post("/api/vrm")
async def upload_vrm(file: UploadFile = File(...), _: User = Depends(current_user)):
    """Import a .vrm. Audited before it is written, so a file that cannot work
    as an avatar is rejected at the door rather than failing later in the
    renderer, where the only symptom is a model that never appears."""
    data = await file.read()
    if len(data) > vrm.MAX_BYTES:
        raise HTTPException(413, f"model exceeds {vrm.MAX_BYTES // 1048576} MB")
    try:
        report = vrm.audit(data)
    except vrm.NotAVrm as e:
        raise HTTPException(400, str(e)) from e
    if not report["usable"]:
        raise HTTPException(400, "unusable as an avatar: " + "; ".join(report["problems"]))

    name = Path(file.filename or "model.vrm").name
    if not name.lower().endswith(".vrm"):
        name += ".vrm"
    dest = _vrm_dir() / name
    stem, i = dest.stem, 1
    while dest.exists():                            # never silently replace a model
        dest = _vrm_dir() / f"{stem}-{i}.vrm"
        i += 1
    dest.write_bytes(data)
    return {"name": dest.name, **report}


@app.delete("/api/vrm/{name}")
async def delete_vrm(name: str, cur: Ctx = Depends(get_ctx)):
    """Remove a model. Refused while a card still points at it."""
    path = _vrm_path(name)
    # Models are shared by every user, so check every library, not just this one:
    # a card copied to someone else would otherwise lose its avatar.
    users = []
    for root in sorted(UROOT.glob("*/characters")):
        for c in _list(root, CharacterCard):
            if c.vrm == path.name:
                users.append(c.name if root == cur.cards else f"{c.name} (another user)")
    if users:
        raise HTTPException(409, "in use by: " + ", ".join(users))
    path.unlink()
    return {"deleted": path.name}


@app.get("/api/vrm/{name}")
async def get_vrm(name: str, _: User = Depends(current_user)):
    """Serve one model by name. Used by the card editor's picker and the harness,
    so a model can be previewed before it is assigned to a card."""
    return FileResponse(_vrm_path(name), media_type="model/gltf-binary",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/cards/{card_id}/avatar.vrm")
async def get_avatar(card_id: str, cur: Ctx = Depends(get_ctx)):
    """Return this card's VRM model, if it is on the avatar tier.

    Models live in data/vrm/ rather than inside the card: a .vrm is tens of
    megabytes, and the card JSON is read and rewritten on every turn.
    """
    card = _load(cur.cards, card_id, CharacterCard)
    if not card:
        raise HTTPException(404, "card not found")
    if not card.vrm:
        raise HTTPException(404, "card is on the sprite tier")
    # Filename only — never let a card walk out of the models dir.
    path = (DATA / "vrm" / Path(card.vrm).name).resolve()
    if not str(path).startswith(str((DATA / "vrm").resolve())) or not path.is_file():
        raise HTTPException(404, "model file missing")
    # Same URL, different bytes whenever the card is pointed at another model,
    # so it must not be cached by the view that loads it.
    return FileResponse(path, media_type="model/gltf-binary",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/cards/{card_id}/expressions")
async def list_expressions(card_id: str, cur: Ctx = Depends(get_ctx)):
    """Which emotions this card currently has sprites for (names only, no blobs)."""
    card = _load(cur.cards, card_id, CharacterCard)
    if not card:
        raise HTTPException(404, "card not found")
    return {"emotions": sorted(card.expressions.keys())}


@app.post("/api/cards/{card_id}/expression/{emotion}")
async def regenerate_expression(card_id: str, emotion: str, body: dict, cur: Ctx = Depends(get_ctx)):
    """(Re)generate a single expression sprite."""
    card = _load(cur.cards, card_id, CharacterCard)
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
            use_kontext=card.expr_kontext and await comfy.has_kontext(),
        )
    except Exception as e:
        raise HTTPException(502, f"generation failed: {e}")
    # Merge onto a fresh read, for the same reason as the batch above.
    fresh = _load(cur.cards, card_id, CharacterCard)
    if not fresh:
        raise HTTPException(404, "card was deleted during generation")
    fresh.expressions.update(sprites)
    if "appearance" in body:
        fresh.appearance = card.appearance
    _save(fresh, cur.cards)
    return {"ok": True, "emotion": emotion}


@app.delete("/api/cards/{card_id}/expression/{emotion}")
async def delete_expression(card_id: str, emotion: str, cur: Ctx = Depends(get_ctx)):
    card = _load(cur.cards, card_id, CharacterCard)
    if not card:
        raise HTTPException(404, "card not found")
    card.expressions.pop(emotion, None)
    _save(card, cur.cards)
    return {"ok": True, "total": len(card.expressions)}


# --------------------------------------------------------------------------
# Personas
# --------------------------------------------------------------------------
@app.get("/api/personas")
async def list_personas(cur: Ctx = Depends(get_ctx)):
    return [p.model_dump() for p in _list(cur.personas, Persona)]


@app.post("/api/personas")
async def create_persona(persona: Persona, cur: Ctx = Depends(get_ctx)):
    _save(persona, cur.personas)
    if persona.is_default:
        _clear_other_default_personas(cur, persona.id)
    return persona.model_dump()


@app.put("/api/personas/{persona_id}")
async def update_persona(persona_id: str, persona: Persona, cur: Ctx = Depends(get_ctx)):
    persona.id = persona_id
    _save(persona, cur.personas)
    if persona.is_default:
        _clear_other_default_personas(cur, persona.id)
    return persona.model_dump()


@app.delete("/api/personas/{persona_id}")
async def delete_persona(persona_id: str, cur: Ctx = Depends(get_ctx)):
    p = cur.personas / f"{persona_id}.json"
    if p.exists():
        p.unlink()
    return {"ok": True}


# --------------------------------------------------------------------------
# Relationship
# --------------------------------------------------------------------------
@app.get("/api/relationship")
async def get_relationship(character_id: str, persona_id: str, cur: Ctx = Depends(get_ctx)):
    rel = _load_relationship(cur, character_id, persona_id)
    card = _load(cur.cards, character_id, CharacterCard)
    s = relationship.summary(rel)
    s["power_dynamic"] = rel.power_dynamic if rel.power_dynamic is not None else (card.power_dynamic if card else 0.0)
    s["social_acceptance"] = rel.social_acceptance if rel.social_acceptance is not None else (card.social_acceptance if card else 50.0)
    s["context_overridden"] = rel.power_dynamic is not None or rel.social_acceptance is not None
    return s


@app.put("/api/relationship")
async def set_relationship(body: dict, cur: Ctx = Depends(get_ctx)):
    """Per-relationship override of context (power_dynamic / social_acceptance). Null clears -> card default."""
    cid, pid = body.get("character_id"), body.get("persona_id")
    if not cid or not pid:
        raise HTTPException(400, "character_id and persona_id required")
    rel = _load_relationship(cur, cid, pid)
    if "power_dynamic" in body:
        rel.power_dynamic = body["power_dynamic"]
    if "social_acceptance" in body:
        rel.social_acceptance = body["social_acceptance"]
    _save_relationship(cur, rel)
    return relationship.summary(rel)


# --------------------------------------------------------------------------
# Relationship presets (reusable scenario seeds)
# --------------------------------------------------------------------------
@app.get("/api/presets")
async def list_presets(cur: Ctx = Depends(get_ctx)):
    return [p.model_dump() for p in _list(cur.presets, RelationshipPreset)]


@app.post("/api/presets")
async def create_preset(preset: RelationshipPreset, cur: Ctx = Depends(get_ctx)):
    _save(preset, cur.presets)
    return preset.model_dump()


@app.get("/api/presets/{preset_id}")
async def get_preset(preset_id: str, cur: Ctx = Depends(get_ctx)):
    p = _load(cur.presets, preset_id, RelationshipPreset)
    if not p:
        raise HTTPException(404, "preset not found")
    return p.model_dump()


@app.put("/api/presets/{preset_id}")
async def update_preset(preset_id: str, preset: RelationshipPreset, cur: Ctx = Depends(get_ctx)):
    preset.id = preset_id
    _save(preset, cur.presets)
    return preset.model_dump()


@app.delete("/api/presets/{preset_id}")
async def delete_preset(preset_id: str, cur: Ctx = Depends(get_ctx)):
    p = cur.presets / f"{preset_id}.json"
    if p.exists():
        p.unlink()
    return {"ok": True}


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
@app.get("/api/sessions")
async def list_sessions(cur: Ctx = Depends(get_ctx)):
    return [
        {"id": s.id, "title": s.title, "character_id": s.character_id,
         "updated_at": s.updated_at, "messages": len(s.messages)}
        for s in _list(cur.sessions, ChatSession)
    ]


@app.post("/api/sessions")
async def create_session(body: dict, cur: Ctx = Depends(get_ctx)):
    card = _load(cur.cards, body.get("character_id", ""), CharacterCard)
    if not card:
        raise HTTPException(400, "character_id required and must exist")
    persona = _load(cur.personas, body.get("persona_id", ""), Persona) or _default_persona(cur)

    session = ChatSession(
        title=card.name, character_id=card.id, persona_id=persona.id,
    )

    # Scenario preset: seed a session-scoped relationship + optional opening note.
    scenario_note = ""
    preset_id = body.get("preset_id")
    if preset_id and body.get("mode") == "scenario":
        preset = _load(cur.presets, preset_id, RelationshipPreset)
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
    _save(session, cur.sessions)
    return session.model_dump()


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str, cur: Ctx = Depends(get_ctx)):
    s = _load(cur.sessions, session_id, ChatSession)
    if not s:
        raise HTTPException(404, "session not found")
    d = s.model_dump()
    if s.relationship is not None:                 # scenario session -> its own summary
        d["relationship_summary"] = relationship.summary(s.relationship)
    return d


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, cur: Ctx = Depends(get_ctx)):
    s = _load(cur.sessions, session_id, ChatSession)
    if s:
        _forget_swept_session(cur, s)
    p = cur.sessions / f"{session_id}.json"
    if p.exists():
        p.unlink()
    return {"ok": True}


@app.put("/api/sessions/{session_id}/swipe")
async def set_swipe(session_id: str, body: dict, cur: Ctx = Depends(get_ctx)):
    """Select which stored alternate (swipe) of the last char message is active."""
    s = _load(cur.sessions, session_id, ChatSession)
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
                    card = _load(cur.cards, s.character_id, CharacterCard)
                    if card and card.expressions:
                        try:
                            m.swipe_expressions[idx] = await expressions.classify(
                                kobold, m.content, labels=list(card.expressions))
                        except Exception:
                            m.swipe_expressions[idx] = "neutral"
                expr = m.swipe_expressions[idx] or m.expression
                m.expression = expr
                _save(s, cur.sessions)
            break
    return {"ok": True, "expression": expr}


@app.get("/api/sessions/{session_id}/memory")
async def get_memory(session_id: str, cur: Ctx = Depends(get_ctx)):
    s = _load(cur.sessions, session_id, ChatSession)
    if not s:
        raise HTTPException(404, "session not found")
    return s.memory.model_dump()


@app.put("/api/sessions/{session_id}/memory")
async def set_memory(session_id: str, body: dict, cur: Ctx = Depends(get_ctx)):
    """Edit the session's pinned facts and/or rolling summary (user-curated)."""
    s = _load(cur.sessions, session_id, ChatSession)
    if not s:
        raise HTTPException(404, "session not found")
    if "pinned" in body:
        raw = body.get("pinned") or []
        if not isinstance(raw, list):
            raise HTTPException(400, "pinned must be a list of strings")
        s.memory.pinned = [str(x).strip() for x in raw if str(x).strip()]
    if "summary" in body:
        s.memory.summary = str(body.get("summary") or "").strip()
    _save(s, cur.sessions)
    return s.memory.model_dump()


@app.get("/api/memories")
async def get_character_memory(character_id: str, persona_id: str, cur: Ctx = Depends(get_ctx)):
    """Cross-session memory for one (character, persona) pairing. Same query-param
    shape as GET /api/relationship, which is scoped identically."""
    return _load_character_memory(cur, character_id, persona_id).model_dump()


@app.put("/api/memories")
async def put_character_memory(body: dict, cur: Ctx = Depends(get_ctx)):
    """Edit remembered facts and/or the cross-session history (user-curated).

    Facts round-trip by id so an existing one keeps its `origin` and `created_at`
    — otherwise every save through the modal would silently relabel auto-written
    facts as user-written and destroy the audit trail the origin tag exists for.
    """
    cid = str(body.get("character_id") or "")
    pid = str(body.get("persona_id") or "")
    if not cid or not pid:
        raise HTTPException(400, "character_id and persona_id are required")

    mem = _load_character_memory(cur, cid, pid)
    if "facts" in body:
        raw = body.get("facts") or []
        if not isinstance(raw, list):
            raise HTTPException(400, "facts must be a list")
        known = {f.id: f for f in mem.facts}
        facts: list[MemoryFact] = []
        for item in raw:
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict):
                continue
            # Clamped like an extracted fact: unbounded text here would eat the
            # whole injection budget and push every other fact out of the block.
            text = str(item.get("text") or "").strip()[:memory.LONGTERM_FACT_MAX_CHARS]
            if not text:
                continue
            prev = known.get(str(item.get("id") or ""))
            if prev:
                prev.text = text
                facts.append(prev)
            else:
                facts.append(MemoryFact(text=text, origin="user"))
        # Newest-wins if the user blows past the cap — the store must stay bounded.
        mem.facts = facts[-memory.LONGTERM_MAX_FACTS:]
    if "summary" in body:
        mem.summary = str(body.get("summary") or "").strip()
    mem.updated_at = datetime.now().isoformat()
    _save_character_memory(cur, mem)
    # Re-index facts wholesale rather than diffing: they are editable and
    # deletable, so replace-by-kind is both simpler and correct.
    await asyncio.to_thread(
        memory.index_add, _memory_index_path(cur, cid, pid),
        [{"text": f.text, "kind": "fact"} for f in mem.facts], "fact")
    return mem.model_dump()


@app.get("/api/assistant-memory")
async def get_assistant_memory(character_id: str, persona_id: str, cur: Ctx = Depends(get_ctx)):
    """Standing instructions, facts and task notes for one assistant pairing."""
    return _load_assistant_memory(cur, character_id, persona_id).model_dump()


@app.put("/api/assistant-memory")
async def put_assistant_memory(body: dict, cur: Ctx = Depends(get_ctx)):
    """Edit them. Directives and facts round-trip BY ID so editing through the
    modal cannot relabel an auto-written entry as hand-written — the origin tag is
    the whole reason auto-extraction is allowed to write here at all."""
    cid = str(body.get("character_id") or "")
    pid = str(body.get("persona_id") or "")
    if not cid or not pid:
        raise HTTPException(400, "character_id and persona_id are required")
    mem = _load_assistant_memory(cur, cid, pid)

    for field, cap in (("directives", assistant_memory.MAX_DIRECTIVES),
                       ("facts", assistant_memory.MAX_FACTS)):
        if field not in body:
            continue
        raw = body.get(field) or []
        if not isinstance(raw, list):
            raise HTTPException(400, f"{field} must be a list")
        known = {f.id: f for f in getattr(mem, field)}
        kept: list[MemoryFact] = []
        for item in raw:
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()[:assistant_memory.FACT_MAX_CHARS]
            if not text:
                continue
            prev = known.get(str(item.get("id") or ""))
            if prev:
                prev.text = text
                kept.append(prev)
            else:
                kept.append(MemoryFact(text=text, origin="user"))
        setattr(mem, field, kept[-cap:])

    if "notes" in body:
        raw = body.get("notes") or []
        if isinstance(raw, str):
            raw = [l for l in raw.splitlines()]
        mem.notes = [str(n).strip()[:assistant_memory.NOTE_MAX_CHARS]
                     for n in raw if str(n).strip()][:assistant_memory.MAX_NOTES]
    mem.updated_at = datetime.now().isoformat()
    _save_assistant_memory(cur, mem)
    return mem.model_dump()


@app.post("/api/sessions/{session_id}/sweep")
async def sweep_session(session_id: str, cur: Ctx = Depends(get_ctx)):
    """Idle trigger: fold a finished conversation into long-term memory.

    The better of the two triggers — it captures a conversation that actually
    ended, instead of cutting at whatever turn the threshold happened to land on.
    Idempotent (see fold_into_longterm), so the frontend can fire it whenever a
    session goes quiet without tracking whether it already did.
    """
    session = _load(cur.sessions, session_id, ChatSession)
    if not session:
        raise HTTPException(404, "session not found")
    if session.relationship is not None:
        return {"ok": True, "swept": False, "reason": "scenario session"}
    card = _load(cur.cards, session.character_id, CharacterCard)
    persona = _load(cur.personas, session.persona_id, Persona) or _default_persona(cur)
    if not card:
        return {"ok": True, "swept": False, "reason": "session has no character"}

    st = _load_settings(cur)
    window = await _context_tokens(st)
    if getattr(card, "chat_mode", "roleplay") == "assistant":
        am = _load_assistant_memory(cur, session.character_id, persona.id)
        changed = await assistant_memory.fold(
            kobold, am, session, card, persona, max_tokens=st.summary_max_tokens,
            extract_items=st.auto_facts_enabled, max_new=st.auto_facts_max_new,
            context_tokens=window)
        if changed:
            _save_assistant_memory(cur, am)
            session.memory.last_summarized_index = am.swept_sessions.get(
                session.id, session.memory.last_summarized_index)
            _save(session, cur.sessions)
        return {"ok": True, "swept": changed, "pipeline": "assistant"}

    mem = _load_character_memory(cur, session.character_id, persona.id)
    changed = await memory.fold_into_longterm(
        kobold, mem, session, card, persona, max_tokens=st.summary_max_tokens,
        index_path=_memory_index_path(cur, session.character_id, persona.id),
        extract_facts=st.auto_facts_enabled, max_new_facts=st.auto_facts_max_new,
        context_tokens=window)
    if changed:
        _save_character_memory(cur, mem)
    return {"ok": True, "swept": changed}


@app.delete("/api/sessions/{session_id}/messages")
async def clear_messages(session_id: str, cur: Ctx = Depends(get_ctx)):
    s = _load(cur.sessions, session_id, ChatSession)
    if not s:
        raise HTTPException(404, "session not found")
    # Keep the opening greeting; drop the rest and reset memory.
    s.messages = s.messages[:1]
    s.memory.summary = ""
    s.memory.last_summarized_index = 0
    _save(s, cur.sessions)
    # The pairing's long-term store tracks this chat by position too; resetting
    # the session's own indices without that one leaves folding stuck.
    _forget_swept_session(cur, s)
    return {"ok": True}


# --- one message: edit, delete, truncate (the new page's ⋯ menu; PYTINCTURE_PLAN.md) ---
# Messages are addressed by id, not position, so a chat changed in another tab cannot
# make a request land on the wrong message. Two indices track this chat by POSITION —
# the rolling summary's `last_summarized_index` and the pairing store's
# `swept_sessions[id]` — so anything that removes messages must move them too, or the
# summary skips/duplicates a turn and folding into long-term memory goes wrong (the same
# failure `_forget_swept_session` documents for clearing).
#
# None of these touch mood or the relationship: those move on generation only, exactly
# as a re-roll leaves them alone. Text already folded into a summary or into long-term
# memory stays there — editing a line is not a request to rewrite what was remembered.

def _find_message(s: ChatSession, message_id: str) -> int:
    for i, m in enumerate(s.messages):
        if m.id == message_id:
            return i
    raise HTTPException(404, "message not found")


def _shift_swept(cur: Ctx, s: ChatSession, removed_from: int, count: int) -> None:
    """`count` messages starting at index `removed_from` were removed: pull the
    pairing's fold mark back so it still points at the same next-unfolded message."""
    if count <= 0 or s.relationship is not None or not s.character_id:
        return
    persona = _load(cur.personas, s.persona_id, Persona) or _default_persona(cur)
    card = _load(cur.cards, s.character_id, CharacterCard)
    assistant = bool(card) and getattr(card, "chat_mode", "roleplay") == "assistant"
    store = (_load_assistant_memory(cur, s.character_id, persona.id) if assistant
             else _load_character_memory(cur, s.character_id, persona.id))
    mark = store.swept_sessions.get(s.id)
    if mark is None or mark <= removed_from:
        return
    store.swept_sessions[s.id] = max(removed_from, mark - count)
    store.updated_at = datetime.now().isoformat()
    (_save_assistant_memory if assistant else _save_character_memory)(cur, store)


@app.put("/api/sessions/{session_id}/messages/{message_id}")
async def edit_message(session_id: str, message_id: str, body: dict, cur: Ctx = Depends(get_ctx)):
    """Replace one message's text. On a character reply it edits the swipe being shown."""
    s = _load(cur.sessions, session_id, ChatSession)
    if not s:
        raise HTTPException(404, "session not found")
    text = str(body.get("content") or "").strip()
    if not text:
        raise HTTPException(400, "a message cannot be empty — delete it instead")
    m = s.messages[_find_message(s, message_id)]
    m.content = text
    if m.swipes and 0 <= m.swipe_index < len(m.swipes):
        m.swipes[m.swipe_index] = text
    s.updated_at = datetime.now().isoformat()
    _save(s, cur.sessions)
    return {"ok": True, "message": m.model_dump()}


@app.delete("/api/sessions/{session_id}/messages/{message_id}")
async def delete_message(session_id: str, message_id: str, cur: Ctx = Depends(get_ctx)):
    """Remove one message. The opening greeting is kept, as clearing a chat keeps it."""
    s = _load(cur.sessions, session_id, ChatSession)
    if not s:
        raise HTTPException(404, "session not found")
    i = _find_message(s, message_id)
    if i == 0:
        raise HTTPException(400, "the opening greeting stays — clear the chat to start over")
    del s.messages[i]
    if i < s.memory.last_summarized_index:
        s.memory.last_summarized_index -= 1
    s.updated_at = datetime.now().isoformat()
    _save(s, cur.sessions)
    _shift_swept(cur, s, i, 1)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/messages/{message_id}/truncate")
async def truncate_after(session_id: str, message_id: str, cur: Ctx = Depends(get_ctx)):
    """Drop every message AFTER this one ("Save & resend": edit, truncate, regenerate).

    A cut below what the rolling summary already covers resets the summary, as
    clearing a chat does: the summary would otherwise describe turns that no longer
    exist, and they would be injected as "the story so far"."""
    s = _load(cur.sessions, session_id, ChatSession)
    if not s:
        raise HTTPException(404, "session not found")
    keep = _find_message(s, message_id) + 1
    removed = len(s.messages) - keep
    if removed <= 0:
        return {"ok": True, "removed": 0}
    s.messages = s.messages[:keep]
    if keep < s.memory.last_summarized_index:
        s.memory.summary = ""
        s.memory.last_summarized_index = 0
    s.updated_at = datetime.now().isoformat()
    _save(s, cur.sessions)
    _shift_swept(cur, s, keep, removed)
    return {"ok": True, "removed": removed}


# --------------------------------------------------------------------------
# Chat — SSE streaming
# --------------------------------------------------------------------------
# ---- memory maintenance, after the reply --------------------------------------------
# A turn saves and sends `done` first; the summary and long-term fold run afterwards in
# _memory_job. The user may send the next message, edit, delete or truncate while that
# job is still waiting on the model, so:
#   - jobs for one session run one at a time (_memory_locks) and each starts from a
#     fresh read — the next job re-checks the threshold against what the last one did;
#   - a job writes ONLY the two fields the memory steps change (summary,
#     last_summarized_index), onto a fresh read, and only if neither moved since it
#     started and the turns it folded are unchanged — otherwise it drops its result and
#     a later turn redoes it;
#   - a turn's own save (_save_turn) adopts a newer summary instead of writing back the
#     one it loaded before generating.
_memory_locks: dict[str, asyncio.Lock] = {}
_memory_tasks: set[asyncio.Task] = set()      # strong refs: the loop keeps only weak ones


def _save_turn(cur: Ctx, session: ChatSession) -> None:
    """Save a finished turn without rolling back a summary a memory job wrote meanwhile."""
    fresh = _load(cur.sessions, session.id, ChatSession)
    if fresh and fresh.memory.last_summarized_index > session.memory.last_summarized_index:
        session.memory = fresh.memory
    _save(session, cur.sessions)


def _spawn_memory_job(cur: Ctx, session_id: str) -> asyncio.Task:
    task = asyncio.create_task(_memory_job(cur, session_id))
    _memory_tasks.add(task)
    task.add_done_callback(_memory_tasks.discard)
    return task


async def _memory_job(cur: Ctx, session_id: str) -> None:
    lock = _memory_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        try:
            await _maintain_memory(cur, session_id)
        except Exception as e:  # noqa: BLE001 — best-effort, as it always was
            print(f"memory maintenance failed for session {session_id}: {e!r}", flush=True)


def _memory_basis(session: ChatSession) -> tuple:
    return (session.memory.summary, session.memory.last_summarized_index,
            tuple((m.id, m.content) for m in session.messages))


def _apply_memory(cur: Ctx, worked: ChatSession, basis: tuple) -> bool:
    """Write the job's summary fields onto a fresh read, if it is still valid there."""
    summary0, index0, msgs0 = basis
    summary1, index1 = worked.memory.summary, worked.memory.last_summarized_index
    if (summary1, index1) == (summary0, index0):
        return False
    fresh = _load(cur.sessions, worked.id, ChatSession)
    if not fresh:
        return False
    if (fresh.memory.summary, fresh.memory.last_summarized_index) != (summary0, index0):
        return False              # a sweep, truncate or clear moved it: theirs wins
    if tuple((m.id, m.content) for m in fresh.messages)[:index1] != msgs0[:index1]:
        return False              # a turn it folded was edited or deleted meanwhile
    fresh.memory.summary, fresh.memory.last_summarized_index = summary1, index1
    _save(fresh, cur.sessions)
    return True


async def _maintain_memory(cur: Ctx, session_id: str) -> None:
    """The memory maintenance a turn used to run before `done` (thresholds user-tunable)."""
    session = _load(cur.sessions, session_id, ChatSession)
    if not session:
        return
    card = _load(cur.cards, session.character_id, CharacterCard)
    if not card:
        return
    persona = _load(cur.personas, session.persona_id, Persona) or _default_persona(cur)
    assistant = getattr(card, "chat_mode", "roleplay") == "assistant"
    scenario = session.relationship is not None
    basis = _memory_basis(session)
    st = _load_settings(cur)
    window = await _context_tokens(st)
    if assistant:
        # The window still has to advance or the prompt grows without bound, but
        # nothing narrative comes out of it: the fold writes directives, facts and task
        # notes, and leaves session.memory.summary empty — clearing a roleplay-era one
        # if the card was switched, since nothing rewrites that field for an assistant
        # and a stale narrative would sit in every prompt forever.
        keep = min(st.keep_recent, st.summarize_after - 1)
        fold_end = len(session.messages) - keep
        if (memory.needs_summary(session, st.summarize_after)
                and fold_end > session.memory.last_summarized_index and not scenario):
            am = _load_assistant_memory(cur, session.character_id, persona.id)
            if await assistant_memory.fold(
                    kobold, am, session, card, persona, upto=fold_end,
                    max_tokens=st.summary_max_tokens,
                    extract_items=st.auto_facts_enabled,
                    max_new=st.auto_facts_max_new, context_tokens=window):
                _save_assistant_memory(cur, am)
                session.memory.last_summarized_index = fold_end
    else:
        await memory.summarize(kobold, session, card, persona,
                               summarize_after=st.summarize_after,
                               keep_recent=st.keep_recent,
                               max_tokens=st.summary_max_tokens,
                               context_tokens=window)
        # Threshold trigger: fold forward exactly what summarize() just folded out of
        # the verbatim window, so nothing leaves the live prompt without being recorded
        # durably first. Loaded here, right before the fold — a concurrent sweep may
        # have advanced it, and writing back a stale copy would lose that.
        if not scenario:
            cm = _load_character_memory(cur, session.character_id, persona.id)
            if await memory.fold_into_longterm(
                    kobold, cm, session, card, persona,
                    upto=session.memory.last_summarized_index,
                    max_tokens=st.summary_max_tokens,
                    index_path=_memory_index_path(cur, session.character_id, persona.id),
                    extract_facts=st.auto_facts_enabled, max_new_facts=st.auto_facts_max_new,
                    context_tokens=window):
                _save_character_memory(cur, cm)
    _apply_memory(cur, session, basis)


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# Seconds of silence before the chat stream sends a keep-alive. Must stay well under
# pytincture's BFF_STREAM_IDLE_TIMEOUT_SECONDS (30 s default), which closes a quiet stream.
STREAM_KEEPALIVE_S = 10.0


@app.post("/api/chat")
async def chat(req: ChatRequest, cur: Ctx = Depends(get_ctx)):
    session = _load(cur.sessions, req.session_id, ChatSession)
    if not session:
        raise HTTPException(404, "session not found")
    card = _load(cur.cards, session.character_id, CharacterCard)
    persona = _load(cur.personas, session.persona_id, Persona) or _default_persona(cur)
    if not card:
        raise HTTPException(400, "session has no character")
    if req.continue_turn and card.chat_mode == "assistant":
        raise HTTPException(400, "Continue is for roleplay chats")

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
        if not req.continue_turn:          # Continue: she goes on without a new user message
            session.messages.append(ChatMessage(role="user", content=req.input_text))

    scenario = session.relationship is not None
    rel = session.relationship if scenario else _load_relationship(cur, session.character_id, persona.id)
    power = rel.power_dynamic if rel.power_dynamic is not None else card.power_dynamic
    social = rel.social_acceptance if rel.social_acceptance is not None else card.social_acceptance
    # Assistant-mode characters get NO relationship framing. The descriptor and the
    # boundary note are scene-and-feelings text — affection, attraction, emotional
    # openness, physical consent — and injecting them beneath a system prompt that
    # says "there is no physical setting or scene" is what had assistants replying
    # with narrated thoughts, gestures and self-description. The boundary note was
    # the worst of it: it lands LAST, the slot small models weight most heavily, and
    # its own wording says "move the scene forward". Gated exactly the way the
    # date/time line is gated the other way in build_messages().
    assistant = getattr(card, "chat_mode", "roleplay") == "assistant"
    # The descriptor and boundary note are written ABOUT the character, so they need
    # the card's pronouns; blank keeps the she/her these strings used to hardcode.
    pronouns = relationship.Pronouns.parse(getattr(card, "pronouns", ""))
    rel_note = "" if assistant else relationship.descriptor(
        rel, card.name, persona.name, power, social, pronouns=pronouns)
    if not req.regenerate and session.pending_milestone:
        # Consumed either way. An assistant has no milestone to announce, and leaving
        # it pending would let it pile up and then fire at whatever moment the card
        # was next switched to roleplay.
        if not assistant:
            rel_note += " MILESTONE MOMENT — " + session.pending_milestone
        session.pending_milestone = ""
    # Cross-session memory is skipped for scenario sessions, exactly like `rel` above:
    # a scenario is a deliberate alternate premise seeded from a preset, so the main
    # timeline's remembered history would contradict the state it was just given.
    # Two pipelines, chosen once. An assistant never touches services/memory.py:
    # no narrative summary, no companion facts, no semantic recall over roleplay
    # turns — see services/assistant_memory.py for why that is a module and not a
    # branch.
    char_mem = None if (scenario or assistant) else _load_character_memory(
        cur, session.character_id, persona.id)
    asst_mem = _load_assistant_memory(cur, session.character_id, persona.id) \
        if (assistant and not scenario) else None
    settings = _load_settings(cur)

    # Semantic recall (Phase 4): the long-term summary is lossy by design, so pull
    # back the specifics that relate to what was just said. Retrieval SHARES
    # longterm_block's character budget rather than adding to it — turning recall
    # on must not silently double the memory footprint of every prompt.
    # Searched BEFORE the long-term block is rendered so that block can make room:
    # it fills the whole budget once facts accumulate, which used to starve recall
    # to nothing exactly when the pairing had enough history to need it.
    # Offloaded to a thread: onnxruntime is sync and would block the event loop,
    # stalling every other session's stream for the duration.
    hits = []
    if char_mem is not None and settings.recall_enabled:
        last_user = next((m.content for m in reversed(session.messages)
                          if m.role == "user" and m.content), "")
        if last_user:
            hits = await asyncio.to_thread(
                memory.index_search,
                _memory_index_path(cur, session.character_id, persona.id),
                last_user, settings.recall_top_k)

    lt_budget = memory.LONGTERM_BUDGET_CHARS - (memory.RECALL_RESERVE_CHARS if hits else 0)
    lt_note = memory.longterm_block(char_mem, card.name, persona.name, lt_budget) if char_mem else ""
    recall_note = memory.recall_block(
        hits, memory.LONGTERM_BUDGET_CHARS - len(lt_note), lt_note) if hits else ""
    if asst_mem is not None:
        lt_note = assistant_memory.block(asst_mem, card.name, persona.name)
    if hits and not recall_note and char_mem is not None:
        # Every hit was already in the long-term block — give the space back
        # rather than shrinking her memory for a block that isn't there.
        lt_note = memory.longterm_block(char_mem, card.name, persona.name)

    window = await _context_tokens(settings)
    trimmed: list = []
    messages = prompt_builder.build_messages(
        card, persona, session, relationship_note=rel_note,
        stage_rank=relationship._stage_rank(rel),
        boundary_note="" if assistant else relationship.boundary_note(
            rel, card.name, persona.name, pronouns=pronouns),
        longterm_note="\n\n".join(p for p in (lt_note, recall_note) if p),
        # Only ask for beats we can actually render a sprite for.
        expr_labels=sorted(card.expressions.keys()) if card.expressions else [],
        context_tokens=window, trimmed=trimmed)
    if trimmed and trimmed[0]:
        # Not an error: the summary absorbs these turns on its next fold. Logged
        # because a session that trims every turn wants a lower "Summarize after".
        print(f"[context] dropped {trimmed[0]} oldest turn(s) of session {session.id[:8]} "
              f"to fit {window} tokens", flush=True)
    # Qwen3 "thinking" toggle: /no_think must sit on the LAST user turn (this model
    # ignores it in the system prompt). The model then emits an empty <think></think>
    # (stripped client-side) so the whole token budget goes to the actual reply.
    # Mutates only the transient generation copy, not the stored history.
    if settings.disable_thinking:
        for m in reversed(messages):
            if m["role"] == "user":
                m["content"] += " /no_think"
                break
    params = prompt_builder.kobold_params(session)
    # Stop if the model starts writing {{user}}'s turn (prevents multi-turn / speaking for the user).
    params["stop"] = list(params.get("stop") or []) + [f"\n{persona.name}:", f"\n{persona.name} —"]

    # Inline [expr:x] beats the model emitted, in order. The last valid one wins for
    # the stored message; the classifier below is the fallback when there were none.
    tagged: list[str] = []
    known = set(expressions.CORE_EMOTIONS) | set(expressions.FULL_EMOTIONS)

    async def _turn():
        acc = ""
        try:
            async for tok in kobold.chat_generate_stream(messages, params):
                if isinstance(tok, ExprEvent):
                    label = str(tok).strip().lower()
                    if label in known:
                        tagged.append(label)
                        yield _sse({"expr": label})
                    continue          # never reaches the transcript or the TTS text
                acc += tok
                yield _sse({"token": tok})
        except Exception as e:
            yield _sse({"error": str(e)})
            return

        # A label the model emitted without its brackets never reached the tag
        # parser, so scrub it here before it is stored and re-spoken on reload.
        reply = expressions.strip_bare_labels(acc.strip(), card.expressions.keys())
        if assistant:
            # Questions are for clarification only; a courtesy question tacked onto
            # a finished answer is the assistant making conversation, which is not
            # the job. See prompt_builder for why this is code and not just a rule.
            reply = prompt_builder.strip_initiative_question(reply)
            reply = prompt_builder.strip_closing_offer(reply)
        swipes = (_pending_swipes + [reply]) if _pending_swipes else [reply]
        msg = ChatMessage(role="char", content=reply, swipes=swipes,
                          swipe_index=len(swipes) - 1)

        # Classify emotion for the expression sprite — only if this card has sprites.
        new_expr = ""
        if card.expressions:
            # Prefer what the model tagged inline — it is free and beat-accurate.
            # Only fall back to the classifier round-trip when it tagged nothing usable.
            for label in reversed(tagged):
                if label in card.expressions:
                    new_expr = label
                    break
            if not new_expr:
                try:
                    new_expr = await expressions.classify(kobold, reply, labels=list(card.expressions))
                except Exception:
                    new_expr = "neutral"
        msg.expression = new_expr or None

        # The feeling that drives mood and the relationship. With sprites it's the sprite
        # label; without, it used to be nothing at all — the classifier only ran to pick a
        # sprite, so a roleplay card without sprites never moved off Stranger however long
        # the chat (Hermione: 49 replies, every axis still 0). Read it from the full set
        # instead; the stored message keeps no expression, as there is no sprite to show.
        # Not on a re-roll: nothing below uses it there.
        feel = new_expr
        if not feel and not assistant and not req.regenerate:
            try:
                feel = await expressions.classify(kobold, reply, labels=expressions.FULL_EMOTIONS)
            except Exception:
                feel = "neutral"

        # Update the running valence/mood (inertia): neutral holds; a clear emotion
        # advances the mood and nudges valence via EMA. Foundation for a relationship score.
        if feel and feel != "neutral":
            a = 0.35
            v = expressions.valence_of(feel)
            session.mood.valence = round(max(-1.0, min(1.0, session.mood.valence * (1 - a) + v * a)), 3)
            session.mood.mood = feel

        # Advance the persistent relationship (skip on re-rolls to avoid double-counting).
        # Assistant-mode cards are skipped entirely: none of this reaches their prompt
        # any more, so advancing it only accrued affection and attraction numbers for a
        # character that has no relationship arc, and paid for a classify_activity()
        # round-trip per reply to do it. Their stored file is left as it is rather than
        # reset — inert while the card is an assistant, intact if it is ever switched back.
        milestone_toasts = []
        if not req.regenerate and not assistant:
            relationship.update(rel, feel, power, social)
            # Escalation: once physical intimacy has begun and she's positively engaged, actually
            # doing acts nudges willingness toward the next rung (social acceptance modulates the pace).
            if relationship.physical_tier(rel)[0] >= 1 and expressions.valence_of(feel) >= 0:
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
                _save_relationship(cur, rel)

        # Keep per-swipe emotions aligned with swipes (older swipes cached from before).
        exprs = list(_pending_exprs)
        while len(exprs) < len(_pending_swipes):
            exprs.append("")
        exprs.append(new_expr)
        msg.swipe_expressions = exprs

        session.messages.append(msg)

        # Saved now, and memory maintenance runs after `done` in a background job
        # (_memory_job). It is one to several model calls — the summary and the
        # long-term fold — and the reply is complete without them; holding `done` for
        # them kept her "thinking" for 30 s+ after the text was on screen.
        _save_turn(cur, session)
        _spawn_memory_job(cur, session.id)
        done_evt = {"done": True, "message_id": msg.id, "expression": msg.expression,
                    "mood": session.mood.mood, "valence": session.mood.valence,
                    # Omitted for assistants — a strip that never moves again is worse
                    # than no strip. The frontend hides it when the key is absent.
                    **({} if assistant else {"relationship": relationship.summary(rel)})}
        if milestone_toasts:
            done_evt["milestones"] = milestone_toasts
        yield _sse(done_evt)

    return StreamingResponse(_keepalive(_turn), media_type="text/event-stream")


async def _keepalive(turn):
    """``turn()`` with a keep-alive. After the last token, a chat turn still runs model calls
    (expression, relationship activity) and sends nothing until ``done``, and a long prompt
    can sit silent before the first token. In a long chat that took over 30 s, pytincture's
    BFF_STREAM_IDLE_TIMEOUT_SECONDS closed the stream, which cancelled the turn before it
    was saved — the reply had streamed in full and then vanished on reload. A ``ping``
    every few seconds of silence keeps the stream open; every client ignores it."""
    q: asyncio.Queue = asyncio.Queue()
    end = object()

    async def pump():
        try:
            async for item in turn():
                await q.put(item)
        finally:
            await q.put(end)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(q.get(), STREAM_KEEPALIVE_S)
            except asyncio.TimeoutError:
                yield _sse({"ping": True})
                continue
            if item is end:
                break
            yield item
        await task                    # surface an exception from the turn
    finally:
        # Client gone (Stop, closed tab): cancel the turn — "Stop" means it is not saved.
        if not task.done():
            task.cancel()


# --------------------------------------------------------------------------
# Narrator events and drafts of {{user}}'s reply — roleplay chats only.
# The narrator writes a neutral scene event into the chat (role "narrator"); she can
# then react to it (chat with continue_turn) or {{user}} answers. The draft writes
# {{user}}'s next message for the composer — streamed, never saved. Neither touches
# mood, the relationship or expressions.
# --------------------------------------------------------------------------
def _scene_parts(req: SceneRequest, cur: Ctx):
    session = _load(cur.sessions, req.session_id, ChatSession)
    if not session:
        raise HTTPException(404, "session not found")
    card = _load(cur.cards, session.character_id, CharacterCard)
    if not card:
        raise HTTPException(400, "session has no character")
    if card.chat_mode == "assistant":
        raise HTTPException(400, "the narrator is for roleplay chats")
    persona = _load(cur.personas, session.persona_id, Persona) or _default_persona(cur)
    return session, card, persona


def _scene_stage(cur: Ctx, session: ChatSession, persona: Persona) -> int:
    """Relationship stage for lorebook gating, chosen the way chat() chooses it."""
    rel = session.relationship if session.relationship is not None else \
        _load_relationship(cur, session.character_id, persona.id)
    return relationship._stage_rank(rel)


def _no_think(cur: Ctx, messages: list) -> None:
    """Same Qwen3 switch as chat(): /no_think on the last user turn when thinking is off."""
    if _load_settings(cur).disable_thinking:
        messages[-1]["content"] += " /no_think"


def _scene_params(session: ChatSession, max_tokens: int, stop: list[str]) -> dict:
    params = prompt_builder.kobold_params(session)
    params["max_tokens"] = min(params.get("max_tokens") or max_tokens, max_tokens)
    params["stop"] = list(params.get("stop") or []) + stop
    return params


async def _stream_text(messages: list, params: dict, acc: list):
    """Tokens as SSE; the text collects in acc[0]. Expression beats are dropped."""
    acc.append("")
    async for tok in kobold.chat_generate_stream(messages, params):
        if isinstance(tok, ExprEvent):
            continue
        acc[0] += tok
        yield _sse({"token": tok})


@app.post("/api/narrate")
async def narrate(req: SceneRequest, cur: Ctx = Depends(get_ctx)):
    session, card, persona = _scene_parts(req, cur)
    replace_id = ""
    if req.replace and session.messages and session.messages[-1].role == "narrator":
        replace_id = session.messages[-1].id
        session.messages.pop()                       # re-roll: the narration it replaces
    messages = prompt_builder.build_narrator_messages(
        card, persona, session, req.direction, _scene_stage(cur, session, persona))
    _no_think(cur, messages)
    # No blank-line stop: a side character's line usually opens its own paragraph, and
    # that stop cut 7 of 8 of them off. trim_narration() below drops a later paragraph
    # that slides into writing her or {{user}} instead, which is what the stop was for.
    params = _scene_params(session, 220, [f"\n{card.name}:", f"\n{persona.name}:"])

    async def _turn():
        acc: list = []
        try:
            async for ev in _stream_text(messages, params, acc):
                yield ev
        except Exception as e:
            yield _sse({"error": str(e)})
            return
        text = prompt_builder.trim_narration(
            prompt_builder.clean_piece(acc[0], ["Narrator", "Narration", "Game Master", "GM"]),
            card.name, persona.name)
        if not text:
            yield _sse({"error": "the narrator came up empty — try again"})
            return
        msg = ChatMessage(role="narrator", content=text)
        live = _load(cur.sessions, session.id, ChatSession)     # fresh: never write an old copy
        if live is None:
            yield _sse({"error": "that chat no longer exists"})
            return
        if replace_id and live.messages and live.messages[-1].id == replace_id:
            live.messages.pop()
        live.messages.append(msg)
        _save_turn(cur, live)
        _spawn_memory_job(cur, live.id)
        yield _sse({"done": True, "message_id": msg.id})

    return StreamingResponse(_keepalive(_turn), media_type="text/event-stream")


@app.post("/api/draft")
async def draft_reply(req: SceneRequest, cur: Ctx = Depends(get_ctx)):
    """{{user}}'s next message, written for the composer. Nothing is saved."""
    session, card, persona = _scene_parts(req, cur)
    messages = prompt_builder.build_draft_messages(
        card, persona, session, req.direction, _scene_stage(cur, session, persona))
    _no_think(cur, messages)
    # One paragraph: past a blank line the model moved on to her side of the scene
    # ("*Hermione examines the letter…*") — the speaker stops alone didn't catch that.
    params = _scene_params(session, 200, [f"\n{card.name}:", "\nNarrator:", "\n\n"])

    async def _turn():
        acc: list = []
        try:
            async for ev in _stream_text(messages, params, acc):
                yield ev
        except Exception as e:
            yield _sse({"error": str(e)})
            return
        yield _sse({"done": True, "text": prompt_builder.clean_piece(acc[0], [persona.name])})

    return StreamingResponse(_keepalive(_turn), media_type="text/event-stream")


# --------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=7861)
