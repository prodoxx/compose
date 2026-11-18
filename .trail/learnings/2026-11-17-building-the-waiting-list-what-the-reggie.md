---
title: "Building the waiting list: what the reviews found that the plan missed"
author: Reggie
date: 2026-11-17
branch: worktree-waiting-list
cost: { minutes: 71, tokens: 547478 }
touches:
  - bin/compose.py
  - bin/fc_hooks.py
  - sections/0h.md
  - tests/test_docs.py
  - tests/test_skillcheck.py
  - tests/test_redo_chain.py
  - tests/test_waiting.py
  - bin/fc_waiting.py
  - bin/fc_show.py
  - docs/cli.md
---

## Decided
- `results/waiting.json` per project (bin/fc_waiting.py) is brought up to date on every read from files compose already writes (crew status files, resume.json, verify.json, layers.json); only confirm items are opened and closed by a command (`compose decide` reconciles them against the map it just wrote).
- An item closed as `settled` may open again under the same key; one closed by a no, `decided` or `pushed` stays closed. A source file that cannot be parsed changes nothing that round.
- An ask that names question ids stores them and its issue, is also opened from the watch's `event crew=... state=needs-user` line (the status file's last line may already be past it), and closes only when a decide leaves none of its questions open.
- The prompt hook never asks the terminal host (`probe=False`): a herdr call can block for its 120 second timeout, and the hook runs on every prompt.
- `sync` catches every exception and says why on stderr; `decide`'s waiting-list block does the same on stdout: a list failure never stops a watch, a hook or a decide.
- `compose show run --line` reads the newest run-end record of the run history, never run.log plus run-cost.json: a resume writes resume.log and rewrites the cost file, so the two disagree.

## Tried and ruled out
- Closing every ask at the reporter's next report: 2 of 15 past asks with question ids were followed by the reporter's `done` within six seconds, so the item closed before the person could answer.
- Matching answered questions by id alone: every map starts at Q1, so a decide on one issue closed the asks of another.
- Handing the footer once at the start of a turn and calling it done: a turn that runs `decide` or `ack` ends on stale lines, so the note also says when to fetch it again.
- Recording a `needs-user` line refused for its length as a refused report: most past asks were over 300 characters, and each refusal woke the manager as urgent.

## Watch out
- Still open: an ask headed by a story whose question is written only on the feature's map never closes. The fix needs a decision on whether a question id means the same question on a story's map and the feature's (`clear_elsewhere` says yes, the issue scoping in `answered` says no).
- bin/fc_crew.py sits at exactly 600 lines and bin/fc_watch.py at 594 (the cap is 600): put new code in a lower module and pass data in.
- A hermetic test that lets a role-less prompt hook run reaches git through `fc_waiting.gather`; mock it in setUp (tests/test_skillcheck.py, tests/test_harness.py do).
- `trail check` on this branch flags 20 lines; all are phrase matches against unrelated learnings (for example the import line of bin/compose.py).
