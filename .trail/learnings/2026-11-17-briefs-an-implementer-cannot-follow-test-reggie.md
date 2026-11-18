---
title: "Briefs an implementer cannot follow, test setup it had to guess, and tickets left stale"
author: Reggie
date: 2026-11-17
branch: main
cost: { minutes: 16, tokens: 266784 }
touches:
  - SKILL.md
  - bin/fc_decide.py
  - bin/fc_implement.py
  - bin/fc_planner_brief.py
  - bin/fc_readguard.py
  - bin/fc_readiness.py
  - bin/fc_reads.py
  - bin/fc_spec.py
  - bin/fc_stale.py
  - docs/cli.md
---

## Decided
- The limits of an implementer were already in sections/2.md (`delete:`) and docs/tasks.md (`kind: hand`) and planners still wrote "delete X", `git mv` and `uv lock` into briefs, so the fix is mechanical: `fc_spec.beyond_implementer` fails a dispatched task whose brief asks for a delete or rename command, a delete of a path not under `delete:`, a package manager command its `files:` do not allow, or a binary file in `files:`. The planner brief (bin/fc_planner_brief.py) now states the same limits and lists `delete:` and `kind: hand` among the keys.
- The lint is narrow on purpose: fenced blocks, the CLI's `## Route` and `## Evidence`, and any span inside the task's own `test:` are not the brief's asks, and a path must look like a file (a slash, then an extension or a trailing slash) so `w-1/2`, `application/json` and `/v4/auth/me` never match.
- uv joined the read guard as its own ecosystem: `PACKAGE_FILE` maps each manager to its package file, so a uv.lock at the root never gives a JavaScript task uv, and a pyproject.toml with no uv.lock gives no manager (poetry and pip are not allowed). `COMMANDS` holds each manager's verbs, flags and package pattern; uv gets `lock`, `sync` and `add [--dev]`.
- Test setup reaches the implementer without the planner: `fc_reads.grounding` adds the package files above the task's files and, for a new file of a known kind (`.test.mjs`, `.stories.tsx`, `test_*.py`, `*Test.php`), one existing file of that kind in the same directory, to the read guard and the prompt.
- Stale tickets (new bin/fc_stale.py): `spec-check` fails a story or feature that still says `Not shaped yet` once shaped, or keeps a readiness question past Todo; readiness holds a deep-dive issue whose tech doc was last written before a live decision on it; `compose decide` removes the placeholder on an approval and prints a note to reread the tech doc when it records any other decision on an issue that links one.
- The `deep-dive` label stays on after the deep dive: the planner brief and `fc_readiness.gates` read the tech doc only where the label is. Dropping it would hide the tech doc from both.

## Tried and ruled out
- Removing the placeholder when `compose linear update --drop-label needs-shaping` runs: fc_linear_cmd sits below fc_change in tests/modules/rank.txt, so it cannot import the section reader fc_stale needs. The approval in `compose decide` is the hook instead, and `spec-check` catches a label dropped any other way.
- Removing a stale readiness question by itself: on the ticket that started this, the question ("tech doc is not approved yet") was still true, so the CLI reports it and a person or agent answers it.
- A `move:` key for renames: `delete:` plus `files:` plus `reads: <rev>:<old path>` already carries a rename.
- Flagging `python3 ...` or `bunx ...` spans in a brief: test plans quote such commands, so the binary file in `files:` is the signal for "draw a PNG" instead.

## Watch out
- A decision line carries only a day, so a decision of the same day the tech doc was last written counts as known to the doc.
- `fc_stale.tech_doc_behind` reads `updatedAt` from READINESS_TREE's documents; a test tree without it reports no gap.
- `trail check` flags this change against unrelated learnings by keyword (Frame0 images, the herdr prompt path, Linear's data model); they were read and are not conflicts.
- Not fixed here: the test reader cannot open results/NNN.test.log from inside the worktree, and checks that ran against a stale install or a broken environment.
