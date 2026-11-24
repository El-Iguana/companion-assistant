"""Quick answers (#101): the time, the date and what's playing, without the model.

"Mira, what time is it?" was a full model turn, ~20 s, for a line the clock already knows.
A cheap word gate leaves most turns alone; Fortuna names the question at ``QUICK_MIN`` or more
(time, date, what's playing, or something else); the answer is composed here for the ear and
said aloud. Anything unsure, or about another day, place or event ("what time is the game
tomorrow?"), goes to her as before.
"""
from __future__ import annotations

import re
from datetime import datetime

QUICK_MIN = 0.8
MAX_LEN = 80                 # a quick question is short; a long line is a conversation

INTENTS = {
    "time": "asks what time it is right now, here",
    "date": "asks today's date, or what day of the week it is today",
    "playing": "asks the name or the singer of the song that is playing right now",
    "other": "anything else: a remark about the music, a request to play or change it, other songs or "
             "artists, another day, another place's time, an event's time, conversation",
}

# Most turns never reach Fortuna: only these words can make a quick question.
_GATE = re.compile(r"\b(time|clock|date|day|today|playing|song|track|tune|singing|sings|singer|"
                   r"this|listening)\b", re.I)
# Plainly about something else: left to her without asking Fortuna.
_NOT_NOW = re.compile(r"\b(tomorrow|yesterday|tonight|next|last|ago|until|till|since|in \w+ (?:hours?|days?|minutes?)|"
                      r"timer|alarm|remind|meeting|game|show|flight|appointment|birthday)\b", re.I)
# A quick answer is to a question: "I love this song." and "Play this song again." are not
# (Fortuna read both as "what's playing", 0.9).
_QUESTION = re.compile(r"\?\s*$|^(?:what|what's|whats|who|who's|whos|which|is|do|does|can|could|tell me|"
                       r"got|any idea|know)\b", re.I)
_TODAY = re.compile(r"\b(?:it|today|today's|now|the date)\b", re.I)
_HEAD = r"^\s*(?:(?:hey|ok|okay)\b[ ,]*)?(?:{name}\b[ ,!.]*)?(?:(?:so|um|uh|and)\b[ ,]*)*"


_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
         "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty"]
_ORD = ["", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
        "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
        "eighteenth", "nineteenth"]


def _words(n: int) -> str:
    """0-59 as words: 7 -> "seven", 42 -> "forty-two"."""
    if n < 20:
        return _ONES[n] or "zero"
    t, o = divmod(n, 10)
    return _TENS[t] + (f"-{_ONES[o]}" if o else "")


def _ordinal(n: int) -> str:
    """1-31: "first", "twentieth", "twenty-fourth", "thirty-first"."""
    if n < 20:
        return _ORD[n]
    t, o = divmod(n, 10)
    return _TENS[t] + "-" + _ORD[o] if o else _TENS[t][:-1] + "ieth"


def _part(h: int) -> str:
    return ("in the morning" if 3 <= h < 12 else "in the afternoon" if 12 <= h < 17
            else "in the evening" if 17 <= h < 21 else "at night")


def spoken_time(now: datetime) -> str:
    """"It's quarter past three in the afternoon." Said, not read: no digits for the voice."""
    h, m = now.hour, now.minute
    if (h, m) == (0, 0):
        return "It's midnight."
    if (h, m) == (12, 0):
        return "It's noon."
    hour = _words(h % 12 or 12)
    if m == 0:
        clock = f"{hour} o'clock"
    elif m == 15:
        clock = f"quarter past {hour}"
    elif m == 30:
        clock = f"half past {hour}"
    elif m == 45:
        clock = f"quarter to {_words((h + 1) % 12 or 12)}"
    else:
        clock = f"{hour} {'oh ' + _words(m) if m < 10 else _words(m)}"
    return f"It's {clock} {_part(h)}."


def spoken_date(now: datetime) -> str:
    """"It's Tuesday, November the twenty-fourth." """
    return f"It's {now.strftime('%A')}, {now.strftime('%B')} the {_ordinal(now.day)}."


def spoken_playing(players: list[dict], prefer: str = "") -> str:
    """What's on: the player the question came from, else one that is playing."""
    on = [d for d in players if d.get("online")]
    pick = next((d for d in on if d.get("id") == prefer), None)
    playing = [d for d in on if (d.get("state") or {}).get("status") == "playing"]
    if pick is None or (pick.get("state") or {}).get("status") != "playing":
        pick = playing[0] if playing else pick
    st = (pick or {}).get("state") or {}
    now = st.get("now") or {}
    if st.get("status") not in ("playing", "paused") or not now.get("title"):
        return "Nothing's playing."
    by = f" by {now['artist']}" if now.get("artist") else ""
    return (f"This is {now['title']}{by}." if st["status"] == "playing"
            else f"{now['title']}{by}, paused.")


def candidate(text: str, name: str = "") -> str:
    """The question without her name, or "" when it can't be a quick one."""
    head = re.compile(_HEAD.replace("{name}", re.escape(name) if name else "(?!)"), re.I)
    q = head.sub("", text or "").strip()
    if not q or len(q) > MAX_LEN or not _GATE.search(q) or _NOT_NOW.search(q) or not _QUESTION.search(q):
        return ""
    return q


async def answer(text: str, name: str = "", *, players=None, prefer: str = "",
                 now: datetime | None = None) -> str | None:
    """Her line for a quick question, or None (hers). ``players``: a callable returning the
    user's residents with their state, for "what's playing"; None when the card has no DJ."""
    from services import fortuna

    q = candidate(text, name)
    if not q:
        return None
    labels = dict(INTENTS)
    if players is None:
        labels.pop("playing")
    got = await fortuna.choose(q, "What is the user asking?", labels)
    if got is None or got[0] == "other" or got[1] < QUICK_MIN:
        return None
    now = now or datetime.now().astimezone()
    if got[0] == "time":
        return spoken_time(now)
    if got[0] == "date":
        # About today, not some named day: "What day is Thanksgiving?" read as the date (0.87).
        return spoken_date(now) if _TODAY.search(q) else None
    try:
        return spoken_playing(players(), prefer)
    except Exception:                              # noqa: BLE001 — hers, then
        return None
