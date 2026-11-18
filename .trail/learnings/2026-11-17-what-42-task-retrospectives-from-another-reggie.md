---
title: What 42 task retrospectives from another machine say about grounding
author: Reggie
date: 2026-11-17
branch: main
cost: { minutes: 21, tokens: 290849 }
touches:
  - get-compose-session-data.sh
  - bin/fc_planner_brief.py
  - bin/fc_readguard.py
---

## Decided
- Read on 2026-11-17 from a digest of another machine's compose data (get-compose-session-data.sh): 42 tasks ended without passing across 4 projects; retro.jsonl labels them 21 brief, 12 code, 9 scope. 38 were read in full.
- Exploration is not the weak step: 8 of 10 projects with teammate records ran an explore teammate, and the two codebase briefs read on Linear carry file:line, patterns, commands and risks. One of the two without an explore reused a sibling feature's brief and the planner corrected it in the worktree.
- The weak step is what reaches the implementer. Three causes cover 21 of the 38:
  - 8: the brief asks for something the sandbox forbids (delete a file, `git mv`, draw a PNG with python3, `uv lock`). bin/fc_planner_brief.py says nothing about these limits, and `LOCK_MANAGERS` (bin/fc_readguard.py:26) knows only bun, npm, pnpm and yarn, so a Python task cannot update `uv.lock`.
  - 8: the implementer guessed the repo's test setup (jsdom where the repo has happy-dom, `@testing-library/react` and `@storybook/react` that are not installed, JSX in a `.mjs` test). The codebase brief is read by the planner only, and two reflections say the read guard blocked package.json.
  - 5: the check ran in the wrong environment (a stale copy of the package imported from outside the worktree, a python3 without tomllib, a database host that did not resolve).
- retro.jsonl's `kind` under-counts brief problems: the 8 test-setup tasks are labeled `code`.

## Tried and ruled out
- Judging grounding from the teammate records alone: no explore record does not mean no brief, since the brief lives on the Linear feature and can be reused from a sibling feature.
- Cutting digest lines at 400 characters: a retro line's JSON prefix uses the budget and the WHY is lost. The script now prints each retrospective's `lines` in full.

## Watch out
- 24 of the 38 retrospectives had read refusals, 179 in all; several reflections say the test reader could not open results/NNN.test.log because it is outside the worktree.
- history.jsonl's `repo` is an absolute path, so `compose status --history` shows nothing for a history file copied from another machine.
- Counts come from the last 20 retrospectives per project and one four-day window; they are a sample, not a rate.
