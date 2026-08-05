"""Companion data model.

Character cards follow the Character Card V2/V3 spec (the SillyTavern / Chub
ecosystem) so cards can be imported/exported losslessly. See services/card_io.py
for the PNG-embedded (tEXt `chara` / `ccv3`) import + export.
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now().isoformat()


# ---------------------------------------------------------------------------
# App settings (singleton — data/settings/app.json)
# ---------------------------------------------------------------------------
class AppSettings(BaseModel):
    """Global, non-per-character settings. Voice / hands-free config lives here."""
    hands_free: bool = False             # continuous open-mic listening on/off
    wake_word: str = ""                  # optional; "" = pure open-mic (no gate)
    vad_silence_ms: int = 800            # trailing silence that ends an utterance
    vad_threshold: float = 0.015         # RMS gate (0–1): speech vs. silence
    echo_suppress: float = 0.8           # ignore-herself: raise the start gate by this × her output level


# ---------------------------------------------------------------------------
# Character Card V2/V3
# ---------------------------------------------------------------------------
class LorebookEntry(BaseModel):
    """A character_book / world-info entry. Injected when a key matches recent chat."""
    keys: List[str] = []                 # trigger words
    content: str = ""                    # text injected into the prompt
    enabled: bool = True
    insertion_order: int = 100
    case_sensitive: bool = False
    name: str = ""
    priority: int = 10
    selective: bool = False              # requires a secondary key too
    secondary_keys: List[str] = []
    constant: bool = False               # always injected, no key needed
    position: str = "before_char"        # before_char | after_char
    min_stage: str = ""                  # gate: only injected at/above this relationship stage ("" = always)
    comment: str = ""


class CharacterBook(BaseModel):
    name: str = ""
    description: str = ""
    scan_depth: int = 4                  # how many recent messages to scan for keys
    token_budget: int = 512
    recursive_scanning: bool = False
    entries: List[LorebookEntry] = []
    extensions: Dict[str, Any] = {}


class CharacterCard(BaseModel):
    """Character Card V2/V3 `data` object, plus local storage fields."""
    # --- local storage (not part of the card spec) ---
    id: str = Field(default_factory=_uid)
    image: Optional[str] = None          # base64 PNG portrait (the card art)
    appearance: str = ""                 # visual prompt used to generate expression sprites
    power_dynamic: float = 0.0           # -100 ({{user}} holds power) .. +100 (character holds power); |value| = caution
    social_acceptance: float = 50.0      # 0 taboo/stigmatized .. 100 fully accepted (default context)
    tts_voice: str = ""                  # Kokoro voice id for this character ("" = default)
    expr_from_upload: bool = False       # derive expression sprites from the uploaded portrait (img2img) vs. txt2img
    expr_denoise: float = 0.55           # img2img denoise for emotion sprites (lower = closer to base/likeness)
    expressions: Dict[str, str] = {}     # emotion -> base64 PNG sprite
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)

    # --- Character Card V2 spec fields ---
    name: str = "Companion"
    description: str = ""                 # persistent character definition
    personality: str = ""                # short personality summary
    scenario: str = ""                   # current situation / setting
    first_mes: str = ""                  # opening greeting
    mes_example: str = ""                 # example dialogue (<START> blocks)
    creator_notes: str = ""
    system_prompt: str = ""              # overrides default system prompt if set
    post_history_instructions: str = ""  # injected after chat history (jailbreak/UJB)
    alternate_greetings: List[str] = []
    tags: List[str] = []
    creator: str = ""
    character_version: str = ""
    character_book: Optional[CharacterBook] = None
    extensions: Dict[str, Any] = {}

    # --- V3 additions ---
    nickname: str = ""
    group_only_greetings: List[str] = []


# ---------------------------------------------------------------------------
# Persona ({{user}}) — who the human is in the conversation
# ---------------------------------------------------------------------------
class Persona(BaseModel):
    id: str = Field(default_factory=_uid)
    name: str = "User"
    title: str = ""                      # role/title, e.g. "Ship Captain"
    description: str = ""
    background: str = ""
    image: Optional[str] = None
    is_default: bool = False


# ---------------------------------------------------------------------------
# Chat session
# ---------------------------------------------------------------------------
class ChatMessage(BaseModel):
    id: str = Field(default_factory=_uid)
    role: str                            # "char" | "user" | "system"
    content: str
    timestamp: str = Field(default_factory=_now)
    image: Optional[str] = None          # optional inline base64 PNG
    expression: Optional[str] = None     # emotion label for sprite display
    swipes: List[str] = []               # alternate generations for this turn
    swipe_index: int = 0
    swipe_expressions: List[str] = []    # cached emotion per swipe (aligned with swipes)


class MemoryState(BaseModel):
    """Long-term memory: a rolling summary plus pinned facts."""
    summary: str = ""                    # rolling summary of older turns
    pinned: List[str] = []               # author's-note style always-on facts
    last_summarized_index: int = 0       # message index up to which summary covers


class Relationship(BaseModel):
    """Persistent per (character, persona) relationship state. Multi-axis, 0-100 each."""
    character_id: str = ""
    persona_id: str = ""
    affection: float = 0.0
    trust: float = 0.0
    attraction: float = 0.0
    tension: float = 0.0
    emotional_intimacy: float = 0.0     # openness/vulnerability (gated by trust)
    physical_willingness: float = 0.0   # autonomous openness to physical intimacy -> ladder tier
    power_dynamic: Optional[float] = None       # per-relationship override of the card default
    social_acceptance: Optional[float] = None   # per-relationship override of the card default
    milestones: List[str] = []          # tier ids already reached (Phase 2, one-time events)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class MoodState(BaseModel):
    """Running emotional state with inertia. Foundation for a relationship system."""
    valence: float = 0.0     # running positive/negative position, [-1, 1]
    mood: str = "neutral"    # current display emotion (drives the portrait); persists through neutral turns
    # future: arousal, per-topic affinity, relationship/affection score


class GenerationConfig(BaseModel):
    max_tokens: int = 220
    temperature: float = 0.9
    top_p: float = 0.92
    top_k: int = 40
    rep_pen: float = 1.1
    min_p: float = 0.05
    stop_sequences: List[str] = []


class ChatSession(BaseModel):
    id: str = Field(default_factory=_uid)
    title: str = "New chat"
    character_id: str = ""
    persona_id: str = ""
    messages: List[ChatMessage] = []
    memory: MemoryState = Field(default_factory=MemoryState)
    mood: MoodState = Field(default_factory=MoodState)
    relationship: Optional["Relationship"] = None   # session-scoped snapshot (scenario mode); else persistent file
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    pending_milestone: str = ""          # one-time beat injected into the next reply after a tier crossing
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Backend connection settings
# ---------------------------------------------------------------------------
class RelationshipPreset(BaseModel):
    """A reusable scenario seed: context + starting relationship metrics for a new chat."""
    id: str = Field(default_factory=_uid)
    name: str = "New preset"
    description: str = ""
    character_id: str = ""               # optional pairing (suggested character)
    persona_id: str = ""                 # optional pairing (suggested persona)
    # context
    power_dynamic: float = 0.0           # -100 ({{user}} holds power) .. +100 (character does)
    social_acceptance: float = 50.0      # 0 taboo/stigmatized .. 100 accepted
    # starting relationship metrics (the character's genuine starting feelings)
    affection: float = 0.0
    trust: float = 0.0
    attraction: float = 0.0
    tension: float = 0.0
    emotional_intimacy: float = 0.0
    physical_willingness: float = 0.0
    scenario_note: str = ""              # optional opening/context line
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class KoboldSettings(BaseModel):
    url: str = "http://koboldcpp:5001"


class ComfyUISettings(BaseModel):
    url: str = "http://host.containers.internal:8188"
    checkpoint: str = ""
    style: str = "anime"


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    session_id: str
    input_text: str
    regenerate: bool = False             # re-roll the last char message (swipe)
