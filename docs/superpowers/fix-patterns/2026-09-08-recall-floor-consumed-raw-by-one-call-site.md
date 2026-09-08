# Fix pattern: a recall floor consumed raw by one of its call sites

**Date:** 2026-09-08
**Origin:** production — a session parked on a Monitor sat under NEEDS YOU as
"Answer needed"; asked what it needed, it replied "Nothing — I don't need an answer
from you." Fixed in `SessionOrchestrator._persist_status`
(`server/sessions/orchestrator.py`), with the docs corrected in
`server/sessions/background_tasks.py` and `server/sessions/open_question.py`.

## The bug signature

A deliberately **over-broad** detector is written as the *recall floor* of a narrowing
judge: the floor answers "could this be X?" cheaply and the judge answers "is it X?"
expensively. The pairing is sound, and the floor's breadth is deliberate — a miss is
the expensive error, a false positive is meant to be caught downstream.

Then a **second** call site asks the same question and calls the floor directly. It now
has the breadth with none of the narrowing, and the floor's design comment — which says
its false positives are cheap — is *true only of the call site that narrows*. Nothing
flags it, because both call sites read as correct in isolation and the floor's own tests
all still pass: they test the floor, and the floor is behaving exactly as specified.

The instance: `background_tasks.text_awaits_user` is a substring scan of the last 800
chars for phrases like `"your call"` / `"let me know"` / `"want me to"`.
`open_question.completion_awaits_user` runs it first and then narrows a `True` with a
gated Haiku call. The completion auto-archive gate used the judge. The parked-turn
overlay used the floor:

```python
prose = strip_markers(self._last_assistant_prose(session_id))
status = ("waiting_question" if text_awaits_user(prose)   # ← floor, not judge
          else "waiting_external")
```

## Why it stayed invisible for so long

1. **The false positive is only visible to the user, and only as absence.** The card said
   "Answer needed"; opening it revealed no question. There is no error, no log line, and
   no assertion that can fire — the status pipeline did exactly what it was told.
2. **The prose that trips it is prose about deferring, not asking.** The message named a
   decision that would arise *if* a flake recurred ("that's your call rather than
   something I'd do unasked"). A phrase list cannot separate a conditional from an ask.
3. **The disclaimer re-arms the badge.** Because the scan matches the phrase rather than
   its sense, "I overstated it by saying 'that's your call'" contains `"your call"` and
   trips again — so the session could not talk its way out of the hold. Verified: both
   the original message and its retraction return `True`.
4. **Test symmetry hid it.** Both call sites had tests asserting `waiting_question` on
   asking prose. The overlay's passed through the floor; the gate's through the judge.
   Same assertion, different mechanism.

## Fix shape

Route **every** call site through the judge; leave exactly one caller on the floor — the
judge itself. The judge already composes the floor internally, so this is a substitution,
not an addition:

```python
prose = strip_markers(self._last_assistant_prose(session_id))
awaits = await completion_awaits_user(prose, cwd=getattr(rec, "cwd", None))
status = "waiting_question" if awaits else "waiting_external"
```

Two properties make this safe to put on the hot status path, and both are worth checking
before copying the shape:

- **The floor still runs first**, inside the judge. A clean parked turn returns `False`
  with no model call at all, so the cost lands only on turns that already trip the phrase
  list.
- **Every degrade path returns the floor's own verdict** (no usable account, breaker
  cooled down, SDK failure, unparseable output → `True`). A helper outage reverts to the
  old behaviour exactly. The change can only *remove* false holds, never bury a real
  question.

Then fix the floor's docstring to say it is a floor and name its single legitimate
caller. The stale docstring naming `_persist_status` as a direct consumer is what would
let the next author re-add a raw call in good faith.

## Where to look for siblings

Swept 2026-09-08; the shape appears once in this repo.

- **Any cheap detector paired with an expensive judge.** Grep for the judge, then grep
  for the detector: if the detector has non-test callers other than the judge, that is
  the bug. Here `run_helper_call` enumerates all seven gated LLM lanes
  (`open_question`, `pr_intent`, `autopilot_decider`, `model_pick`, `goal_inference`,
  `title_generator`, `prompt_refiner`).
- **The other six are NOT this shape.** They degrade to a null/unresolved verdict
  (`None`, "unresolved") rather than to a shared heuristic that anything else can call,
  so there is no floor to consume raw. Degrade-to-null is the pattern to prefer when the
  caller can act on "don't know"; a floor is only needed when it cannot.
- **Read the comment as a scope claim.** A comment justifying breadth ("a false positive
  is a harmless nudge") is an assertion about *the caller that narrows*. When you find
  one, check who else calls it — the comment is where the invariant is written down, and
  it is not enforced anywhere.

## Regression tests

- `server/tests/test_orchestrator_status_name.py` —
  `test_persist_status_parked_prose_ask_is_narrowed_by_the_classifier` pins the judge to
  `False` on the verbatim prod prose and asserts `waiting_external`. The two sibling
  parked-ask tests now stub the judge explicitly: unstubbed they passed through the
  degrade-to-`True` path, i.e. green for the wrong reason.
