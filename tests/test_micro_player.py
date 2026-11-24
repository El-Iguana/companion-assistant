"""The micro player's buttons (desktop/musiccore.page_command, Music M1). No GTK needed."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
import musiccore as core  # noqa: E402


@pytest.mark.parametrize("msg,want", [
    ("music:toggle", ("toggle", None)),
    ("music:previous", ("previous", None)),
    ("music:next", ("next", None)),
    ("music:stop", ("stop", None)),
    ("music:open", ("open", None)),
    ("music:seek:81.5", ("seek", 81.5)),
    ("music:volume:55", ("volume", 55.0)),
    ("music:volume:250", ("volume", 100.0)),      # clamped
])
def test_buttons(msg, want):
    assert core.page_command(msg) == want


@pytest.mark.parametrize("msg", [
    "", "settings", "hud:90", "music", "music:shuffle", "music:seek", "music:seek:x",
    "music:seek:-3", "music:volume:nan", None,
])
def test_anything_else_is_ignored(msg):
    assert core.page_command(msg) is None


def test_the_snapshot_is_the_state_cantinero_gets():
    q, v = core.Queue(), core.Volume()
    q.load([{"id": "1", "title": "Back in Black", "artist": "AC/DC", "album": "Back in Black"}])
    st = core.state_message("paused", q, v, 42.0, 255.0)
    assert st["now"]["title"] == "Back in Black" and st["position_ms"] == 42000 and st["status"] == "paused"
