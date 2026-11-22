---
title: "figma judge: failed means a confirmed NOT_MET; a criterion another task owns is set aside"
author: Reggie
date: 2026-11-22
branch: worktree-agent-a61297d16cebd30fc
cost: { minutes: 9, tokens: 98523 }
touches:
  - bin/fc_figma.py
  - bin/fc_figmascope.py
  - bin/fc_models.py
  - docs/cli.md
  - sections/0d.md
---

## Decided
- The judge never took criteria from the story: criteria_from_tasks reads each task's own '## Acceptance criteria'. The planner copied a story-wide criterion (bind each value to a variable) into every task, including token-only ones, so the hypothesis in #487 (story criteria, screen cap) was wrong; the screen cap only left nodes without a screen, which only made screen verdicts UNCLEAR.
- status was 'failed' whenever any row was not MET, so UNCLEAR rows (parts NOT_MET, confirm refuted, whole-tree no) gave failed with an empty failed list. Now: passed (all MET), failed (a confirmed NOT_MET in failed), blocked (judge error), unclear (none violated, not all met), with a printed line naming compose done.
- fc_figmascope.split_scope sorts a task's criteria (two or more) by whose work they are, on the task's brief and the criterion alone, and skips one only at p(OTHER) >= 0.9; a failed, odd or all-OTHER sort judges everything. Skipped rows are kept in the result's skipped, never silently dropped.
- The judge says what it did not cover: screens_missing and a 'screens cover M of N nodes' line, whole_tree_skipped when a tree is over SETTLE_TREE_CHARS. Confirm and whole-tree evidence keep 1200 characters (was 300, cut mid-sentence).
## Tried and ruled out
- Treating UNCLEAR as passed: a design nobody could show met must still be read by the designer.
- Comparing the task's nodes: names to the tree: nodes: lists what the builder MAY touch, so a mismatch would be noise.
## Watch out
- The scope sort was not run live against Jev (no key in the sandbox); its 0.9 bar copies look_criteria's, whose scores for a named style were 0.6 to 0.7. Check the first real run.
- fc_models.py is at 598 of 600 lines.
