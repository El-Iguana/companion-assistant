"""Skills ("the Menu"): tools a bartender can use, by native tool calling.

A **skill** is a set of tools plus the code behind them (SKILLS_PLAN.md). A card opts in
by listing skill ids in ``CharacterCard.skills``; only those tools are offered to the
model, and every tool's schema costs prompt tokens on every turn, so skills expose few,
coarse tools.

This module is the runtime the architecture doc (AGENTIC_SKILLS_MEDIA_ARCHITECTURE.md)
asks for, in-process rather than a separate service:

- **Registry** — ``register(Skill)``; ``tools_for(card, ctx)`` asks each of the card's
  skills which tools it offers right now (a skill's tools can depend on its state, e.g.
  the DJ offers only ``media_connect`` until it is paired).
- **Executor** — ``run(call, tools, ctx)``: parse and validate the model's arguments
  against the tool's schema (strict: unknown keys, wrong types, out-of-range values and
  bad enums are refused), enforce a timeout, and normalise every outcome to a dict with
  ``ok``. A failure is a typed error the model can talk about, never an exception into
  the chat turn ("a failed skill degrades to conversation").
- **Permissions** — the handler gets a ``SkillContext`` bound to the signed-in user's
  library; nothing the model sends can name another user, a URL, a path or a command.
- **Audit** — one JSON line per call in ``data/u/<uid>/skills/audit.jsonl``: operation
  id, tool, validated arguments, outcome, duration. Never a token.

The model is untrusted input: tool arguments come from a model steered by a
user-authored (and importable) card.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

TOOL_TIMEOUT_S = 20.0          # a tool that takes longer is reported as TIMEOUT
RESULT_MAX_CHARS = 2000        # what goes back to the model; it pays for every character
STRING_MAX = 300               # default cap on a string argument


@dataclass
class SkillContext:
    """Who is asking, bound server-side from the request (never from the model)."""
    user_id: str
    root: Path                 # the user's library folder, data/u/<uid>
    card_id: str = ""
    session_id: str = ""
    # The Orpheus player of the resident this request came from ("" from the web chat).
    player_id: str = ""


@dataclass
class Tool:
    name: str                                  # [a-z_]+, unique across skills
    description: str
    parameters: dict                           # JSON Schema subset, see validate()
    handler: Callable[[SkillContext, dict], Awaitable[dict]]
    status: str = ""                           # what the page shows while it runs
    timeout: float = 0                         # seconds; 0 = TOOL_TIMEOUT_S

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


@dataclass
class Skill:
    id: str                                    # what CharacterCard.skills lists
    title: str                                 # the Menu's name for it
    description: str                           # one line for the Studio
    tools: Callable[[SkillContext], list[Tool]]  # the tools it offers right now
    note: Callable[[SkillContext], str] = field(default=lambda ctx: "")  # optional prompt line


REGISTRY: dict[str, Skill] = {}


def register(skill: Skill) -> Skill:
    REGISTRY[skill.id] = skill
    return skill


def menu() -> list[dict]:
    """Every registered skill, for the Studio's Menu section."""
    return [{"id": s.id, "title": s.title, "description": s.description}
            for s in sorted(REGISTRY.values(), key=lambda s: s.title.lower())]


def card_skills(card) -> list[Skill]:
    return [REGISTRY[i] for i in (getattr(card, "skills", None) or []) if i in REGISTRY]


def tools_for(card, ctx: SkillContext) -> list[Tool]:
    out: list[Tool] = []
    for skill in card_skills(card):
        try:
            out.extend(skill.tools(ctx))
        except Exception as e:  # noqa: BLE001 — a broken skill offers nothing, chat goes on
            print(f"[skills] {skill.id} offered no tools: {e!r}", flush=True)
    return out


def notes_for(card, ctx: SkillContext) -> str:
    lines = []
    for skill in card_skills(card):
        try:
            n = skill.note(ctx)
        except Exception:  # noqa: BLE001
            n = ""
        if n:
            lines.append(n)
    return "\n".join(lines)


def schema_tokens(tools: list[Tool], chars_per_token: float = 4.0) -> int:
    """What the tool schemas cost in the prompt, for the context budget."""
    return int(len(json.dumps([t.schema() for t in tools])) / chars_per_token) + 1 if tools else 0


# --- validation -----------------------------------------------------------------------

class ArgError(ValueError):
    pass


