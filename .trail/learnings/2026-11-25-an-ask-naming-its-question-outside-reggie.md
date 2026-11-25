---
title: An ask naming its question outside the questions form never closed
author: Reggie
date: 2026-11-25
branch: main
cost: { minutes: 8, tokens: 145096 }
touches:
  - "bin/fc_ask.py#ask_gate"
  - "bin/fc_ask.py#loose_ids"
  - tests/test_ask.py
---

## Decided
- WAIT14 ("NEB-179 Q10: account has resumes ... ") stayed on the waiting list after D9 closed Q10. The line named Q10 outside the `<issue>: <n> questions <ids>; recommendation <one line>` form, so `question_ids` read no ids and ASK_HEAD no issue: the gate let it through unchecked, the waiting list stored an answer item with no ids, and `answered`/`settle_asks` skip such items. Only the reporter's next report replaced it.
- Fixed at the gate: `ask_gate` now exits with the reason (as the length limit does) when a needs-user names, outside the form, an id open on the map of an issue it names or on the feature's; without a readable map, any `Q<n>`. The reason gives the exact line to send.
- Ruled out: parsing ids loosely in `question_ids` for the waiting list. `CARD_ID` also matches `NEB-179`, and a loose parse would let bad lines past the gate's map checks instead of fixing the line.

## Watch out
- A WAIT item already stored with no ids closes only when its reporter reports again; nothing in decide reaches it.
