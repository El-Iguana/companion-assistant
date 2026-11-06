"""Story memory: episodes and world details in their own index, scenario chats' own
recall, and a pairing's files deleted with its character or persona.

    .venv/bin/python -m pytest -q tests/test_story_memory.py

No model and no embedder: both are stubbed (a bag-of-words hash stands in for BGE).
"""
from __future__ import annotations

import asyncio
import hashlib

import numpy as np
import pytest

import main
from muninn import embeddings, recall
from models.schema import (CharacterCard, CharacterMemory, ChatMessage, ChatSession,
                           Persona, Relationship)
from services import memory
from test_chat_stream import cur  # noqa: F401 — fixture

DIM = 384


def _encode(texts, is_query=False):
    out = np.zeros((len(texts), DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        for w in (t or "").lower().split():
            w = w.strip(".,!?:;'\"()")
            if w:
                out[i, int(hashlib.sha256(w.encode()).hexdigest()[:8], 16) % DIM] += 1.0
        out[i, 0] += 0.1
    return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-12)


@pytest.fixture()
def embed(monkeypatch):
    monkeypatch.setattr(embeddings, "available", lambda: True)
    monkeypatch.setattr(embeddings, "encode", _encode)


class Kob:
    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    async def generate_answer(self, prompt, params):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else "NONE"


CARD, PERSONA = CharacterCard(name="Mira Vale"), Persona(name="Jonah")


def _msg(role, text):
    return ChatMessage(role=role, content=text)


# --- world lines ---------------------------------------------------------------------------

def test_world_lines_keep_narration_and_her_sentences_that_name_others():
    msgs = [
        _msg("narrator", "Rain hammers the shutters of the Gilded Gull."),
        _msg("char", "*She leans in.* Old Bess runs this place. She owes Captain Imra a favour. "
                     "I like the rain."),
        _msg("char", "Jonah, you look tired. Mira thinks so too."),          # only the two of them
        _msg("user", "Tell me about Bess."),                                  # his turns: the turn index
    ]
    lines = memory.world_lines(msgs, "Mira Vale", "Jonah")
    assert lines == ["Narrator: Rain hammers the shutters of the Gilded Gull.",
                     "Mira Vale: Old Bess runs this place.",          # "Old" opens it; "Bess" is a name
                     "Mira Vale: She owes Captain Imra a favour."]


def test_a_possessive_name_still_counts():
    assert memory._names_other_than("The ship was Bess's pride.", {"mira", "jonah"})
    assert not memory._names_other_than("That was Mira's idea.", {"mira", "jonah"})


# --- episodes ------------------------------------------------------------------------------

def test_episode_note_is_short_dated_and_none_means_nothing():
    note = asyncio.run(memory.episode_note(
        Kob("Jonah and Mira hired Pell to sail them to Gull Rock under fog."),
        "Jonah: Can you take us?", "Mira Vale", "Jonah", when="Nov 6"))
    assert note == "Earlier (Nov 6): Jonah and Mira hired Pell to sail them to Gull Rock under fog."
    assert asyncio.run(memory.episode_note(Kob("NONE"), "x", "Mira", "Jonah")) == ""
    assert asyncio.run(memory.episode_note(Kob("ok"), "x", "Mira", "Jonah")) == ""   # too short


# --- the fold writes the story index ---------------------------------------------------------

def _story_chat():
    s = ChatSession(character_id="c", persona_id="p")
    s.messages = [_msg("user", "Where can we find a boat at this hour?"),
                  _msg("char", "Pell keeps a skiff at the Tar Docks. He owes me."),
                  _msg("narrator", "A foghorn sounds somewhere past the breakwater."),
                  _msg("user", "Then let's go and wake Pell up right now.")]
    return s


def test_a_fold_indexes_episodes_and_world_lines_beside_the_turns(embed, tmp_path):
    s, mem = _story_chat(), CharacterMemory()
    turns, story = tmp_path / "x.vec.npz", tmp_path / "x.story.vec.npz"
    kob = Kob("- Jonah wants a boat.", "They went to the Tar Docks to find Pell and his skiff.")
    assert asyncio.run(memory.fold_into_longterm(kob, mem, s, CARD, PERSONA, consolidate=True,
                                                 index_path=turns, story_path=story))
    _, meta = recall._index_load(story)
    kinds = sorted(m["kind"] for m in meta)
    assert kinds == ["episode", "world", "world"]
    assert any(m["text"].startswith("Earlier (") and "Tar Docks" in m["text"] for m in meta)
    _, tmeta = recall._index_load(turns)
    assert all(m["kind"] == "turn" for m in tmeta) and len(tmeta) == 2     # her lines stay out of it


def test_a_failed_fold_writes_no_story(embed, tmp_path):
    s, mem = _story_chat(), CharacterMemory()
    story = tmp_path / "x.story.vec.npz"
    assert not asyncio.run(memory.fold_into_longterm(Kob("Here is the updated memory:"), mem, s,
                                                     CARD, PERSONA, consolidate=True, story_path=story))
    assert not story.exists()


def test_without_story_path_nothing_changes(embed, tmp_path):
    s, mem = _story_chat(), CharacterMemory()
    kob = Kob("- Jonah wants a boat.")
    asyncio.run(memory.fold_into_longterm(kob, mem, s, CARD, PERSONA, consolidate=True))
    assert len(kob.prompts) == 1                                         # no episode call


def test_story_index_is_capped_world_lines_first(embed, tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "STORY_MAX_ENTRIES", 3)
    p = tmp_path / "s.story.vec.npz"
    memory.story_add(p, [{"text": "Earlier: an episode about the lighthouse", "kind": "episode"}])
    memory.story_add(p, [{"text": f"Mira: Pell sails the skiff number {i}", "kind": "world"}
                         for i in range(4)])
    _, meta = recall._index_load(p)
    assert len(meta) == 3 and meta[0]["kind"] == "episode"


def test_story_search_has_its_own_gate(embed, tmp_path):
    p = tmp_path / "s.story.vec.npz"
    memory.story_add(p, [{"text": t, "kind": "world"} for t in (
        "Mira: Pell keeps a skiff at the Tar Docks",
        "Narrator: A foghorn sounds past the breakwater",
        "Mira: Old Bess runs the Gilded Gull inn",
        "Narrator: Snow settles on the chapel roof")])
    assert memory.search_story(p, "who keeps a skiff at the docks") == []   # too small to gate on
    memory.story_add(p, [{"text": t, "kind": "world"} for t in (
        "Mira: The lighthouse keeper is called Aldous", "Narrator: Gulls wheel over the harbour wall",
        "Mira: Corso cheats at dice in the Gilded Eel", "Narrator: The tide turns early tonight")])
    hits = memory.search_story(p, "who keeps a skiff at the docks")
    assert hits and "skiff" in hits[0]["text"]


# --- scenario chats remember themselves ---------------------------------------------------

def test_a_scenario_chat_records_and_recalls_its_own_story(cur, embed, monkeypatch):  # noqa: F811
    card = CharacterCard(name="Mira Vale")
    main._save(card, cur.cards)
    persona = main._default_persona(cur)
    s = ChatSession(character_id=card.id, persona_id=persona.id, relationship=Relationship())
    s.messages = _story_chat().messages
    main._save(s, cur.sessions)
    turns, story = main._scene_index_paths(cur, s.id)
    n = asyncio.run(memory.record_scene(Kob("They set out to find Pell at the Tar Docks."),
                                        s, card, persona, 0, 4, turns, story))
    assert n == 3 and turns.exists() and story.exists()
    # Deleting the chat deletes its memory.
    asyncio.run(main.delete_session(s.id, cur=cur))
    assert not turns.exists() and not story.exists()


# --- deleting a character or persona deletes the pairing's files ----------------------------

def _touch(*paths):
    for p in paths:
        p.write_text("{}")


def test_deleting_a_character_deletes_its_memories_and_relationships(cur):  # noqa: F811
    card = CharacterCard(name="Gone")
    main._save(card, cur.cards)
    keep = CharacterCard(name="Kept")
    mine = [cur.memories / f"{card.id}__p1.json", cur.memories / f"{card.id}__p1.vec.npz",
            cur.memories / f"{card.id}__p1.story.vec.npz", cur.assistant / f"{card.id}__p1.json",
            cur.relationships / f"{card.id}__p1.json"]
    theirs = [cur.memories / f"{keep.id}__p1.json", cur.relationships / f"{keep.id}__p1.json"]
    _touch(*mine, *theirs)
    r = asyncio.run(main.delete_card(card.id, cur=cur))
    assert r["forgot"] == 5 and not any(p.exists() for p in mine)
    assert all(p.exists() for p in theirs)


def test_deleting_a_persona_deletes_every_pairing_with_it(cur):  # noqa: F811
    p = Persona(name="Old me")
    main._save(p, cur.personas)
    mine = [cur.memories / f"c1__{p.id}.json", cur.memories / f"c2__{p.id}.vec.npz",
            cur.relationships / f"c1__{p.id}.json"]
    other = cur.memories / "c1__someone-else.json"
    _touch(*mine, other)
    asyncio.run(main.delete_persona(p.id, cur=cur))
    assert not any(x.exists() for x in mine) and other.exists()


def test_a_wildcard_id_deletes_nothing(cur):  # noqa: F811
    f = cur.memories / "c1__p1.json"
    _touch(f)
    assert main._forget_pairings(cur, character_id="*") == 0
    assert main._forget_pairings(cur, persona_id="../x") == 0
    assert f.exists()


def test_an_episode_that_continues_the_story_is_dropped():
    for story in ('Mira: *orders two more ales* "If we\'re staying, we might as well enjoy it."',
                  '*She glances at the map again.*', 'Mira says, "Gull Rock isn\'t far."'):
        assert asyncio.run(memory.episode_note(Kob(story), "x", "Mira", "Jonah")) == ""
    mixed = "Jonah won Corso's map in a dice game at the Gilded Eel.\nMira: \"Nice roll.\""
    assert asyncio.run(memory.episode_note(Kob(mixed), "x", "Mira", "Jonah")) == \
        "Earlier: Jonah won Corso's map in a dice game at the Gilded Eel."


def test_an_episode_heading_is_not_part_of_the_note():
    note = asyncio.run(memory.episode_note(
        Kob("The key events:\nJonah won the map from Corso at the Gilded Eel."), "x", "Mira", "Jonah"))
    assert note == "Earlier: Jonah won the map from Corso at the Gilded Eel."


def test_a_long_episode_ends_at_a_sentence():
    long = ("Jonah won the map from Corso in a dice game at the Gilded Eel. " * 3
            + "Then they hired Pell to row them out past the breakwater towards Gull Rock in the fog.")
    note = asyncio.run(memory.episode_note(Kob(long), "x", "Mira", "Jonah"))
    assert note.endswith(".") and len(note) <= memory.RECALL_MAX_CHARS


def test_italics_around_a_whole_summary_are_unwrapped_and_fragments_dropped():
    assert asyncio.run(memory.episode_note(
        Kob("*Mira and Jonah hired Pell to take them by ferry to Gull Rock.*"), "x", "Mira", "Jonah")) \
        == "Earlier: Mira and Jonah hired Pell to take them by ferry to Gull Rock."
    assert asyncio.run(memory.episode_note(
        Kob("Jonah won the map at the Gilded Eel. Corso lost the dice game and handed over the map to"),
        "x", "Mira", "Jonah")) == "Earlier: Jonah won the map at the Gilded Eel."
    assert asyncio.run(memory.episode_note(
        Kob("Corso lost the dice game and handed over the map to"), "x", "Mira", "Jonah")) == ""


def test_a_contracted_pronoun_is_not_a_name():
    own = {"mira", "jonah"}
    assert not memory._names_other_than("Then I'm learning proper knots this week.", own)
    assert not memory._names_other_than("And we'll see. I'll pack the biscuits.", own)
    assert memory._names_other_than("Then I'm learning knots from Pell.", own)
