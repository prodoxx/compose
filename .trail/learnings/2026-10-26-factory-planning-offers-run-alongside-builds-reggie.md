---
title: Factory planning offers run alongside builds and reach later waves
author: Reggie
date: 2026-10-26
branch: main
cost: { minutes: 29, tokens: 335439 }
touches:
  - bin/fc_factoryturn.py
  - bin/fc_nightprep.py
  - commands/factory.md
  - docs/how-it-works.md
  - tests/test_docs.py
  - tests/test_factory.py
  - bin/fc_delivery.py
  - tests/modules/fc_delivery.txt
  - tests/test_delivery.py
  - bin/compose.py
---

## Decided
- fc_factoryturn.pick always returns the first ticket to plan (out["offer"]); _turn offers it whenever no planning round is open and offers are not declined, even in a turn that dispatches or has a ready story waiting for a slot. The product owner asked for planning to go on in parallel with builds.
- Entries whose feature waits on another (e["waits_on"]) are judged for planning but never picked to build: classify is skipped for them.
- JUDGE_LIMIT counts only fresh verdicts (r["cached"] false), so walking past many kept verdicts to a later-wave story costs nothing.
- Night mode's IDLE (fc_nightprep.prep_idle) also runs while builds go: it spawns one prep worker at a time and respects the night drain.
## Tried and ruled out
- `compose factory status` is not a check for live runs: it said "not running" while a tanstack-start run was mid-turn. `compose update` (without --force) is what names live runs.
## Watch out
- An offered later-wave story is not "out of order" (that is only a ticket other than the offer), so move-up does not fire for it.
- The factory loop is long-lived: new code needs /compose:factory stop then /compose:factory.
