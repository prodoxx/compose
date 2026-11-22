---
title: Linear Duplicate state read as canceled by type
author: Reggie
date: 2026-11-22
branch: worktree-agent-aa5179401c9529cf6
cost: { minutes: 4, tokens: 87945 }
touches:
  - "bin/fc_tasks.py#state_name"
  - bin/fc_linear.py
  - bin/fc_delivery.py
---

decided: compose reads a Linear state by its type, not only its name. fc_tasks.state_name(state) returns Canceled for every state of type canceled (Duplicate, Won't Do, a team's own name), and every read of a Linear state that feeds CANCELED comparisons (sync payload, blockers, delivery nodes, ready_ids/freed/task_holds, decide's children) goes through it. The GraphQL fields LINEAR_TREE_FIELDS, READY_NODE and READY_ROOT therefore ask for state { name type }. ruled out: adding "duplicate" to fc_tasks.CANCELED, because linear_cancel picks its target state as type canceled AND name in CANCELED, and a cancel must never land in Duplicate (that spelling is Linear's for a merge); it would also miss Won't Do. watch out: a node read without a type keeps its name, so a new Linear query that feeds these readers must select type; compose sync --stdin has no type, so its JSON must say Canceled for such an issue.
