"""Assistant data model.

Character cards follow the Character Card V2/V3 spec (the SillyTavern / Chub
ecosystem) so cards can be imported/exported losslessly. See services/card_io.py
for the PNG-embedded (tEXt `chara` / `ccv3`) import + export.

Pydantic ignores unknown fields, so JSON written by the roleplay app this repo was
split from (chat_mode, power_dynamic, a session's relationship, pending_milestone,
allow_side_characters, ...) still loads; those fields are dropped on the next save.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
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
    # Assistant memory fold tuning (services/assistant_memory.py).
    summarize_after: int = Field(24, ge=6, le=200)          # fold once this many un-folded turns pile up
    keep_recent: int = Field(12, ge=2, le=100)              # most recent turns always kept verbatim
    summary_max_tokens: int = Field(400, ge=100, le=1200)   # token budget for one fold pass
    auto_facts_enabled: bool = True                         # extract instructions + facts into memory
    auto_facts_max_new: int = Field(3, ge=1, le=10)         # max entries extracted per fold
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
    comment: str = ""


class CharacterBook(BaseModel):
    name: str = ""
    description: str = ""
    scan_depth: int = 4                  # how many recent messages to scan for keys
    token_budget: int = 512
    recursive_scanning: bool = False
    entries: List[LorebookEntry] = []
    extensions: Dict[str, Any] = {}


# Orpheus TTS left the stack. A card saved with one of its voices keeps a distinct voice
# of the same character instead of collapsing onto a single default.
LEGACY_ORPHEUS_VOICES = {
    "tara": "af_heart", "leah": "af_sarah", "jess": "af_jessica", "mia": "af_nova",
    "zoe": "af_sky", "leo": "am_michael", "dan": "am_adam", "zac": "am_puck",
}


def current_voice_id(voice: str) -> str:
    """A voice id the TTS engines can speak; maps a saved `orpheus:<name>` to Kokoro."""
    if voice.startswith("orpheus:"):
        return LEGACY_ORPHEUS_VOICES.get(voice[len("orpheus:"):], "af_heart")
    return voice


class Tone(BaseModel):
    """How a character speaks, each from -1 to 1 (0 = no lean either way). Rendered into
    the prompt as one "Voice and tone" line (prompt_builder.tone_note)."""
    warmth: float = 0.0          # reserved .. warm
    formality: float = 0.0       # casual .. formal
    playfulness: float = 0.0     # serious .. playful
    detail: float = 0.0          # brief .. thorough
    femininity: float = 0.0      # masculine .. feminine speech style

    @field_validator("warmth", "formality", "playfulness", "detail", "femininity", mode="before")
    @classmethod
    def _clamp(cls, v) -> float:
        # Out-of-range or junk values clamp rather than fail: a card must always load.
        try:
            return max(-1.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.0


class CharacterCard(BaseModel):
    """Character Card V2/V3 `data` object, plus local storage fields."""
    # --- local storage (not part of the card spec) ---
    id: str = Field(default_factory=_uid)
    image: Optional[str] = None          # base64 PNG portrait (the card art)
    appearance: str = ""                 # visual prompt used to generate expression sprites
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
    # The figure the 3D avatar stands as: which poses she uses and how she stands in them
    # (vtuber.mjs POSE_SETS). "" = not set, the shared set every card had before.
    figure: str = ""                     # "" | "female" | "male"
    # Voice and tone: how they talk, alongside the free-text personality.
    tone: Tone = Field(default_factory=Tone)
    # The Menu: ids of the skills this bartender may use (services/skills). Opt-in per card.
    skills: List[str] = []
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
    name: str = "Assistant"
    description: str = ""                 # persistent character definition
    personality: str = ""                # short personality summary
    scenario: str = ""                   # background (spec field; injected as "Background:")
    first_mes: str = ""                  # opening greeting
    mes_example: str = ""                 # example dialogue (<START> blocks)
    creator_notes: str = ""
    system_prompt: str = ""              # overrides prompt_builder.DEFAULT_ASSISTANT if set
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

    @field_validator("tts_voice")
    @classmethod
    def _speakable_voice(cls, v: str) -> str:
        return current_voice_id(v)

    @field_validator("figure", mode="before")
    @classmethod
    def _known_figure(cls, v) -> str:
        # Anything else (a typo in hand-edited JSON) reads as "not set" rather than
        # failing to load the card.
        v = str(v or "").strip().lower()
        return v if v in ("female", "male") else ""


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
    role: str                            # "char" | "user" | "system" ("narrator" only in sessions from the roleplay app; never prompted)
    content: str
    timestamp: str = Field(default_factory=_now)
    image: Optional[str] = None          # optional inline base64 PNG
    expression: Optional[str] = None     # emotion label for sprite display
    swipes: List[str] = []               # alternate generations for this turn
    swipe_index: int = 0
    swipe_expressions: List[str] = []    # cached emotion per swipe (aligned with swipes)


class MemoryState(BaseModel):
    """A chat's own memory: pinned facts plus how far the assistant fold has read.

    `summary` is never written or injected here; it exists so a session copied from
    the roleplay app keeps its recap until assistant_memory.fold() clears it."""
    summary: str = ""                    # roleplay-era recap only; cleared by the fold
    pinned: List[str] = []               # author's-note style always-on facts
    last_summarized_index: int = 0       # message index up to which memory has folded


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


class AssistantMemory(BaseModel):
    """Memory for a task-driven assistant.

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
    """Running emotional state with inertia; drives the sprite or the avatar's face."""
    valence: float = 0.0     # running positive/negative position, [-1, 1]
    mood: str = "neutral"    # current display emotion (drives the portrait); persists through neutral turns


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
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Backend connection settings
# ---------------------------------------------------------------------------
class KoboldSettings(BaseModel):
    url: str = "http://koboldcpp:5001"


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    session_id: str
    input_text: str
    regenerate: bool = False             # re-roll the last char message (swipe)
