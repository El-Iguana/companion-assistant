"""The four Muninn modules Cantinero uses, copied into this repo.

Muninn (JotunheimPress/muninn) is the memory package shared with Whimsy and Rhapsode. It
is private, so a public Cantinero can't build against it. Copied from commit 0b61f3d
(2026-11-23); changes since are made here and are not synced back or forth.

  context   the context window: effective size, token estimate, history fit
  fold      splitting a range of turns into context-sized fold passes
  emotion   emotion label sets and valence
  text      strip_reasoning: only the answer from a model's output, before it is stored
"""
