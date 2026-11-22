---
title: "Acceptance judge: own change per task, rows that settle, dismissable ids"
author: Reggie
date: 2026-11-22
branch: worktree-agent-a389fbe49dc6bb9ee
cost: { minutes: 60, tokens: 107477 }
touches:
  - bin/fc_accept.py
  - bin/fc_review.py
  - bin/fc_taskreview.py
  - bin/fc_verify.py
---

## Decided
- A task is judged on its own change: the diff of its branch against the layer below it (fc_accept.own_changes), not its hunks of the cumulative base...HEAD diff, which carry every later task's edits to the same file (#492 task 288). The stack's branches are listed from cfg main_repo: from the stack worktree HEAD is the top, so every branch is merged and fc_worktree.task_branches (--no-merged=HEAD) lists none.
- The judge gets the task's files whole, after the change, only when a criterion names in backticks something the change text does not hold (fc_accept.needs_files). A grep criterion about an unchanged line (#492 task 264) and a criterion about unchanged lines (#482) are then judged on the file. Built output no source file holds stays UNCLEAR; only a test proof or a dismissal clears it.
- Every row that is not MET gets an id, finding_id of file 'acceptance of task <id>' and the criterion text, so the existing review --dismiss (dismissed.json, ledger event) reaches NOT MET and UNCLEAR rows. A reworded criterion is a new id, on purpose.
- A kept verdict is reused only when every row is MET, proven by a passing test event, or dismissed. A kept row that is none of these gets proven_by_test again (no model call), else the task is rejudged. Before, a kept UNCLEAR was reused for ever on unchanged code.

## Ruled out
- Making a criteria rewrite keep its old test proof: fc_ledger.counted drops events whose criterion ids left the task and fc_reroute.ledger_triggers reads that as 'voided' evidence on purpose (it raises the depth). The proof comes back when the task's test runs again; verify now says so in the row's note instead.
- A deterministic grep for a grep-shaped criterion: parsing criterion prose is fragile; showing the whole file and letting the judge quote the line covers it.

## Watch out
- tests are not allowed to run git (tests/hermetic.py): own_changes is the one function that asks git for a task's change, and tests patch it. To see it on real git, build a scratch repo with a stack worktree and pass cfg main_repo.
- bin/fc_verify.py is at 579 of 600 lines: acceptance printing lives in fc_accept.report_lines.