def validate(value: Any, schema: dict, path: str = "arguments") -> Any:
    """Validate (and lightly coerce) ``value`` against a small JSON Schema subset: object
    with properties/required/additionalProperties (default False), string (maxLength,
    enum), integer/number (minimum/maximum, enum), boolean, array (items, maxItems).
    Returns the cleaned value. Strict by default: an argument the schema doesn't name is
    an error, not something to pass along."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            raise ArgError(f"{path} must be an object")
        props = schema.get("properties") or {}
        extra = set(value) - set(props)
        if extra and not schema.get("additionalProperties", False):
            raise ArgError(f"{path} has unknown field(s): {', '.join(sorted(extra))}")
        for req in schema.get("required") or []:
            if req not in value or value[req] in (None, ""):
                raise ArgError(f"{path}.{req} is required")
        return {k: validate(v, props[k], f"{path}.{k}") for k, v in value.items()
                if k in props and v is not None}
    if t == "string":
        if not isinstance(value, str):
            raise ArgError(f"{path} must be a string")
        value = value.strip()
        if len(value) > int(schema.get("maxLength", STRING_MAX)):
            raise ArgError(f"{path} is too long")
    elif t in ("integer", "number"):
        if isinstance(value, bool):
            raise ArgError(f"{path} must be a number")
        try:
            value = int(value) if t == "integer" and float(value) == int(float(value)) else float(value)
        except (TypeError, ValueError):
            raise ArgError(f"{path} must be a number") from None
        if t == "integer" and not isinstance(value, int):
            raise ArgError(f"{path} must be a whole number")
        if "minimum" in schema and value < schema["minimum"]:
            raise ArgError(f"{path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ArgError(f"{path} must be at most {schema['maximum']}")
    elif t == "boolean":
        if not isinstance(value, bool):
            raise ArgError(f"{path} must be true or false")
    elif t == "array":
        if not isinstance(value, list):
            raise ArgError(f"{path} must be a list")
        if len(value) > int(schema.get("maxItems", 20)):
            raise ArgError(f"{path} has too many items")
        return [validate(v, schema.get("items") or {}, f"{path}[{i}]") for i, v in enumerate(value)]
    else:
        raise ArgError(f"{path}: unsupported schema")
    if "enum" in schema and value not in schema["enum"]:
        raise ArgError(f"{path} must be one of: {', '.join(map(str, schema['enum']))}")
    return value


# --- execution --------------------------------------------------------------------------

def error(code: str, message: str, **extra) -> dict:
    return {"ok": False, "error": code, "message": message, **extra}


async def run(call: dict, tools: list[Tool], ctx: SkillContext) -> dict:
    """Run one tool call from the model. Never raises."""
    op = "op-" + uuid.uuid4().hex[:10]
    name = str(call.get("name") or "")
    t0 = time.monotonic()
    tool = next((t for t in tools if t.name == name), None)
    args: dict = {}
    if tool is None:
        result = error("UNKNOWN_TOOL", f"There is no tool called {name!r} on this bartender's menu.")
    else:
        try:
            raw = call.get("arguments") or "{}"
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            args = validate(parsed if parsed is not None else {}, tool.parameters)
        except (ValueError, ArgError) as e:
            result = error("BAD_ARGUMENTS", str(e))
        else:
            try:
                result = await asyncio.wait_for(tool.handler(ctx, args), tool.timeout or TOOL_TIMEOUT_S)
                if not isinstance(result, dict):
                    result = error("TOOL_FAILED", "the tool returned nothing usable")
                result.setdefault("ok", True)
            except asyncio.TimeoutError:
                result = error("TIMEOUT", f"{name} took too long")
            except Exception as e:  # noqa: BLE001 — degrade to conversation
                result = error("TOOL_FAILED", f"{name} failed: {type(e).__name__}")
                print(f"[skills] {name} failed: {e!r}", flush=True)
    result["operation_id"] = op
    _audit(ctx, op, name, args, result, time.monotonic() - t0)
    return result


def result_text(result: dict) -> str:
    """The tool message the model reads: compact JSON, capped."""
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= RESULT_MAX_CHARS else text[:RESULT_MAX_CHARS - 20] + '…"}'


def _audit(ctx: SkillContext, op: str, tool: str, args: dict, result: dict, secs: float) -> None:
    try:
        d = ctx.root / "skills"
        d.mkdir(parents=True, exist_ok=True)
        line = {"at": datetime.now().isoformat(timespec="seconds"), "op": op, "tool": tool,
                "args": args, "ok": bool(result.get("ok")), "error": result.get("error", ""),
                "card": ctx.card_id, "session": ctx.session_id, "ms": int(secs * 1000)}
        with open(d / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass                                   # an audit failure never fails the tool
