"""The calendar, worked out in code (#103): the model counted days wrong."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from services import calendar_facts as cal
from services import prompt_builder

NOW = datetime(2026, 11, 24, 15, 0)


@pytest.mark.parametrize("year,name,want", [
    (2025, "Thanksgiving", date(2025, 11, 27)), (2026, "Thanksgiving", date(2026, 11, 26)),
    (2027, "Thanksgiving", date(2027, 11, 25)), (2026, "Black Friday", date(2026, 11, 27)),
    (2025, "Easter", date(2025, 4, 20)), (2026, "Easter", date(2026, 4, 5)), (2027, "Easter", date(2027, 3, 28)),
    (2026, "Memorial Day", date(2026, 5, 25)), (2026, "Labor Day", date(2026, 9, 7)),
    (2026, "Mother's Day", date(2026, 5, 10)), (2026, "Father's Day", date(2026, 6, 21)),
    (2026, "Martin Luther King Jr. Day", date(2026, 1, 19)), (2026, "Presidents' Day", date(2026, 2, 16)),
    (2026, "Columbus Day", date(2026, 10, 12)),
])
def test_holiday_dates(year, name, want):
    assert cal.holidays(year)[name] == want


@pytest.mark.parametrize("q,said", [
    ("What day is Thanksgiving?", "Thanksgiving is Thursday, November the twenty-sixth, in two days."),
    ("How many days until Christmas?", "Christmas is in thirty-one days, on Friday, December the twenty-fifth."),
    ("What day of the week is New Year's Day?",
     "New Year's Day is Friday, January the first, 2027, in thirty-eight days."),
    ("Is today Thanksgiving?", "No, Thanksgiving is Thursday, November the twenty-sixth, in two days."),
    ("When is Halloween?", "Halloween is Sunday, October the thirty-first, 2027, in three hundred and forty-one days."),
    ("How long till Christmas Eve?", "Christmas Eve is in thirty days, on Thursday, December the twenty-fourth."),
])
def test_holiday_questions_are_answered(q, said):
    assert cal.holiday_answer(q, NOW) == said


@pytest.mark.parametrize("q", ["I love Christmas music", "Play Christmas songs", "When is my birthday?",
                               "What should I cook for Thanksgiving?", "tell me about Easter"])
def test_not_a_holiday_date_question(q):
    assert cal.holiday_answer(q, NOW) is None


def test_on_the_day_itself():
    assert cal.holiday_answer("When is Thanksgiving?", datetime(2026, 11, 26, 9)) == "Thanksgiving is today."
    assert cal.holiday_answer("Is it Thanksgiving?", datetime(2026, 11, 26, 9)) == "Yes, it's Thanksgiving today."


def test_the_model_gets_the_worked_out_calendar_for_date_talk():
    note = cal.calendar_note(NOW)
    assert "Thu Nov 26" in note and "Thanksgiving: Thursday, November 26, 2026 (in 2 days)" in note
    assert "Christmas: Friday, December 25, 2026 (in 31 days)" in note and "Tue Nov 24 (today)" in note
    for said, wants in (("What are we doing Friday?", True), ("Any plans for the weekend?", True),
                        ("How many days until my trip?", True), ("Tell me a joke", False)):
        hit = bool(prompt_builder._ASKS_TIME.search(said) or prompt_builder._ASKS_CALENDAR.search(said))
        assert hit is wants, said
