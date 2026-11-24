"""The calendar, worked out in code (#103): holidays, weekdays, days until.

The local model can't count days. Given today's date, it said Christmas was 28 days away
(it was 31), New Year's Day a Monday (a Friday), and in a live chat "Thanksgiving was
yesterday" (it was two days off) (2026-11-24). So:

- ``holiday_answer``: "when is Thanksgiving?", "how many days until Christmas?" answered here,
  said aloud (quick.py calls it first);
- ``calendar_note``: for any other question about dates, the next three weeks and the coming
  holidays, with their weekdays and how far off they are, in the trailing note, so the model
  reads the answer instead of computing it.

US holidays: the user is in the US (America/New_York).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta


def _nth(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th weekday (Mon=0) of a month; n=-1 is the last."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Western Easter (the anonymous Gregorian computus)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def holidays(year: int) -> dict[str, date]:
    tg = _nth(year, 11, 3, 4)
    return {
        "New Year's Day": date(year, 1, 1),
        "Martin Luther King Jr. Day": _nth(year, 1, 0, 3),
        "Valentine's Day": date(year, 2, 14),
        "Presidents' Day": _nth(year, 2, 0, 3),
        "St. Patrick's Day": date(year, 3, 17),
        "Easter": _easter(year),
        "Mother's Day": _nth(year, 5, 6, 2),
        "Memorial Day": _nth(year, 5, 0, -1),
        "Father's Day": _nth(year, 6, 6, 3),
        "Juneteenth": date(year, 6, 19),
        "Independence Day": date(year, 7, 4),
        "Labor Day": _nth(year, 9, 0, 1),
        "Columbus Day": _nth(year, 10, 0, 2),
        "Halloween": date(year, 10, 31),
        "Veterans Day": date(year, 11, 11),
        "Thanksgiving": tg,
        "Black Friday": tg + timedelta(days=1),
        "Christmas Eve": date(year, 12, 24),
        "Christmas": date(year, 12, 25),
        "New Year's Eve": date(year, 12, 31),
    }


# What people call them; matched on the normalized question.
ALIASES = {
    "New Year's Day": r"new years? day|new years?(?! eve)",
    "Martin Luther King Jr. Day": r"(?:martin luther king|mlk)(?: jr)?(?: day)?",
    "Valentine's Day": r"valentines?(?: day)?",
    "Presidents' Day": r"presidents? day",
    "St. Patrick's Day": r"(?:st |saint )?patricks? day",
    "Easter": r"easter(?: sunday)?",
    "Mother's Day": r"mothers? day",
    "Memorial Day": r"memorial day",
    "Father's Day": r"fathers? day",
    "Juneteenth": r"juneteenth",
    "Independence Day": r"independence day|(?:the )?(?:fourth|4th) of july|july (?:fourth|4th)",
    "Labor Day": r"labou?r day",
    "Columbus Day": r"columbus day|indigenous peoples? day",
    "Halloween": r"hallowe?en",
    "Veterans Day": r"veterans? day",
    "Thanksgiving": r"thanksgiving(?: day)?",
    "Black Friday": r"black friday",
    "Christmas Eve": r"christmas eve",
    "Christmas": r"christmas(?! eve)(?: day)?|xmas",
    "New Year's Eve": r"new years? eve",
}
_ALIAS_RE = [(name, re.compile(rf"\b(?:{pat})\b")) for name, pat in ALIASES.items()]


def next_occurrence(name: str, today: date) -> date:
    """This year's, or next year's once this year's has passed."""
    d = holidays(today.year)[name]
    return d if d >= today else holidays(today.year + 1)[name]


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower().replace("'", "")).split())


def named_holiday(text: str) -> str:
    """The one holiday a question names, or ""."""
    t = _norm(text)
    hits = [name for name, rx in _ALIAS_RE if rx.search(t)]
    return hits[0] if len(hits) == 1 else ""


