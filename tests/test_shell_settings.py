"""The resident's own Settings (desktop/shellsettings.py, RESIDENT_PLAN.md R6.2). No GTK needed."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
import shellsettings as ss  # noqa: E402


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_defaults_without_a_file():
    assert ss.load() == {"show_at_start": True, "listening_at_start": True, "dance_to_music": False,
                         "dance_clip": ""}


def test_round_trip(config_home):
    ss.save({"show_at_start": False, "listening_at_start": True, "dance_to_music": True, "dance_clip": "lambada"})
    assert ss.load() == {"show_at_start": False, "listening_at_start": True, "dance_to_music": True,
                         "dance_clip": "lambada"}
    assert (config_home / "pantheon-desktop" / "settings.json").exists()


def test_bad_values_and_unknown_keys_are_ignored(config_home):
    p = config_home / "pantheon-desktop" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"show_at_start": "no", "listening_at_start": False, "x": 1}))
    assert ss.load() == {"show_at_start": True, "listening_at_start": False, "dance_to_music": False,
                         "dance_clip": ""}


def test_unreadable_file_gives_the_defaults(config_home):
    p = config_home / "pantheon-desktop" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert ss.load() == ss.DEFAULTS


def test_page_script_hands_listening_to_the_page():
    assert ss.page_script({"listening_at_start": False}) == 'window.__cantineroShell = {"listening": false};'


def _fake_systemctl(monkeypatch, states):
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 0, states[args[1]] + "\n", "")
        for u in args[1:]:
            states[u] = "enabled" if args[0] == "enable" else "disabled"
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ss, "_systemctl", run)
    return calls


def test_login_state(monkeypatch):
    states = dict.fromkeys(ss.UNITS, "enabled")
    _fake_systemctl(monkeypatch, states)
    assert ss.login_state() == "on"
    states["cantinero-shell.service"] = "disabled"
    assert ss.login_state() == "off"                 # one of two is not "on"
    states["cantinero-shell.service"] = "not-found"
    assert ss.login_state() == "none"                # not installed: the switch is greyed out


def test_set_login_enables_and_disables_both(monkeypatch):
    states = dict.fromkeys(ss.UNITS, "enabled")
    calls = _fake_systemctl(monkeypatch, states)
    assert ss.set_login(False) == "off"
    assert ("disable", *ss.UNITS) in calls
    assert ss.set_login(True) == "on"
    assert not any(c[0] in ("start", "stop") for c in calls)   # never starts or stops her


def test_a_dance_choice_of_the_wrong_type_is_ignored(config_home):
    p = config_home / "pantheon-desktop" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"dance_clip": 7, "dance_to_music": True}))
    assert ss.load()["dance_clip"] == "" and ss.load()["dance_to_music"] is True


def test_the_dance_selector_lists_auto_and_the_bounce_first():
    got = ss.dance_choices([{"id": "lambada", "name": "Lambada", "bpm": 150},
                            {"id": "charleston", "name": "Charleston", "bpm": 180}])
    assert [v for v, _ in got] == ["", ss.GENERATED, "charleston", "lambada"]
    assert got[0][1].startswith("Auto") and "bounce" in got[1][1]
    assert ss.fits(100) == "In Auto it dances to songs of about 80–125 BPM."
