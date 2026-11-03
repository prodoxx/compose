---
title: "Lean defaults: why compose was slow and what the default path became"
author: Reggie
date: 2026-11-02
branch: main
cost: { minutes: 42, tokens: 300565 }
touches:
  - README.md
  - SKILL.md
  - bin/fc_decide.py
  - bin/fc_depth.py
  - bin/fc_hooks.py
  - bin/fc_linear.py
  - bin/fc_planner_brief.py
  - bin/fc_readiness.py
  - bin/fc_spec.py
  - bin/fc_verify.py
---

## Decided
- The slow part of compose was never the implementer (1.6 min median, 71% first-try pass in ~/.compose/history.jsonl); it was the gates before `compose run` and the bounces back: 90 of 170 runs failed, a quarter of task slots ended in a process verdict, and the lead could not type a one-line fix.
- Depth routing (fc_depth) only changed verify, ran at the deepest task in the whole stack, and Jev's FAST criterion made 92% of tasks STANDARD; check-only was unreachable because every task got a protected test.
- The default path is now brief, plan, build, verify: the lead writes `## Intent` (0b) and the planner starts; the shaping conversation runs only on request, the `needs-shaping` label, a policy match, or an irreversible guess.
- Stories are created and reported (`working "stories <id>: n created"`), never proposed for a go; 3b never waits unless the product owner asked to review every list.
- `route: check-only` is the default compose block; `protected:` only on a policy match or when asked.
- The guard (fc_hooks.GUARDED_ROLES) denies only the planner; LEAD_ROLES keeps `orchestrator` as its own COMPOSE_ROLE so the crew switch guard and watches still see it.
- `fc_depth.verified_tasks` skips tasks whose branch is in the last passed verify's `head` (new key in results/verify.json) when choosing the stack depth.
- `tasks`, `spec-check` and `verify` call `fc_linear.refresh_tasks(proj, say_stale=False)` like `run` does.
- `fc_readiness.gates` no longer has a "design is not approved" gate.

## Tried and ruled out
- Changing Linear's data model (one issue per task) or batching writes: left for later; too big for one change and not what made the loop wait.

## Watch out
- bin/fc_verify.py sits at exactly 600 lines (test_modules cap); the next line added there needs one removed.
- tests/test_harness.py guard helpers default to role `planner` now; a test that wants a denial must not use `orchestrator`.
- `trail check` flags keyword matches (guard, sync linear, worktree file) against unrelated learnings on this change; they were read and are not conflicts.
