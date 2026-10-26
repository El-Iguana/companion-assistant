"""Background jobs for work that outlasts a BFF call — ComfyUI sprite generation.

A full 28-sprite set takes minutes; a BFF call is capped at 30 s. So the Studio starts a
job (an asyncio task on the server's own loop, where Pantheon's ComfyUI client lives) and
polls ``status``. Plain module, not a BFF: pytincture re-executes BFF modules per call, and
a registry kept in one would be empty on the next.

Jobs belong to the user who started them, are kept in memory only (a restart forgets
them; the sprites they already saved stay on the card), and finished ones are dropped
after an hour. One generation per card at a time: ComfyUI runs prompts one after another
anyway, and two jobs racing to write the same card's sprites help nobody.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field

_ids = itertools.count(1)
_KEEP_S = 3600


@dataclass
class Job:
    id: str
    user_id: str
    card_id: str
    label: str
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    state: str = "running"          # running | done | error
    error: str = ""
    result: dict = field(default_factory=dict)
    task: object = None

    def public(self) -> dict:
        end = self.finished or time.time()
        return {"id": self.id, "card_id": self.card_id, "label": self.label, "state": self.state,
                "error": self.error, "result": self.result, "elapsed": round(end - self.started)}


_jobs: dict[str, Job] = {}


class Busy(RuntimeError):
    pass


def _prune() -> None:
    now = time.time()
    for jid in [j.id for j in _jobs.values() if j.finished and now - j.finished > _KEEP_S]:
        _jobs.pop(jid, None)


def start(user_id: str, card_id: str, label: str, coro_factory) -> Job:
    """Run ``coro_factory()`` in the background. Refuses a second job on the same card."""
    _prune()
    for j in _jobs.values():
        if j.card_id == card_id and j.state == "running":
            raise Busy(f"already {j.label.lower()} for this character")
    job = Job(id=f"j{next(_ids)}", user_id=user_id, card_id=card_id, label=label)

    async def run() -> None:
        try:
            job.result = await coro_factory() or {}
            job.state = "done"
        except Exception as exc:  # noqa: BLE001 - reported to the page, not raised
            detail = getattr(exc, "detail", None)
            job.error = str(detail or exc) or exc.__class__.__name__
            job.state = "error"
        finally:
            job.finished = time.time()

    job.task = asyncio.get_running_loop().create_task(run())
    _jobs[job.id] = job
    return job


def get(user_id: str, job_id: str) -> Job | None:
    job = _jobs.get(job_id)
    return job if job is not None and job.user_id == user_id else None


def running_for(user_id: str, card_id: str) -> Job | None:
    return next((j for j in _jobs.values()
                 if j.user_id == user_id and j.card_id == card_id and j.state == "running"), None)
