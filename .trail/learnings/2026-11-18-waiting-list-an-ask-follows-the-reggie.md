---
title: "Waiting list: an ask follows the map its question is on, and items are WAIT<n>"
author: Reggie
date: 2026-11-18
branch: worktree-waiting-list
cost: { minutes: 32, tokens: 427218 }
touches:
  - bin/fc_decide.py
  - bin/fc_waiting.py
  - docs/cli.md
  - tests/test_decide.py
  - tests/test_waiting.py
  - bin/compose.py
  - tests/test_skillcheck.py
  - tests/test_supervision.py
  - tests/test_yolo.py
  - ".superpowers/sdd/2026-11-17-waiting-list/walk/l1.sh"
---

## Decided

- An ask (an `answer` item of the waiting list) keeps a question id only while it is open where `compose report` read it when it let the ask through: the map of the ask's own issue, or the feature's (`fc_ask.ask_gate` reads both, so a team lead may head an ask with a story while the question is written on the feature).
- The rule lives in `fc_waiting.settle_asks(proj, issue, ids, open_on, cleared)`. `cmd_decide` calls it after `clear_elsewhere`, with the maps that call cleared and a reader (`fc_decide.open_reader`) that knows the body just written and reads any other map once.
- Every ask a decide can have changed (an ask of the map it wrote, of a map it cleared, or one naming a settled id) is judged on all its ids: `report` lets an ask through while one question is open, so an ask can name an id that was decided before it was asked.
- The ids a decide settled count as open on none of the map it wrote, the maps it cleared and the feature, whatever a read that is behind the write says.
- A map that cannot be read changes nothing: what cannot be known stays on the list.
- A waiting item is `WAIT<n>` (`fc_waiting.PREFIX`), no longer `W<n>`.
- This replaces what the two earlier waiting-list learnings say about `W<n>` ids and about the story-and-feature defect being open.

## Tried and ruled out

- Sweeping an ask against its head issue's map only (`answered(..., still_open=)`): any decision on a story closed that story's ask about a question still open on the feature, so a question that still waited left the list.
- Leaving an ask of another issue alone unless its own map was cleared: the answer recorded on the feature, where the question is written, never closed the story's ask, and no command did.
- Judging only the settled ids of another issue's ask: an id decided before the ask was made never left it, and the ask stayed.
- Reading Linear inside the function that holds `results/waiting.lock`: the lock has no timeout, so a slow read during `decide` would hold up the prompt hook, the watch and `compose ack`. The maps are read before the lock; under it a map not read yet is not known.
- Keeping `W<n>`, or another prefix of one to three letters: a question id is one to three letters and a number (`fc_ask.CARD_ID`), and maps do number their questions W1, W2 and on (one project's run to W36, and tests/test_ask.py parses `7 questions W1-W7`). Four letters can never be a question id.

## Watch out

- `fc_ask.map_bodies` returns nothing while `fc_config.LINEAR_KEY` is unset, and the decide tests mock only `fc_linear.linear_gql`. A test that needs another map read patches the key (`decide_reading_maps` in tests/test_decide.py) and uses `maps_on_linear`; the default `FakeLinear` answers every issue with NEB-1's body.
- `clear_elsewhere` takes a closed id off the feature's tree even when the decide was on an issue outside it. `settle_asks` leans on that: a settled id is never open on the feature.
- An ask of an issue outside the feature's tree keeps its own question of the same id; its map is read to know.
- Both wrong closes were found by walking real `compose` processes in a sandbox, not by the suite, which passed throughout.
- `trail check` on this branch reports conflicts that are all inside `.trail/learnings/` files matched against each other; none is in code.
