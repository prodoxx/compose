---
title: "A failed Figma node fetch names its file, and a story's figma: is not inherited"
author: Reggie
date: 2026-11-22
branch: worktree-agent-a61297d16cebd30fc
cost: { minutes: 9, tokens: 98523 }
touches:
  - bin/fc_framelink.py
  - bin/fc_figma.py
  - bin/fc_run.py
  - docs/tasks.md
---

## Decided
- framelink_fetch drops Framelink's [INFO] and [DEBUG] log lines from stderr, hides each secret, then keeps the START of what is left (2000 characters), never the last 500. The old tail started inside `[INFO] Using Personal Access Token for authentication` and read as an auth failure.
- The stop names the node and the file key. code_nodes adds where the key came from (the task's figma: line, or the project's stored file) through fc_framelink.node_file, which run's start lines (stored_file_notes) use too.
- 'Figma refused the token (authentication)' is printed only for 401, 403 or an invalid token; Framelink's own 'Node ... was not found' passes through and means Figma answered 404 for that node in that file.
- A story's or feature's figma: is NOT inherited by its tasks: task_from_issue reads only the task's own compose block, and an inherited key would be a second hidden source, the very thing that caused the wrong-file fetch. Written in docs/tasks.md.
## Tried and ruled out
- Closed #483 as a duplicate of #484 (NOT_PLANNED): same two defects, #484 is the one kept.
## Watch out
- A test helper named fail() on a TestCase shadows unittest's self.fail, so every assertNotIn that failed passed silently.
