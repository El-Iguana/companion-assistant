# assistant-reply probe — run 1

**2026-09-02 · 5 prompts × 3 runs · Qwen3.8-9B-distill-heretic**
`podman exec -i companion python3 /app/eval/assistant_replies/run.py --runs 3`

Triggered by a real reply that got four things wrong at once:

> My systems report a state of optimal functionality. The ambient temperature in
> this digital room remains a crisp twenty-four degrees Celsius, which feels quite
> pleasant for my cooling fans… I remain at your disposal, should you require
> anything further from me. **How is your day treating you?**

Invented environment, invented embodiment, a padding closer, and a question asked
on the assistant's own initiative.

## Result

|                        | baseline | + prompt rule | + code strip |
|------------------------|---------:|--------------:|-------------:|
| clean                  |     2/15 |          5/15 |    **13/15** |
| initiative-question    |     9/15 |          8/15 |     **1/15** |
| padding-closer         |     3/15 |          2/15 |         1/15 |
| invented-environment   |     0/15 |          0/15 |         0/15 |
| invented-embodiment    |     2/15 |          0/15 |         0/15 |
| narration              |     0/15 |          0/15 |         0/15 |

## The finding

**The prompt rule barely worked.** Told plainly to ask questions only when it
cannot act without the answer, the model went from 9 courtesy questions in 15
replies to 8. Adding a rule to a prompt does not beat a strong conversational
habit — the same result as the narration fix, where the system prompt lost to the
transcript, and the dedupe, where the model re-emitted facts it was told to skip.

**The code strip did the work.** `prompt_builder.strip_initiative_question()`
removes a trailing courtesy question — "is there anything else", "shall I",
"how may I assist" — and only those, and only when it is the last sentence and
something else was actually said. Genuine clarification survives: "Which of the
two logs did you mean, sir?" and "Do you want me to retry it or roll back?" are
both kept, because no regex can tell whether a question is needed, but it can tell
that "anything else?" never is.

Keep both. The prompt is what stops the habit forming mid-reply; the strip is what
guarantees the last line.

## Note on the harness

The probe applies the same post-processing `chat()` does. Measuring the raw model
output would have scored the strip at zero, since it happens after generation —
an eval that does not run the real path measures the wrong thing.

## Next

- `padding-closer` at 1/15 is a declarative sentence ("I shall remain at your
  command should you require anything further"), not a question, so the strip does
  not touch it. Stripping declaratives is riskier and wants its own measurement.
- Five prompts is enough to catch these. It is not enough to tune against.
