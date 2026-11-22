---
title: "Two compose blocks on a task issue after a reroute (#485): reroute is not the writer"
author: Reggie
date: 2026-11-22
branch: worktree-agent-a30428b1ef073ee91
cost: { minutes: 40, tokens: 12860 }
touches:
  - SKILL.md
  - bin/fc_linear_cmd.py
  - bin/fc_route.py
  - bin/fc_routerec.py
  - bin/fc_spec.py
  - bin/fc_tasks.py
  - docs/cli.md
  - tests/test_linear_cmd.py
  - tests/test_route.py
  - tests/test_routerec.py
---

## Decided
- A reroute cannot add a compose block: record_route reads the live body, swaps the Route text in place (with_route) and writes the body back; run against a store-backed fake Linear for three reroutes it leaves the one block alone. Nothing in bin/ writes figma or figma_nodes into a block either; those lines come from planner and designer agents (sections/0d.md asks the designer to name figma_nodes on the code task's block, and no command edits one key of a block, only quick pins route:). So the extra blocks that differ in test and figma_nodes are agent writes: a --section or --append write whose text held a block, or a --description built from a stale read. The exact writer on the three issues is not verified; #454 and #475 (fixed in 90f496aa) are the same pattern and sit on the same position (the block follows the Route section).
- The rule now sits at the write chokepoint, not in each verb: fc_linear_cmd.one_block refuses any --append, --description or create whose body holds more compose blocks than the issue has (and more than one). A body that already holds two may be written down to one. --section keeps its own refusal from 90f496aa (the owner chose refusing over replacing there).
- spec-check, compose tasks and the sync all read the first block; fc_tasks.extra_blocks is the one sentence that says how many there are and which id is read, used by spec-check's complaint and by a new compose tasks WARNING.
- record_route now waits for Linear to serve the new body back (fc_linear_cmd.await_write), like every other whole-body writer: the next writer (Evidence refresh, planner section write, next reroute) reads the body and writes all of it.
- with_route and task_from_issue find the Route section by character offsets (fc_tasks.route_span), not by searching for its text: a heading-only Route section was found by an earlier line holding the same words.

## Watch out
- The reroute never needed fixing for the symptom, so do not look for the writer in fc_reroute again; look at agent writes (planner and designer briefs) and the race with Linear serving an old body.
- Not done: a command that sets one key of a block (for example figma_nodes), which would stop the designer hand-off needing --description or --section for it.
