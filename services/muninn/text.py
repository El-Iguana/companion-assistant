"""Cleaning a model's answer before it is stored as memory.

Both apps ask for memory folds, fact extraction and classifications through a
ChatML turn with an empty `<think></think>` pre-filled, the way a Qwen-style
reasoning model is told to skip its reasoning. Models that don't follow that
convention write their
reasoning as plain text anyway and close it with a stray tag, or end the answer
with a tag from some other template. KoboldCPP returns all of it, and the old
cleanup only removed a matched `<think>…</think>` pair, so reasoning and the
prompt's own rules ("DO NOT record Mira's own replies…") were stored as
memory.

`strip_reasoning` is deliberately about tags and the one meta line that follows
them. Reasoning with no tag at all can't be told apart from an answer here;
that belongs to each memory profile's output checks.
"""
from __future__ import annotations

import re

# Closing tags models use to end a reasoning block. The answer is whatever
# follows the LAST one; a matched <think>…</think> is the ordinary case of this.
_REASONING_CLOSE = re.compile(
    r"</\s*(?:think|thinking|thought|thoughts|reasoning|reflection|scratchpad)\s*>", re.I)
# An opening reasoning tag with no close: everything from it on is reasoning.
_REASONING_OPEN = re.compile(
    r"<\s*(?:think|thinking|thought|thoughts|reasoning|reflection|scratchpad)\s*>", re.I)
# End-of-turn markers from any template. The answer ended there; anything after
# is the model writing the next turn.
_END_OF_TURN = re.compile(
    r"</\s*(?:assistant|response|answer|output|reply)\s*>"
    r"|<\|(?:im_end|eot_id|end|endoftext|end_of_turn)\|>|</s>|<end_of_turn>"
    r"|<\|im_start\|>|\[/?INST\]", re.I)
# Opening wrappers some models put around the answer itself.
_LEADING_WRAPPER = re.compile(
    r"^\s*(?:<\s*(?:assistant|response|answer|output|reply)\s*>|<\|im_start\|>assistant)\s*", re.I)
# A first line that talks about the task instead of doing it, as left behind
# after a reasoning tag: "I need to create a concise memory bullet list based on
# Jonah's statements and actions:". Only dropped when real content follows.
_META_LINE = re.compile(
    r"^\s*(?:I need to|I'll|I will|I should|I must|Let me|Let's|Now I|Okay,|OK,|Alright,"
    r"|Here(?:'s| is| are)\b[^\n]*:\s*$)", re.I)


def strip_reasoning(text: str) -> str:
    """The answer part of a model's output: no reasoning, no template tags."""
    if not text:
        return ""
    closes = list(_REASONING_CLOSE.finditer(text))
    if closes:
        text = text[closes[-1].end():]
    else:
        opened = _REASONING_OPEN.search(text)
        if opened:                       # reasoning that never closed: nothing usable after it
            text = text[:opened.start()]
    text = _LEADING_WRAPPER.sub("", text)
    end = _END_OF_TURN.search(text)
    if end:
        text = text[:end.start()]
    text = text.strip()

    if closes:                           # the meta line only ever follows a reasoning tag
        first, _, rest = text.partition("\n")
        if rest.strip() and _META_LINE.match(first):
            text = rest.strip()
    return text
