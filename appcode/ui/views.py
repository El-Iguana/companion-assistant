"""Links to the classic page, for what the new page does not do yet (voice, until Phase 6)."""
from __future__ import annotations

import js


def classic_url(fragment: str = "") -> str:
    """The old page. Same host, port 7861 (the new page is 7866 during the transition)."""
    return f"{js.window.location.protocol}//{js.window.location.hostname}:7861/{fragment}"
