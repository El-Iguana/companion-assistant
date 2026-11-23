"""Fortuna: typed decisions in a fraction of a second (ai-stack's decision backend).

Fortuna answers a question about a piece of text: pick one of N labels (``choice``), yes/no
(``noul``) or a level (``score``), each as calibrated probabilities. It generates nothing.
Cantinero asks it what used to cost a KoboldCPP generation. Measured on amd-halo,
2026-11-23 (JevK5 4B q8):
- the emotion of her reply: 0.23 s against ~7 s;
- whether a request is music, and what kind: 0.15 s, against part of a ~12 s tool pass.

``FORTUNA_URL`` (default ``http://fortuna:8009``, on ai-net) and ``FORTUNA=off`` to disable.
Every caller has a fallback: Fortuna missing or slow is never an error, only slower.
"""
from __future__ import annotations

import os
import time

import httpx

TIMEOUT = httpx.Timeout(3.0, connect=0.5)
DOWN_FOR_S = 60.0            # after a failure, don't try again for a minute

_down_until = 0.0


def enabled() -> bool:
    return os.environ.get("FORTUNA", "on").lower() not in ("off", "0", "false", "no")


def _url() -> str:
    return (os.environ.get("FORTUNA_URL") or "http://fortuna:8009").rstrip("/")


async def choose(state: str, instructions: str, criteria: dict[str, str]) -> tuple[str, float] | None:
    """The label Fortuna picks for ``state`` and its confidence, or None when Fortuna is off,
    down or answers something unusable (the caller falls back)."""
    global _down_until
    if not enabled() or not state.strip() or time.monotonic() < _down_until:
        return None
    body = {"state": state[:4000], "questions": {"q": {"type": "choice", "instructions": instructions,
                                                       "criteria": criteria}}}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(_url() + "/v1/systemone", json=body)
        r.raise_for_status()
        data = r.json()
        a = (data.get("answers") or data).get("q") or {}
        label, conf = a.get("choice"), float(a.get("confidence") or 0)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as e:
        print(f"[fortuna] unavailable ({type(e).__name__}); falling back for {DOWN_FOR_S:.0f} s", flush=True)
        _down_until = time.monotonic() + DOWN_FOR_S
        return None
    if label not in criteria:
        return None
    return label, conf
