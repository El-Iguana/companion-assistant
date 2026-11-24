"""Task notes are changed by number, not rewritten (Memory Phase 7, #17).

    .venv/bin/python -m pytest -q tests/test_notes_items.py
"""
from __future__ import annotations

from datetime import datetime, timedelta

from models.schema import AssistantMemory, MemoryFact
from services import assistant_memory as am

T0 = datetime(2026, 11, 1, 12, 0)


def _mem(*notes):
    m = AssistantMemory(notes=list(notes))
    m.notes_touched = {n: T0.isoformat() for n in notes}
    return m


def test_a_thread_the_fold_doesnt_name_stays():
    m = _mem("Switch the DNS for the blog", "Pick a hotel in Lisbon")
    am.apply_note_changes(m, "NEW: Buy pasta sheets on Friday", "Mira", "Brian", now=T0 + timedelta(days=1))
    assert m.notes == ["Switch the DNS for the blog", "Pick a hotel in Lisbon", "Buy pasta sheets on Friday"]
    assert m.notes_touched["Switch the DNS for the blog"] == T0.isoformat()          # untouched keeps its age


def test_done_and_edit_by_number():
    m = _mem("Switch the DNS for the blog", "Pick a hotel in Lisbon")
    am.apply_note_changes(m, "DONE 2\nEDIT 1: Switch the DNS on Friday", "Mira", "Brian", now=T0 + timedelta(days=2))
    assert m.notes == ["Switch the DNS on Friday"]
    assert m.notes_touched == {"Switch the DNS on Friday": (T0 + timedelta(days=2)).isoformat(timespec="seconds")}


def test_none_and_nonsense_change_nothing():
    m = _mem("Switch the DNS for the blog")
    for raw in ("NONE", "", "DONE 7", "EDIT 9: whatever", "Updated list:", "(empty)", "</assink>"):
        am.apply_note_changes(m, raw, "Mira", "Brian")
        assert m.notes == ["Switch the DNS for the blog"], raw


def test_a_model_that_rewrites_the_list_anyway_doesnt_duplicate():
    m = _mem("Switch the DNS for the blog")
    am.apply_note_changes(m, "- Switch the DNS for the blog\n- Buy pasta sheets on Friday", "Mira", "Brian")
    assert m.notes == ["Switch the DNS for the blog", "Buy pasta sheets on Friday"]


def test_restated_instructions_and_status_lines_are_not_notes():
    m = _mem()
    m.directives = [MemoryFact(text="Keep answers short")]
    am.apply_note_changes(m, "NEW: Keep answers short\nNEW: Mira is operational\nNEW: Book the dentist",
                          "Mira", "Brian")
    assert m.notes == ["Book the dentist"]


def test_stale_threads_leave_the_prompt_but_stay_stored():
    m = _mem("Old thread")
    m.notes.append("Fresh thread")
    m.notes_touched["Fresh thread"] = (T0 + timedelta(days=20)).isoformat()
    assert am.live_notes(m, now=T0 + timedelta(days=13)) == ["Old thread", "Fresh thread"]
    assert am.live_notes(m, now=T0 + timedelta(days=21)) == ["Fresh thread"]
    assert "Old thread" not in am.block(m, "Mira", "Brian", now=T0 + timedelta(days=21))
    assert m.notes == ["Old thread", "Fresh thread"]


def test_over_the_cap_the_stalest_go():
    words = ["apples", "boiler", "camera", "dentist", "engine", "fence", "garage", "hedge",
             "invoice", "jacket", "kettle", "ladder"]
    m = _mem(*[f"Sort out the {w}" for w in words])
    m.notes_touched["Sort out the apples"] = (T0 + timedelta(days=5)).isoformat()      # touched since
    am.apply_note_changes(m, "NEW: Renew the passport", "Mira", "Brian", now=T0 + timedelta(days=6))
    assert len(m.notes) == am.MAX_NOTES and "Sort out the apples" in m.notes and "Renew the passport" in m.notes
    assert "Sort out the boiler" not in m.notes                                         # the stalest left
