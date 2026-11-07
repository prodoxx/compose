---
title: "Reflect, revise, retry: where the loops live and what compose did not do before"
author: Reggie
date: 2026-11-07
branch: worktree-reflect-loops
cost: { minutes: 1, tokens: 3831 }
touches:
  - bin/compose.py
  - bin/fc_briefs.py
  - bin/fc_history.py
  - bin/fc_implement.py
  - bin/fc_linear.py
  - bin/fc_planner_brief.py
  - bin/fc_reader.py
  - bin/fc_retro.py
  - bin/fc_run.py
  - bin/fc_trailpack.py
---

## Decided
- Reflection on a RED attempt is the test reader's job (fc_reader), not the implementer's: it already runs read-only in the clone with the output, and now gets the attempt's diff (cut at 4000 chars) and every earlier reflection. It answers DID / WHY / NEXT; the retry prompt lists the earlier reflections above "What failed". The test decides, never the reflection.
- A task that ends without passing gets a retrospective (fc_retro.record, results/retro.jsonl) with a kind: code, brief, scope, harness, environment. `failed` is code when any attempt reached a test run, else harness. The retrospective is the task's failure comment on Linear (linear_event reads result["retrospective"]).
- fc_retro ranks right after fc_tierpin so fc_briefs, fc_implement and fc_run can all import it; fc_reader ranks right before fc_implement. Moving read_failure out of fc_implement kept it under the 600-line cap (555).
- `compose retro --history` groups by route and tier, so history task lines now carry both (fc_history.FIELDS plus fc_retro.task_facts in fc_run). Older lines show as "-".
- Learnings stay in the implementer's context pack (fc_trailpack.split keeps the blocks, under LEARNED at the end); before this they were stripped on purpose. `learnings=False` keeps the old pack for a caller that wants it.

## Tried and ruled out
- Letting the retrospective or the history report change compose's defaults by itself: the report is for the product owner to read; a default change stays a human decision.
- A separate reflector call after the reader: one call too many per RED attempt; extending the reader's prompt costs nothing extra.

## Watch out
- compose has no end-to-end run_task test (the version control binary on PATH is a fake), so the wiring in run_task (record before atomic_write, then linear_event) is covered by test_retro.IssueComment and the unit tests, not by a run.
- `trail check` flags word-level matches on docstrings ("read file", "test run", "claude code"); none of the 20 on this change was a real conflict.
