---
title: "PRs carry a Risk section from each task's route"
author: Reggie
date: 2026-11-25
branch: main
cost: { minutes: 7, tokens: 77165 }
touches:
  - bin/fc_prrisk.py
  - "bin/fc_stack.py#pr_text"
  - "bin/fc_stack.py#layer_body"
  - sections/4.md
---

## Decided
- The product owner noticed compose PRs (e.g. jobscanco/chrome-extension-vue#150) say nothing about risk. Compose already makes a risk call per task when it routes it: Jev's depth (FAST/STANDARD/DEEP, defined in fc_routejev as regression risk and implementation uncertainty), raised to DEEP by a verification policy's floor. It sat only in the task issue's `## Route`.
- Every PR body now has `## Risk` after What changed: `**<level>**` of the riskiest task (FAST Low, STANDARD Medium, DEEP High), one line per task with its level and why (the policies that set the floor and their checks, else what the depth means), then the team lead's bullets from the PR file.
- The level is always compose's, read from the cached route (results/tasks.json issues), never written by hand: a PR file's Risk bullet that states a level is refused. The team lead writes what could break and where to look.
- Calls made without asking (reversible): the section's place, the level words, and reading the route rather than asking a model per PR. Ruled out: sizing risk from diff lines alone (route already weighs files, shared callers and policies), and a GitHub label (repos carry no risk labels; the body is what reviewers read).

## Watch out
- A layer whose tasks carry no Route (no Linear key, or older tasks) shows no level; the section then holds only the team lead's bullets, or is left out.
- A PR whose text a person edited keeps it: existing PRs gain the section at the next `--submit` only when compose still owns their text.
