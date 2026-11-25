"""Shared test setup: no test may touch the real library or a real KoboldCPP.

The throwaway library is set HERE, before pytest imports any test module. Each
module used to set it itself, before its own `import main`; a module that imported
main first (or a helper module that did) bound main to ./data, the live library
the container mounts, and its fixtures created accounts there (44 of them, found
2026-11-04). `main` reads PANTHEON_DATA_DIR once, at import.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_data = os.environ.get("PANTHEON_DATA_DIR") or tempfile.mkdtemp(prefix="pantheon-test-")
if Path(_data).resolve() == (ROOT / "data").resolve():
    raise pytest.UsageError("tests must not run against the real library (PANTHEON_DATA_DIR=./data)")
os.environ["PANTHEON_DATA_DIR"] = _data
# No test reaches a real Fortuna either; a test that wants one fakes services.fortuna.choose.
os.environ["FORTUNA"] = "off"


def pytest_sessionstart(session):
    import main
    if Path(main.DATA).resolve() == (ROOT / "data").resolve():
        raise pytest.UsageError(f"main was bound to the real library {main.DATA}")


@pytest.fixture(autouse=True)
def _backend_context(monkeypatch, request):
    from services.kobold import KoboldClient

    async def limit(self):
        return 8192

    monkeypatch.setattr(KoboldClient, "context_limit", limit)

    async def abort(self, genkey):
        return None

    monkeypatch.setattr(KoboldClient, "abort", abort)

    # The music catalog rebuilds itself in the background from Orpheus (#80): never in a test
    # (test_music_catalog calls build() itself, against a fake).
    from services.skills import music_catalog
    monkeypatch.setattr(music_catalog, "refresh_soon", lambda root, st: None)

    # A model call a test didn't stub fails at once, as a dead backend would,
    # instead of trying the network. Tests stub the instance (main.kobold), which wins.
    # test_generate_answer tests those methods themselves, over a stubbed HTTP client.
    if request.node.module.__name__.endswith("test_generate_answer"):
        return

    async def no_backend(self, *a, **k):
        raise RuntimeError("no KoboldCPP in tests")

    monkeypatch.setattr(KoboldClient, "generate_answer", no_backend)
    monkeypatch.setattr(KoboldClient, "generate_answer_full", no_backend)


@pytest.fixture(autouse=True)
def _orpheus_device_is_bound(monkeypatch):
    """No test reaches a real Orpheus to check the device's profile (#112): it's bound.
    test_orpheus_profile.py fakes me/profiles/bind_self itself."""
    from services.skills import orpheus_media as om

    async def me(base, token):
        return {"binding": "bound", "profile": {"name": "admin"}}
    monkeypatch.setattr(om, "me", me)


@pytest.fixture(autouse=True)
def _no_background_tempo(monkeypatch):
    """No test starts a background tempo build (#114); test_music_tempo.py calls build itself."""
    from services.skills import music_tempo

    monkeypatch.setattr(music_tempo, "refresh_soon", lambda root, st: None)
