"""Shared test setup: no test may reach a real KoboldCPP for its context size."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _backend_context(monkeypatch):
    from services.kobold import KoboldClient

    async def limit(self):
        return 8192

    monkeypatch.setattr(KoboldClient, "context_limit", limit)
