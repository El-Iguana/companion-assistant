"""A card's figure: female, male or not set, and what reaches the avatar.

    .venv/bin/python -m pytest -q tests/test_figure.py
"""
from __future__ import annotations

import json
from pathlib import Path

from models.schema import CharacterCard


def test_known_figures_are_kept_and_anything_else_reads_as_not_set():
    assert CharacterCard(figure="female").figure == "female"
    assert CharacterCard(figure=" Male ").figure == "male"
    assert CharacterCard(figure="robot").figure == ""        # a typo doesn't break the card
    assert CharacterCard(figure=None).figure == ""


def test_a_card_saved_before_figures_loads_as_not_set():
    old = CharacterCard(name="Iris").model_dump()
    old.pop("figure")
    assert CharacterCard(**old).figure == ""


def test_the_studio_saves_it():
    # BFF modules need pytincture's browser side to import; read EDITABLE from the source.
    import ast
    src = (Path(__file__).parent.parent / "appcode/bff/studio.py").read_text()
    node = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "EDITABLE")
    assert "figure" in ast.literal_eval(node.value)


def test_mira_is_seeded_female():
    mira = json.loads((Path(__file__).parent.parent / "seed/characters/mira.json").read_text())
    assert CharacterCard(**mira).figure == "female"


def test_the_card_the_avatar_gets_carries_it():
    from pantheon_web.desktop import _card_info
    assert _card_info(CharacterCard(name="Iris", figure="male"))["figure"] == "male"