# --- said aloud ---------------------------------------------------------------------------------

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
         "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def number_words(n: int) -> str:
    """0-999."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + (f"-{_ONES[o]}" if o else "")
    h, r = divmod(n, 100)
    return f"{_ONES[h]} hundred" + (f" and {number_words(r)}" if r else "")


def _how_far(days: int) -> str:
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days < 7:
        return f"in {number_words(days)} days"
    weeks, rest = divmod(days, 7)
    if rest == 0 and weeks <= 8:
        return f"in {number_words(weeks)} week{'s' if weeks > 1 else ''}"
    return f"in {number_words(days)} days"


def spoken_day(d: date, today: date) -> str:
    """"Thursday, November the twenty-sixth" (with the year when it isn't this one)."""
    from services.quick import _ordinal
    out = f"{d.strftime('%A')}, {d.strftime('%B')} the {_ordinal(d.day)}"
    return out + (f", {d.year}" if d.year != today.year else "")


_HOLIDAY_ASK = re.compile(r"\b(when|what day|what date|which day|how many days|how long|until|till|til|"
                          r"is it|is today|is tomorrow|what weekday)\b")


def holiday_answer(text: str, now: datetime | None = None) -> str | None:
    """"When is Thanksgiving?" -> "Thanksgiving is Thursday, November the twenty-sixth, in two
    days." None when the line isn't plainly a question about one holiday's date."""
    q = _norm(text)
    if not q or len(q) > 80 or not _HOLIDAY_ASK.search(q):
        return None
    name = named_holiday(q)
    if not name:
        return None
    today = (now or datetime.now().astimezone()).date()
    d = next_occurrence(name, today)
    days = (d - today).days
    if re.search(r"\bis (?:it|today) \b", q + " ") and days == 0:
        return f"Yes, it's {name} today."
    if re.search(r"\bis (?:it|today) \b", q + " "):
        return f"No, {name} is {spoken_day(d, today)}, {_how_far(days)}."
    if re.search(r"\bis tomorrow\b", q):
        return f"Yes, {name} is tomorrow." if days == 1 else f"No, {name} is {spoken_day(d, today)}, {_how_far(days)}."
    if days == 0:
        return f"{name} is today."
    if re.search(r"\b(how many days|how long|until|till|til)\b", q):
        return f"{name} is {_how_far(days)}, on {spoken_day(d, today)}."
    return f"{name} is {spoken_day(d, today)}, {_how_far(days)}."


# --- for the model --------------------------------------------------------------------------------

def calendar_note(now: datetime | None = None, days_ahead: int = 21, holidays_ahead: int = 120) -> str:
    """The next three weeks (each with how many days off it is) and the coming holidays,
    worked out, for the trailing note."""
    today = (now or datetime.now().astimezone()).date()

    def day(i: int) -> str:
        d = today + timedelta(days=i)
        return f"{d.strftime('%a %b')} {d.day} " + ("(today)" if i == 0 else "(tomorrow)" if i == 1 else f"(+{i})")
    week = "; ".join(day(i) for i in range(days_ahead))
    coming = []
    for y in (today.year, today.year + 1):
        for name, d in holidays(y).items():
            if 0 <= (d - today).days <= holidays_ahead:
                coming.append((d, name))
    hol = "; ".join(f"{name}: {d.strftime('%A, %B')} {d.day}, {d.year} (in {(d - today).days} days)"
                    if d != today else f"{name}: today" for d, name in sorted(coming))
    last = sorted(((today - holidays(today.year)[n]).days, n) for n in holidays(today.year)
                  if 0 < (today - holidays(today.year)[n]).days <= 30)
    past = "; ".join(f"{n}: {(today - timedelta(days=ago)).strftime('%A, %B')} "
                     f"{(today - timedelta(days=ago)).day} ({ago} days ago)" for ago, n in last)
    out = (f"Calendar, worked out (authoritative; use it, never count days yourself). "
           f"The next {days_ahead} days: {week}. Coming holidays: {hol or 'none soon'}.")
    if past:
        out += f" Recently: {past}."
    return out
