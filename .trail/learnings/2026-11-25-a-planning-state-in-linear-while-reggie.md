---
title: A Planning state in Linear while a plan is being written
author: Reggie
date: 2026-11-25
branch: linear-planning-status
cost: { minutes: 30, tokens: 247412 }
touches:
  - bin/fc_planstate.py
  - bin/fc_tasks.py
  - "bin/fc_briefs.py#cmd_planner"
  - docs/states.md
---

## Decided
- The product owner asked for a Planning status in Linear, like the one on Jobscan's own tickets, set while an issue is being planned.
- `compose planner` moves the issue it plans (the feature, or the story named with --issue) from Todo or Backlog to Planning. An issue in any other state stays where it is. Task issues are never in Planning: the planner creates them in Todo.
- A team with no Planning state gets one made by compose (workflowStateCreate): type unstarted, placed between the last unstarted state and the first started one. It was made for real in the Engineering team this way, at position 1.5.
- Planning is as open as Todo. fc_tasks.OPEN is the one list of open state names, read by readiness, the factory, ready_ids, start_today and the stale check, so a story a lead is planning is treated exactly as it was when it sat in Todo.
- The issue leaves Planning by the existing roll-up: In Progress once a task of it is dispatched.

## Ruled out
- Moving a planned story back to Todo when readiness finds it specced: a Todo story is ready with no tasks at all, and the factory hands it to a lead who then plans it, so planning ends where building starts.
- Type started for the state: Linear would count the issue as started, and compose's roll-up reads a started child as work in progress.

## Watch out
- The move is said on stderr and never raised: the planner's brief owns stdout, and a brief must not wait on a status.
- fc_linear.py sits near the 600 line cap, which is why this lives in fc_planstate.py.
- No Jobscan team was found in the Linear workspace (teams: Engineering, Distribution), so the state's place and type are compose's call, not a copy.
