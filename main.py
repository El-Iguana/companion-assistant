"""Pantheon assistant — FastAPI app.

A character-card voice assistant (split from El-Iguana/companion, the roleplay app,
on 2026-11-07). Talks to KoboldCPP (text) on the shared `ai-net` Podman network, or
wherever KOBOLD_URL points.

No database: cards, personas, and sessions are JSON files under data/. Multi-user:
each account has its own isolated library under data/u/<user_id>/… ; auth is a
signed HttpOnly cookie (see services/auth.py). Only admins manage users.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Depends
from fastapi.responses import (
    StreamingResponse, Response, FileResponse,
)

from models.schema import (
    CharacterCard, Persona, ChatSession, ChatMessage, ChatRequest,
    AppSettings, AssistantMemory, MemoryFact, User,
)
from services.kobold import KoboldClient, ExprEvent, ToolCallsEvent
from services import skills as skills_rt
import services.skills.media  # noqa: F401 — registers the DJ skill
from services import card_io, context, prompt_builder, memory, assistant_memory, expressions, tts, stt, auth, vrm

# --------------------------------------------------------------------------
BASE = Path(__file__).parent
# PANTHEON_DATA_DIR points a dev or test instance at a throwaway library. Unset in the
# container, where data/ is the mounted volume.
DATA = Path(os.environ.get("PANTHEON_DATA_DIR") or (BASE / "data"))
AUTH = DATA / "auth"
USERS = AUTH / "users"
UROOT = DATA / "u"                       # per-user library roots: data/u/<uid>/<subdir>/
SUBDIRS = ("characters", "personas", "sessions", "settings", "assistant")
# Old flat single-user dirs — migrated into the first admin on first run.
LEGACY = {name: DATA / name for name in SUBDIRS}
for d in (AUTH, USERS, UROOT):
    d.mkdir(parents=True, exist_ok=True)

KOBOLD_URL = os.environ.get("KOBOLD_URL", "http://koboldcpp:5001")
kobold = KoboldClient(KOBOLD_URL)

# The route table is no longer served (2026-10-26, pytincture switch-over): service.py
# serves the page, whose BFF classes (appcode/bff/) and plain routes (pantheon_web/) call
# these handlers directly with the signed-in user's Ctx. The decorators remain as the
# handlers' contract and for the unit tests; the classic page and its cookie login are gone.
app = FastAPI(title="Cantinero")


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
    settings: Path
    assistant: Path


def _user_root(uid: str) -> Path:
    root = UROOT / uid
    for s in SUBDIRS:
        (root / s).mkdir(parents=True, exist_ok=True)
    return root


def _ctx_for(user: User) -> Ctx:
    r = _user_root(user.id)
    return Ctx(user, r / "characters", r / "personas", r / "sessions",
               r / "settings", r / "assistant")


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


SEED = BASE / "seed"                      # example characters every new library starts with


def _seed_library(root: Path) -> int:
    """Put the example characters (seed/characters/*.json — Mira) into a NEW library.

    Called only where a library is created: the first-run ADMIN and create_user. Never
    on later loads, so a user who deletes Mira does not get her back. A card already in
    the library under the same id is left alone. Returns how many were added."""
    added = 0
    for f in sorted((SEED / "characters").glob("*.json")):
        try:
            card = CharacterCard.model_validate_json(f.read_text())
        except Exception as e:  # noqa: BLE001 — a broken seed must not block sign-up
            print(f"[seed] skipped {f.name}: {e}", flush=True)
            continue
        target = root / "characters" / f"{card.id}.json"
        if target.exists():
            continue
        card.created_at = card.updated_at = datetime.now().isoformat()
        _save(card, root / "characters")
        added += 1
    return added


def _seed_password() -> tuple[str, bool]:
    """The first-run admin password: given, or minted. Never a constant.

    A literal here would be the known default admin credential of every install of
    a shared repo — and the container publishes 7867 on all interfaces, so that is
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
    _seed_library(root)
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
# Per-user settings / memory / persona helpers (take the request Ctx)
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


def _forget_files(paths) -> None:
    for f in paths:
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass


def _forget_pairings(cur: Ctx, character_id: str = "", persona_id: str = "") -> int:
    """Delete the assistant memory kept per (character, persona) pairing for a deleted
    character or persona. Deleting a character used to leave it on disk."""
    if not (character_id or persona_id):
        return 0
    # The id becomes a glob: never let one carry a wildcard ("*" would match everyone).
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", x) for x in (character_id, persona_id) if x):
        return 0
    pattern = f"{character_id}__*" if character_id else f"*__{persona_id}.*"
    gone = [f for f in cur.assistant.glob(pattern) if f.is_file() and "__" in f.name]
    _forget_files(gone)
    return len(gone)


def _load_assistant_memory(cur: Ctx, character_id: str, persona_id: str) -> AssistantMemory:
    """Assistant memory for a pairing: data/u/<uid>/assistant/{cid}__{pid}.json."""
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


def _store_fold(cur: Ctx, before: AssistantMemory, after: AssistantMemory) -> None:
    """Save a fold without losing an edit made while it ran.

    A fold loads the pairing's memory, spends up to a minute on model calls, then
    used to save it whole, so anything edited in the Memory modal meanwhile was
    overwritten. Now only what the fold changed is applied to what is stored now:
    the entries it added or evicted (by id), the task notes only if nobody edited
    them, and its swept-session marks. `before` is a copy taken just before the fold.
    """
    fresh = _load_assistant_memory(cur, after.character_id, after.persona_id)
    if fresh.notes == before.notes:
        fresh.notes = after.notes
    for field in ("directives", "facts"):
        had = {x.id for x in getattr(before, field)}
        has = {x.id for x in getattr(after, field)}
        added = [x for x in getattr(after, field) if x.id not in had]
        setattr(fresh, field, [x for x in getattr(fresh, field) if x.id not in had - has] + added)
    for sid, idx in after.swept_sessions.items():
        fresh.swept_sessions[sid] = max(fresh.swept_sessions.get(sid, 0), idx)
    fresh.updated_at = after.updated_at
    _save_assistant_memory(cur, fresh)


def _forget_swept_session(cur: Ctx, session: ChatSession) -> None:
    """Drop `session`'s fold high-water mark from its pairing's long-term store.

    `swept_sessions[id]` is an INDEX into session.messages, so it only means
    anything while that list is intact. Clearing a chat leaves a mark pointing
    past the new end: every later fold sees `upto <= start`, silently records
    nothing, and once the chat grows back past the old length it resumes
    mid-conversation, permanently skipping everything before that point.
    Deleting a session leaves the mark behind forever instead.

    What was already folded into directives, facts and notes stays — memory is
    durable across chats by design, and clearing one chat is not a request to forget it.
    """
    if not session.character_id:
        return
    persona = _load(cur.personas, session.persona_id, Persona) or _default_persona(cur)
    am = _load_assistant_memory(cur, session.character_id, persona.id)
    if am.swept_sessions.pop(session.id, None) is not None:
        am.updated_at = datetime.now().isoformat()
        _save_assistant_memory(cur, am)


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
    for t in list(_sweep_timers.values()):          # waiting idle timers; a restart's
        t.cancel()                                   # catch-up sweeps those chats instead
    await kobold.close()


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
    _seed_library(_user_root(u.id))   # their private library, with the example assistant
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


COPY_KINDS = ("character", "persona")


@app.post("/api/users/{uid}/copy")
async def copy_to_user(uid: str, body: dict, admin: User = Depends(require_admin)):
    """Copy characters and personas from the admin's library into another user's.

    ``body.items`` is ``[{"kind": "character"|"persona", "id": …}]``. Each copy is
    a clone under a new id, so it carries everything stored on the record: portrait,
    sprites, voice and speed, wake word, VRM assignment and its still. VRM files are
    shared by every user in ``data/vrm/``, so the model needs no copying.
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
               "persona": (src.personas, dst.personas, Persona)}

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

    copies = []
    for kind, obj in loaded:
        data = obj.model_dump(exclude={"id", "created_at", "updated_at"})
        if kind == "persona":
            data["is_default"] = False
        copies.append((kind, folders[kind][2](**data)))

    out = []
    for kind, copy in copies:
        note = ""
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
    # Old cards may still carry sprite blobs from before sprites were removed: trim them.
    out = []
    for c in _list(cur.cards, CharacterCard):
        d = c.model_dump(exclude={"expressions", "vrm_still"})
        # Avatar tier: "" here means the client stays on the portrait.
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
    # Its assistant memory goes with it: nothing else can reach it.
    return {"ok": True, "forgot": _forget_pairings(cur, character_id=card_id)}


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
# 3D avatar (VRM)
# --------------------------------------------------------------------------
BASELINE_VRM = BASE / "data" / "vrm" / "baseline.vrm"   # pixiv sample, redistributable; Mira's


def _vrm_dir() -> Path:
    """data/vrm/, holding at least the bundled baseline model the seeded Mira uses.

    The image and the repo both ship it in data/vrm/, but a library elsewhere (a dev or
    test PANTHEON_DATA_DIR, a bind mount made before it existed) starts without it, and
    Mira's avatar then 404s. Copied in once; deleting it later is the user's choice."""
    d = DATA / "vrm"
    d.mkdir(parents=True, exist_ok=True)
    marker = d / ".baseline-seeded"
    if not marker.exists():
        if not (d / BASELINE_VRM.name).exists() and BASELINE_VRM.is_file():
            shutil.copyfile(BASELINE_VRM, d / BASELINE_VRM.name)
        marker.touch()
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


UPLOAD_CHUNK = 1024 * 1024


@app.post("/api/vrm")
async def upload_vrm(file: UploadFile = File(...), _: User = Depends(current_user)):
    """Import a .vrm. Audited before it is written, so a file that cannot work
    as an avatar is rejected at the door rather than failing later in the
    renderer, where the only symptom is a model that never appears.

    Streamed: copied to a part file a megabyte at a time (stopping once it passes
    vrm.MAX_BYTES), audited from its header alone (vrm.audit_file never reads the
    textures), then renamed into place. A 100 MB model used to sit in memory whole."""
    d = _vrm_dir()
    part = d / f".upload-{uuid.uuid4().hex}.part"     # dot-file: never listed as a model
    try:
        size = 0
        with open(part, "wb") as out:
            while chunk := await file.read(UPLOAD_CHUNK):
                size += len(chunk)
                if size > vrm.MAX_BYTES:
                    raise HTTPException(413, f"model is larger than {vrm.MAX_BYTES // 1048576} MB")
                out.write(chunk)
        try:
            report = vrm.audit_file(part)
        except vrm.NotAVrm as e:
            raise HTTPException(400, str(e)) from e
        if not report["usable"]:
            raise HTTPException(400, "unusable as an avatar: " + "; ".join(report["problems"]))

        name = Path(file.filename or "model.vrm").name
        if not name.lower().endswith(".vrm"):
            name += ".vrm"
        if name.startswith("."):
            name = "model" + name
        dest = d / name
        stem, i = dest.stem, 1
        while dest.exists():                        # never silently replace a model
            dest = d / f"{stem}-{i}.vrm"
            i += 1
        part.replace(dest)
        report.pop("name", None)
        return {"name": dest.name, **report}
    finally:
        part.unlink(missing_ok=True)                # refused or failed: leave nothing behind


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
    path = (_vrm_dir() / Path(card.vrm).name).resolve()
    if not str(path).startswith(str(_vrm_dir().resolve())) or not path.is_file():
        raise HTTPException(404, "model file missing")
    # Same URL, different bytes whenever the card is pointed at another model,
    # so it must not be cached by the view that loads it.
    return FileResponse(path, media_type="model/gltf-binary",
                        headers={"Cache-Control": "no-store"})


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
    return {"ok": True, "forgot": _forget_pairings(cur, persona_id=persona_id)}


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

    # Seed the opening greeting (macros substituted for display).
    greeting_idx = int(body.get("greeting_index", 0))
    greetings = [card.first_mes] + list(card.alternate_greetings)
    greeting = greetings[greeting_idx] if 0 <= greeting_idx < len(greetings) else card.first_mes
    if not (greeting and greeting.strip()):
        # Fall back to the first non-empty greeting, then to a generic opener,
        # so a card that ships without a first_mes still opens the chat.
        greeting = next((g for g in greetings if g and g.strip()), "")
    if not (greeting and greeting.strip()):
        greeting = "Hello, {{user}}. What can I do for you?"
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
    return s.model_dump()


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
                # Reclassify this swipe if not already cached (and the card shows a face).
                if not m.swipe_expressions[idx]:
                    card = _load(cur.cards, s.character_id, CharacterCard)
                    labels = expressions.card_emotions(card) if card else []
                    if labels:
                        try:
                            m.swipe_expressions[idx] = await expressions.classify(
                                kobold, m.content, labels=labels)
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
    """Edit the session's pinned facts (user-curated)."""
    s = _load(cur.sessions, session_id, ChatSession)
    if not s:
        raise HTTPException(404, "session not found")
    if "pinned" in body:
        raw = body.get("pinned") or []
        if not isinstance(raw, list):
            raise HTTPException(400, "pinned must be a list of strings")
        s.memory.pinned = [str(x).strip() for x in raw if str(x).strip()]
    _save(s, cur.sessions)
    return s.memory.model_dump()


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
    """A chat ended: fold everything not yet folded, recent turns included, into
    long-term memory now.

    The page and the desktop call this when a chat ends (left, another one opened, the
    tab or window closed, the sleep phrase); the server's own idle timer
    (_schedule_sweep) and the startup catch-up are the net under them. Idempotent: a
    chat with nothing new costs no model call. It runs as a background job (one per
    chat at a time, giving way to replies) and is shielded, so a client that leaves
    straight after asking (a closing tab) doesn't cancel it.
    """
    if not _load(cur.sessions, session_id, ChatSession):
        raise HTTPException(404, "session not found")
    task = asyncio.create_task(_sweep_job(cur, session_id))
    _memory_tasks.add(task)
    task.add_done_callback(_memory_tasks.discard)
    return await asyncio.shield(task)


async def _sweep(cur: Ctx, session_id: str, kobold=kobold) -> dict:
    """The fold a sweep runs: every turn of the chat not yet folded, recent ones too."""
    session = _load(cur.sessions, session_id, ChatSession)
    if not session:
        return {"ok": True, "swept": False, "reason": "session not found"}
    card = _load(cur.cards, session.character_id, CharacterCard)
    persona = _load(cur.personas, session.persona_id, Persona) or _default_persona(cur)
    if not card:
        return {"ok": True, "swept": False, "reason": "session has no character"}

    am = _load_assistant_memory(cur, session.character_id, persona.id)
    if (am.swept_sessions.get(session.id, 0) >= len(session.messages)
            or not any(m.role == "user" for m in session.messages)):
        # Nothing new, or a chat nobody spoke in (only the greeting). Common: every
        # chat that is left or closed asks for a save, and probing the backend's
        # context size first cost seconds a time with KoboldCPP down.
        return {"ok": True, "swept": False}
    st = _load_settings(cur)
    window = await _context_tokens(st)
    am0 = am.model_copy(deep=True)
    changed = await assistant_memory.fold(
        kobold, am, session, card, persona, max_tokens=st.summary_max_tokens,
        extract_items=st.auto_facts_enabled, max_new=st.auto_facts_max_new,
        context_tokens=window)
    if changed:
        _store_fold(cur, am0, am)
        # Onto a fresh read: the session loaded before the fold would roll back a
        # message sent while it ran. Only the index moves, and only forward.
        live = _load(cur.sessions, session.id, ChatSession)
        mark = am.swept_sessions.get(session.id, 0)
        if live and mark > live.memory.last_summarized_index:
            live.memory.last_summarized_index = mark
            _save(live, cur.sessions)
    return {"ok": True, "swept": changed}


# ---- sweeps the server runs itself ---------------------------------------------------
# The idle sweep used to be a 5-minute timer in the browser, so closing the tab, a reload,
# closing the desktop window or a restart before it fired lost it: the chat's recent
# turns stayed out of long-term memory until that chat was opened again. Now every
# finished turn re-arms a timer here, and startup catches up on chats it missed.
SWEEP_IDLE_S = 5 * 60.0          # quiet time after the last reply before a chat is swept
SWEEP_RETRY_S = 60.0             # a sweep that gave way to a reply runs again after this
SWEEP_CATCHUP_DAYS = 2           # at startup, sweep chats active this recently
SWEEP_CATCHUP_DELAY_S = 60.0     # ...starting after this (let the backends come up), then
SWEEP_CATCHUP_GAP_S = 10.0       # ...this far apart, one chat at a time on the model
_sweep_timers: dict[str, asyncio.Task] = {}   # session id -> a timer still WAITING
_sweep_tasks: set[asyncio.Task] = set()       # strong refs to every timer, waiting or
                                              # sweeping. Not _memory_tasks: that set is
                                              # "work in flight", and a timer is mostly sleep


def _schedule_sweep(uid: str, session_id: str, delay: float = SWEEP_IDLE_S) -> None:
    """Arm (or re-arm) the idle sweep of one chat. Only a timer that is still waiting is
    replaced: one whose sweep has started is left to finish, because cancelling it would
    leave KoboldCPP generating for nobody."""
    old = _sweep_timers.pop(session_id, None)
    if old is not None and not old.done():
        old.cancel()
    task = asyncio.create_task(_sweep_later(uid, session_id, delay))
    _sweep_timers[session_id] = task
    _sweep_tasks.add(task)
    task.add_done_callback(_sweep_tasks.discard)


async def _sweep_later(uid: str, session_id: str, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    if _sweep_timers.get(session_id) is asyncio.current_task():
        del _sweep_timers[session_id]           # from here on it can't be re-armed away
    user = _user_by_id(uid)
    if not user:                                # deleted meanwhile: nothing to save, and
        return                                  # _ctx_for would recreate their folders
    await _sweep_job(_ctx_for(user), session_id)


async def _sweep_job(cur: Ctx, session_id: str) -> dict:
    """A sweep under the same rules as the memory job: one job per chat at a time, and a
    reply cancels it (_yield_memory_jobs). One that gave way runs again shortly, since a
    chat that has ended has no next turn to re-arm it. Never raises."""
    lock = _memory_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        k = _YieldingKobold(kobold, f"pantheon-sweep-{uuid.uuid4().hex[:12]}")
        _memory_running[session_id] = k
        try:
            out = await _sweep(cur, session_id, k)
            _memory_yields.pop(session_id, None)
            return out
        except _MemoryYielded:
            print(f"[memory] sweep of session {session_id[:8]} yielded to a reply", flush=True)
            _schedule_sweep(cur.user.id, session_id, SWEEP_RETRY_S)
            return {"ok": True, "swept": False, "reason": "gave way to a reply; runs again shortly"}
        except Exception as e:  # noqa: BLE001 — best-effort, like every memory step
            print(f"memory sweep failed for session {session_id}: {e!r}", flush=True)
            return {"ok": True, "swept": False, "reason": "failed"}
        finally:
            if _memory_running.get(session_id) is k:
                del _memory_running[session_id]


def _unswept_recent(cur: Ctx, days: float = SWEEP_CATCHUP_DAYS) -> list[str]:
    """Chats active in the last `days` with a user turn the sweep hasn't folded."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    out = []
    for s in _list(cur.sessions, ChatSession):
        if (s.updated_at or "") < cutoff or not s.character_id:
            continue
        # The persona _sweep will fold into: a deleted one falls back to the default.
        persona_id = (_load(cur.personas, s.persona_id, Persona) or _default_persona(cur)).id
        mark = _load_assistant_memory(cur, s.character_id, persona_id).swept_sessions.get(s.id, 0)
        if any(m.role == "user" for m in s.messages[mark:]):
            out.append(s.id)
    return out


def start_sweep_catchup() -> None:
    """At startup: schedule a sweep for every recent chat a restart (or a closed tab
    before this server-side timer existed) left unswept. Staggered, so the model isn't
    asked for all of them at once. Called from service.py's lifespan."""
    n = 0
    for user in _all_users():
        try:
            cur = _ctx_for(user)
            for sid in _unswept_recent(cur):
                _schedule_sweep(user.id, sid, SWEEP_CATCHUP_DELAY_S + n * SWEEP_CATCHUP_GAP_S)
                n += 1
        except Exception as e:  # noqa: BLE001 — a bad library must not stop startup
            print(f"[memory] sweep catch-up skipped a library: {e!r}", flush=True)
    if n:
        print(f"[memory] {n} recent chat(s) not yet swept: sweeping them shortly", flush=True)


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
    # The pairing's memory store tracks this chat by position too; resetting
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
# None of these touch mood: it moves on generation only, exactly as a re-roll leaves it
# alone. Text already folded into memory stays there — editing a line is not a request to rewrite what was remembered.

def _find_message(s: ChatSession, message_id: str) -> int:
    for i, m in enumerate(s.messages):
        if m.id == message_id:
            return i
    raise HTTPException(404, "message not found")


def _shift_swept(cur: Ctx, s: ChatSession, removed_from: int, count: int) -> None:
    """`count` messages starting at index `removed_from` were removed: pull the
    pairing's fold mark back so it still points at the same next-unfolded message."""
    if count <= 0 or not s.character_id:
        return
    persona = _load(cur.personas, s.persona_id, Persona) or _default_persona(cur)
    store = _load_assistant_memory(cur, s.character_id, persona.id)
    mark = store.swept_sessions.get(s.id)
    if mark is None or mark <= removed_from:
        return
    store.swept_sessions[s.id] = max(removed_from, mark - count)
    store.updated_at = datetime.now().isoformat()
    _save_assistant_memory(cur, store)


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

    A cut below what memory has already folded resets the chat's fold index, as
    clearing a chat does."""
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
    """Save a finished turn without rolling back memory written while it ran.

    A turn never changes `memory`: the memory job writes the summary, and the Memory
    modal writes pinned facts and the summary. So the stored memory always wins. This
    used to adopt it only when the summary index had moved, so an edit saved in the
    Memory modal while a reply streamed was overwritten by the copy the turn loaded."""
    fresh = _load(cur.sessions, session.id, ChatSession)
    if fresh:
        session.memory = fresh.memory
    _save(session, cur.sessions)


def _spawn_memory_job(cur: Ctx, session_id: str) -> asyncio.Task:
    task = asyncio.create_task(_memory_job(cur, session_id))
    _memory_tasks.add(task)
    task.add_done_callback(_memory_tasks.discard)
    return task


class _YieldingKobold:
    """The memory job's view of the backend: every call carries this job's genkey, so a
    reply can cancel it (_yield_memory_jobs), and a cancelled call never returns its
    partial text — it raises, and the job stores nothing."""

    def __init__(self, base, key: str):
        self.base, self.key, self.cancelled = base, key, False

    def _check(self, result):
        if self.cancelled:
            raise _MemoryYielded()
        return result

    async def generate_answer(self, prompt, params):
        self._check(None)                    # cancelled before this call: don't start it
        return self._check(await self.base.generate_answer(prompt, {**params, "genkey": self.key}))

    async def generate_answer_full(self, prompt, params):
        self._check(None)
        return self._check(await self.base.generate_answer_full(prompt, {**params, "genkey": self.key}))

    def __getattr__(self, name):
        return getattr(self.base, name)


class _MemoryYielded(BaseException):
    """A memory job stopped to let a reply through; it runs again after a later turn.

    A BaseException, like asyncio.CancelledError: the memory steps are best-effort and
    catch `Exception` around every model call, which swallowed this and let the job
    carry on (a measured run spent 71 s on a summary the reply was waiting behind)."""


# KoboldCPP does one generation at a time, and the memory job (a summary pass of up to
# 400 tokens, the long-term fold, fact extraction) runs right after `done`, so a message
# sent soon after waited behind it: measured 2026-11-04, the next reply's first token at
# 52 s instead of 2-3 s. A reply now cancels the running job (it is idempotent and runs
# again after a later turn). A job that has already yielded MEMORY_MAX_YIELDS times in a
# row is left to finish, or a quick typist would keep the summary from ever landing while
# the oldest turns are trimmed out of the prompt unsummarised.
MEMORY_MAX_YIELDS = 2
_memory_running: dict[str, _YieldingKobold] = {}
_memory_yields: dict[str, int] = {}


async def _yield_memory_jobs() -> None:
    """A reply is about to generate: stop background memory work that would delay it."""
    for sid, k in list(_memory_running.items()):
        if k.cancelled or _memory_yields.get(sid, 0) >= MEMORY_MAX_YIELDS:
            continue
        k.cancelled = True
        _memory_yields[sid] = _memory_yields.get(sid, 0) + 1
        await kobold.abort(k.key)
        # A call sent just before the abort may not have reached the backend yet, and an
        # abort for a key it doesn't know does nothing: send it again shortly.
        _memory_tasks.add(t := asyncio.create_task(_abort_later(k.key)))
        t.add_done_callback(_memory_tasks.discard)


async def _abort_later(key: str, delays=(0.5, 2.0)) -> None:
    for d in delays:
        await asyncio.sleep(d)
        await kobold.abort(key)


async def _memory_job(cur: Ctx, session_id: str) -> None:
    lock = _memory_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        k = _YieldingKobold(kobold, f"pantheon-mem-{uuid.uuid4().hex[:12]}")
        _memory_running[session_id] = k
        try:
            await _maintain_memory(cur, session_id, k)
            _memory_yields.pop(session_id, None)
        except _MemoryYielded:
            print(f"[memory] job for session {session_id[:8]} yielded to a reply", flush=True)
        except Exception as e:  # noqa: BLE001 — best-effort, as it always was
            print(f"memory maintenance failed for session {session_id}: {e!r}", flush=True)
        finally:
            if _memory_running.get(session_id) is k:
                del _memory_running[session_id]


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


async def _maintain_memory(cur: Ctx, session_id: str, kobold=kobold) -> None:
    """The memory maintenance a turn used to run before `done` (thresholds user-tunable).

    The verbatim window still has to advance or the prompt grows without bound, but
    nothing narrative comes out of it: the fold writes directives, facts and task notes
    (services/assistant_memory.py) and moves the chat's fold index."""
    session = _load(cur.sessions, session_id, ChatSession)
    if not session:
        return
    card = _load(cur.cards, session.character_id, CharacterCard)
    if not card:
        return
    persona = _load(cur.personas, session.persona_id, Persona) or _default_persona(cur)
    basis = _memory_basis(session)
    st = _load_settings(cur)
    window = await _context_tokens(st)
    keep = min(st.keep_recent, st.summarize_after - 1)
    fold_end = len(session.messages) - keep
    if (memory.needs_summary(session, st.summarize_after)
            and fold_end > session.memory.last_summarized_index):
        am = _load_assistant_memory(cur, session.character_id, persona.id)
        am0 = am.model_copy(deep=True)
        if await assistant_memory.fold(
                kobold, am, session, card, persona, upto=fold_end,
                max_tokens=st.summary_max_tokens,
                extract_items=st.auto_facts_enabled,
                max_new=st.auto_facts_max_new, context_tokens=window):
            _store_fold(cur, am0, am)
            session.memory.last_summarized_index = fold_end
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

    await _yield_memory_jobs()            # nothing in the background delays a reply

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

    asst_mem = _load_assistant_memory(cur, session.character_id, persona.id)
    lt_note = assistant_memory.block(asst_mem, card.name, persona.name)
    settings = _load_settings(cur)

    window = await _context_tokens(settings)
    # The Menu: the tools this bartender offers right now, and what their schemas cost.
    skill_ctx = skills_rt.SkillContext(user_id=cur.user.id, root=cur.cards.parent,
                                       card_id=card.id, session_id=session.id)
    tools = skills_rt.tools_for(card, skill_ctx)
    if tools:
        window = max(2048, window - skills_rt.schema_tokens(tools))
    trimmed: list = []
    messages = prompt_builder.build_messages(
        card, persona, session, longterm_note=lt_note,
        # Beats only for a card whose 3D face can show them (expressions.card_emotions).
        expr_labels=expressions.card_emotions(card),
        context_tokens=window, trimmed=trimmed,
        skills_note=skills_rt.notes_for(card, skill_ctx),
        # Another take: quote what is being replaced, so the new one differs from it.
        retake_of=_pending_swipes if req.regenerate else None)
    if trimmed and trimmed[0]:
        # Not an error: memory absorbs these turns on its next fold. Logged
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
    if tools:
        params["tools"] = [t.schema() for t in tools]
        params["tool_choice"] = "auto"

    # Inline [expr:x] beats the model emitted, in order. The last valid one wins for
    # the stored message; the classifier below is the fallback when there were none.
    tagged: list[str] = []
    emotions = expressions.card_emotions(card)
    known = set(emotions)

    async def _turn():
        acc = ""
        used_skill = False
        convo = list(messages)
        try:
            # A turn may take several passes: the model asks for tools, they run, and it
            # answers with their results (SKILLS_PLAN.md, "Chat-flow change").
            for _pass in range(MAX_TOOL_ROUNDS + 1):
                calls = None
                async for tok in kobold.chat_generate_stream(convo, params):
                    if isinstance(tok, ToolCallsEvent):
                        calls = list(tok)
                        continue
                    if isinstance(tok, ExprEvent):
                        label = str(tok).strip().lower()
                        if label in known:
                            tagged.append(label)
                            yield _sse({"expr": label})
                        continue          # never reaches the transcript or the TTS text
                    acc += tok
                    yield _sse({"token": tok})
                if not calls:
                    break
                used_skill = True
                results = []
                for call in calls[:MAX_TOOLS_PER_ROUND]:
                    tool = next((t for t in tools if t.name == call["name"]), None)
                    yield _sse({"skill": {"tool": call["name"],
                                          "status": (tool.status if tool else "") or "Working on it…"}})
                    results.append((call, await skills_rt.run(call, tools, skill_ctx)))
                convo = _with_tool_results(convo, results)
                if _pass == MAX_TOOL_ROUNDS - 1:
                    params.pop("tools", None)          # last pass: answer, don't call again
                    params.pop("tool_choice", None)
        except Exception as e:
            yield _sse({"error": str(e)})
            return

        # A label the model emitted without its brackets never reached the tag
        # parser, so scrub it here before it is stored and re-spoken on reload.
        reply = expressions.strip_bare_labels(acc.strip(), emotions)
        # Questions are for clarification only; a courtesy question tacked onto a
        # finished answer is the assistant making conversation, which is not the job.
        # See prompt_builder for why this is code and not just a rule.
        # A tool turn may end on the question the tool asked for ("the album or the live
        # recording?"); that one is the point of the turn, not a courtesy question.
        if not used_skill:
            reply = prompt_builder.strip_initiative_question(reply)
        reply = prompt_builder.strip_closing_offer(reply)
        swipes = (_pending_swipes + [reply]) if _pending_swipes else [reply]
        msg = ChatMessage(role="char", content=reply, swipes=swipes,
                          swipe_index=len(swipes) - 1)

        # The reply's emotion, for the 3D face and the mood: only for a card that has one.
        new_expr = ""
        if emotions:
            # Prefer what the model tagged inline — it is free and beat-accurate.
            # Only fall back to the classifier round-trip when it tagged nothing usable.
            for label in reversed(tagged):
                if label in known:
                    new_expr = label
                    break
            if not new_expr:
                try:
                    new_expr = await expressions.classify(kobold, reply, labels=emotions)
                except Exception:
                    new_expr = "neutral"
        msg.expression = new_expr or None

        # Update the running valence/mood (inertia): neutral holds; a clear emotion
        # advances the mood and nudges valence via EMA. Driven by the sprite label, so a
        # card without sprites keeps a neutral mood (no extra classify round-trip).
        feel = new_expr
        if feel and feel != "neutral" and not req.regenerate:
            a = 0.35
            v = expressions.valence_of(feel)
            session.mood.valence = round(max(-1.0, min(1.0, session.mood.valence * (1 - a) + v * a)), 3)
            session.mood.mood = feel

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
        _schedule_sweep(cur.user.id, session.id)    # the idle net if no "end" ever arrives
        yield _sse({"done": True, "message_id": msg.id, "expression": msg.expression,
                    "mood": session.mood.mood, "valence": session.mood.valence})

    return StreamingResponse(_keepalive(_turn), media_type="text/event-stream")


MAX_TOOL_ROUNDS = 3             # find -> play is two; a third covers one retry
MAX_TOOLS_PER_ROUND = 3


def _with_tool_results(convo: list[dict], results: list) -> list[dict]:
    """The conversation for the next pass: the assistant's tool calls and each result,
    in OpenAI form, BEFORE the trailing system note, which must stay last (it carries the
    reply-format rules; SKILLS_PLAN.md)."""
    tail = [convo[-1]] if convo and convo[-1].get("role") == "system" else []
    head = convo[:-1] if tail else list(convo)
    calls = [{"id": c["id"], "type": "function",
              "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}} for c, _ in results]
    out = head + [{"role": "assistant", "content": "", "tool_calls": calls}]
    out += [{"role": "tool", "tool_call_id": c["id"], "content": skills_rt.result_text(r)}
            for c, r in results]
    return out + tail


async def _keepalive(turn):
    """``turn()`` with a keep-alive. After the last token, a chat turn still runs model calls
    (expression) and sends nothing until ``done``, and a long prompt
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
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=7867)
