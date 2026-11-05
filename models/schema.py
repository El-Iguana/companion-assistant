"""Companion data model.

Character cards follow the Character Card V2/V3 spec (the SillyTavern / Chub
ecosystem) so cards can be imported/exported losslessly. See services/card_io.py
for the PNG-embedded (tEXt `chara` / `ccv3`) import + export.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime

# The relationship models belong to Muninn's engine (muninn.models); re-exported
# so every `from models.schema import Relationship` keeps working.
from muninn.models import Relationship, RelationshipPreset  # noqa: F401
from muninn.episodic import Moment  # noqa: E402


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now().isoformat()


# ---------------------------------------------------------------------------
# App settings (singleton — data/settings/app.json)
# ---------------------------------------------------------------------------
# Context windows the Settings screen offers (tokens). 0 = Auto: use whatever the
# backend runs at. Below 4096 the memory folds (instructions + known facts + a
# 1200-token summary) no longer fit, so that is the floor.
CONTEXT_SIZES = (4096, 6144, 8192, 12288, 16384, 24576, 32768, 49152, 65536, 98304, 131072)

class AppSettings(BaseModel):
    """Global, non-per-character settings. Voice / hands-free config lives here."""
    hands_free: bool = False             # continuous open-mic listening on/off
    wake_word: str = ""                  # optional; "" = pure open-mic (no gate)
    # How the wake word gates listening.
    #   "phrase"     every utterance must start with it (the original behaviour)
    #   "continuous" it opens a conversation; everything after is heard until the
    #                sleep phrase or the idle timeout, so follow-ups need no prefix
    wake_mode: str = "phrase"            # "phrase" | "continuous"
    sleep_phrase: str = "that will be all"   # ends a continuous conversation
    farewell: str = "Goodbye."           # spoken when the conversation closes
    # An open mic that never closes is a privacy problem, not just an annoyance:
    # walk away mid-conversation and it would keep transcribing the room. 0 = never.
    sleep_after_s: int = Field(180, ge=0, le=3600)
    vad_silence_ms: int = 800            # trailing silence that ends an utterance
    vad_threshold: float = 0.015         # RMS gate (0–1): speech vs. silence
    echo_suppress: float = 0.8           # legacy level-ducking knob — superseded by full_duplex + AEC
    # --- Phase 5: interruption -------------------------------------------------
    # Half-duplex (the default) means the open mic simply does not listen while she
    # speaks: no self-hearing, but no barge-in either. Full duplex lifts that gate,
    # which is only safe when her voice cannot reach the mic — headphones, or a
    # system echo canceller. Measured on the reference box: the raw webcam mic hears
    # her at +26 dB over the noise floor, a PipeWire WebRTC AEC source at -39 dB.
    full_duplex: bool = False            # listen while she speaks (needs headphones or AEC)
    mic_device_id: str = ""              # "" = browser default; set to an AEC capture source
    mic_device_label: str = ""           # remembered for display; deviceIds are unstable
    # --- Phase 6: desktop mode -------------------------------------------------
    # Which character answers in the corner of the screen. "" = fall back to the
    # most recently used session, which is only a guess -- the desktop shell has
    # no UI for choosing, so this is where the choice lives.
    desktop_card_id: str = ""
    disable_thinking: bool = False       # append /no_think so Qwen3 skips <think> reasoning (faster replies)
    # Context window to budget prompts against (services/context.py). 0 = Auto, the
    # backend's real limit; otherwise one of CONTEXT_SIZES, never above the backend's.
    context_limit: int = 0

    @field_validator("context_limit")
    @classmethod
    def _standard_context(cls, v: int) -> int:
        if v != 0 and v not in CONTEXT_SIZES:
            raise ValueError(f"must be 0 (Auto) or one of {', '.join(map(str, CONTEXT_SIZES))}")
        return v
    # Long-term memory: rolling-summarizer tuning (services/memory.py).
    summarize_after: int = Field(24, ge=6, le=200)          # fold once this many un-summarized turns pile up
    keep_recent: int = Field(12, ge=2, le=100)              # most recent turns always kept verbatim
    summary_max_tokens: int = Field(400, ge=100, le=1200)   # token budget for the "Story so far" summary
    # Semantic recall (muninn.embeddings + muninn.recall).
    recall_enabled: bool = True                             # retrieve related past turns/facts per turn
    recall_top_k: int = Field(4, ge=1, le=10)               # max retrieved snippets injected
    auto_facts_enabled: bool = True                         # extract durable facts into long-term memory
    auto_facts_max_new: int = Field(3, ge=1, le=10)         # max facts extracted per fold
    # Long-term consolidation (muninn.consolidate): folds append new bullets and an idle
    # review deletes stale ones, instead of rewriting the whole memory every fold.
    # On by default since its fold stopped seeing stored memory and its lines are filtered:
    # clean 16/33 vs the rewrite's 9, missed 5 vs 6 (MEMORY.md, Consolidation).
    longterm_consolidate: bool = True
    # The pytincture page's colourway (Ember's palette): bone|ember|moss|dusk|harbor.
    # Per account rather than per browser, so it follows you; the old page ignores it.
    ui_theme: str = "bone"


# ---------------------------------------------------------------------------
# Auth / users (login + per-user isolated libraries)
# ---------------------------------------------------------------------------
class User(BaseModel):
    """A login account. Password is stored as a scrypt hash + per-user salt."""
    id: str = Field(default_factory=_uid)
    username: str = ""
    role: str = "user"                   # "admin" | "user"; only admins manage users
    salt: str = ""
    password_hash: str = ""
    created_at: str = Field(default_factory=_now)


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
    tts_voice: str = ""                  # voice id: Kokoro ("af_sarah") or Piper ("piper:jarvis-medium"); "" = default
    tts_speed: float = Field(1.0, ge=0.5, le=1.5)  # speaking pace; 1.0 = the voice's own tempo, lower = slower
    expr_from_upload: bool = False       # derive expression sprites from the uploaded portrait (img2img) vs. txt2img
    expr_denoise: float = 0.55           # img2img denoise for emotion sprites (lower = closer to base/likeness)
    expr_kontext: bool = True            # emotions via Flux Kontext edits when installed (else img2img)
    expr_checkpoint: str = ""            # model the sprite set was made with; single regen + the Studio default reuse it
    expressions: Dict[str, str] = {}     # emotion -> base64 PNG sprite
    # Avatar tier. "" = sprite tier (the default): this card keeps the generated
    # expression sprites and no 3D avatar. A filename here (under data/vrm/) puts the
    # card on the avatar tier. Sprites are generated per card at no manual cost; a VRM
    # is hand-authored content, so this stays opt-in per character.
    vrm: str = ""
    # Which appearance this character uses. Derivable from `vrm` being set, but
    # stored explicitly so a character can keep a model assigned while showing
    # sprites -- switching back and forth must not throw the other away.
    avatar_mode: str = "sprite"          # "sprite" | "vrm"
    # Profile picture: where the card's picture comes from (cards, rail, chat header).
    # "" = automatic, the behaviour before this field: uploaded portrait, else the
    # neutral sprite. "image" = the uploaded portrait, "sprite" = one expression sprite
    # (`portrait_sprite`), "vrm" = a still of the 3D model (`vrm_still`, rendered in the
    # browser by vtuber.mjs; `vrm_still_of` is the model file it was taken from). A
    # missing source falls back to automatic, so a card never loses its picture.
    portrait_source: str = ""            # "" | "image" | "sprite" | "vrm"
    portrait_sprite: str = "neutral"
    vrm_still: Optional[str] = None      # base64 PNG
    vrm_still_of: str = ""
    # Per-character wake word. Empty falls back to the global one in AppSettings,
    # so existing setups keep working; set it to give each character her own name
    # to answer to, which is the point of having several.
    wake_word: str = ""
    # Per-character too: the phrase that ends her conversation. "" = the global one.
    sleep_phrase: str = ""
    farewell: str = ""                   # what she says on the way out; "" = global
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
    chat_mode: str = "roleplay"          # "roleplay" (scene/world) | "assistant" (personality, no scene); picks the default system prompt
    # Pronouns used when the app writes ABOUT the character — the relationship
    # descriptor and boundary note. "she/her" | "he/him" | "they/them", or a custom
    # "subject/object/possessive". Blank keeps she/her, which is what every card
    # written before this field existed was silently getting.
    pronouns: str = ""
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
    role: str                            # "char" | "user" | "narrator" (a scene event, roleplay) | "system"
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


class MemoryFact(BaseModel):
    """One durable fact in a pairing's long-term memory.

    `origin` is load-bearing, not decoration: it is what lets the Memory modal
    show — and let the user revert — anything the app decided to remember on its
    own. Auto-extraction (MEMORY_PLAN Phase 5) is only shippable on top of this.
    """
    id: str = Field(default_factory=_uid)
    text: str = ""
    origin: str = "user"                 # "user" (hand-written) | "auto" (written by a sweep)
    created_at: str = Field(default_factory=_now)


class CharacterMemory(BaseModel):
    """Cross-session memory for one (character, persona) pairing.

    The durable sibling of ChatSession.memory, deliberately NOT folded into it:
    MemoryState is scoped to one chat and dies with it, this survives across all
    chats in the pairing. Scoped exactly like Relationship (same {cid}__{pid}
    file convention) — same character with a different persona is genuinely a
    different history.

    `swept_sessions` maps session_id -> the message index already folded in, so a
    sweep is idempotent and a session that keeps growing can be swept repeatedly
    without re-folding what it already covered.
    """
    character_id: str = ""
    persona_id: str = ""
    facts: List[MemoryFact] = []
    summary: str = ""                    # cumulative history across every chat in this pairing
    swept_sessions: Dict[str, int] = {}  # session_id -> message index folded into `summary`
    reviewed_summary: str = ""         # `summary` as the last consolidation review left it
    # Moments that moved the relationship (muninn.episodic): what {{user}} said and her
    # line, recorded when one reply moves it sharply or a milestone fires. The "why"
    # behind the relationship numbers; reviewable and removable in the Memory modal.
    moments: List[Moment] = []
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class AssistantMemory(BaseModel):
    """Memory for a task-driven assistant. The deliberate sibling of CharacterMemory.

    A companion remembers a *history*; an assistant must remember *how to behave*
    and *what is true*, and must not drift. So the two halves are stored apart:

      `directives` are standing instructions — "address me as Sir", "always give
      times in Eastern". They are obeyed, never merely known, and they are the
      last thing dropped when the injection budget runs short: silently dropping
      one changes behaviour with no error.

      `facts` are things that are true about the user. Known, not obeyed.

      `notes` replace the rolling narrative summary entirely. Terse task state —
      decisions, open threads — rewritten whole on each fold so a resolved item
      disappears instead of accumulating.

    There is no summary, no relationship, no scene, and no emotional arc, because
    an assistant has none of those and every one of them leaked into a reply the
    moment a shared code path forgot to branch.
    """
    character_id: str = ""
    persona_id: str = ""
    directives: List[MemoryFact] = []
    facts: List[MemoryFact] = []
    notes: List[str] = []
    swept_sessions: Dict[str, int] = {}
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
    # DRY sampler (KoboldCPP): penalises extending any word sequence already in the
    # context, which is what a character reusing her earlier lines is; rep_pen works on
    # single tokens only. Measured (eval/repetition): less wording reused across a chat,
    # and with the retake note an "Another take" no longer opens like the one it
    # replaces. 0 multiplier turns it off.
    dry_multiplier: float = 0.8
    dry_base: float = 1.75
    dry_allowed_length: int = 2
    dry_penalty_last_n: int = 0          # 0 = the whole context
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
class SceneRequest(BaseModel):
    """A narrator event or a draft of {{user}}'s reply (roleplay chats only)."""
    session_id: str
    direction: str = ""                  # optional: what should happen / what {{user}} wants to do
    replace: bool = False                # narrate: re-roll the narration that is the last message


class KoboldSettings(BaseModel):
    url: str = "http://koboldcpp:5001"


class ComfyUISettings(BaseModel):
    url: str = "http://comfyui:8188"
    checkpoint: str = ""
    style: str = "anime"


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    session_id: str
    input_text: str
    regenerate: bool = False             # re-roll the last char message (swipe)
    continue_turn: bool = False          # roleplay: she takes a turn with no new user message
                                         # (after a narrator event, or to carry the scene on)
