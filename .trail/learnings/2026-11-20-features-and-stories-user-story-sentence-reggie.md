---
title: "Features and stories: user story sentence and numbered Given/When/Then criteria"
author: Reggie
date: 2026-11-20
branch: main
cost: { minutes: 10, tokens: 180603 }
touches:
  - bin/fc_change.py
  - bin/fc_decide.py
  - bin/fc_evidence.py
  - bin/fc_linear.py
  - bin/fc_linear_cmd.py
  - bin/fc_plan.py
  - bin/fc_planner_brief.py
  - bin/fc_readiness.py
  - bin/fc_reconcile.py
  - bin/fc_tasks.py
---

## Decided
- Reggie asked on 2026-11-20 for the user story template on features and stories: `As a <role or persona>, I want <goal or feature>, so that <reason, benefit or value>.` On a story it is the first line of the description; on a feature it is the first line of `## Intent`, because the planner, readiness and fc_stale find a feature's design by that heading.
- Their acceptance criteria are numbered scenarios, one bullet each: `- AC1: Given ..., When ..., Then ...`, main scenario first, edge cases last. A feature now has `## Acceptance criteria` too.
- The rule lives in sections/0w.md; sections/0b.md and the planner brief point to it. Tasks keep plain bullets, since a task's criteria are checked by its test.
- `fc_tasks.parse_criteria` also reads `AC1: ...` lines typed with no bullet, and keeps the number in the criterion text.

## Tried and ruled out
- Three lines (As a / I want / So that) in place of one sentence: not checked how Linear stores single line breaks, so one sentence is the safe form.
- Making spec-check refuse a story whose criteria are not in the AC form: every story written before this would stop building.

## Watch out
- Compose never had the As a / I want / So that template before this; Intent and Goals were its only shape for the why.
