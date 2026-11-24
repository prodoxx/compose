---
title: "Linear type labels: the Type label group and standalone labels both work (#494)"
author: Reggie
date: 2026-11-24
branch: linear-type-label-group
touches:
  - SKILL.md
  - bin/fc_linear_cmd.py
  - bin/fc_reconcile.py
  - docs/cli.md
  - tests/modules/rank.txt
  - tests/test_linear_cmd.py
  - bin/fc_labels.py
  - tests/test_modules.py
---

## Decided
- All label resolution lives in `bin/fc_labels.py`; `fc_linear_cmd` (create, update, list, labels) and `fc_reconcile.Team.label` call it. A bare name for a write resolves: the `Type` group's child, else the standalone label, else a child of another group (the old first-match lookup, kept so nothing that worked breaks). `"Type > Story"` is exactly that child.
- Readers of an issue's type needed no change: Linear returns a group child by its own name (`Story`, not `Type > Story`), so every `name.lower() == "story"` check and the `eqIgnoreCase: "feature"` filters already accept both shapes.
- In the optional `types` map in `.compose/linear.json`, a bare value means the standalone label exactly. That is the way out for a team that wants the loose label on purpose, because a bare `--label Story` would pick the group child. A null or missing entry resolves by the order above.
- Single-select (`--add-label` only): adding a child of a group drops the issue's other child of that group, unless the group's `groupType` is `multiSelect`. On a team that has a `Type` group, adding any type label also drops the issue's other type labels in either shape. A team with no groups sees no change. A bare `--drop-label` drops every label on the issue with that name, in either shape. `--label` writes the list as given.
- `labels --create` refuses a name the team already has, including the `Type` child of that name. `--create "Type > X"` passes `parentId`.

## Tried and ruled out
- Making the readers understand the types map (so that `"task": "Type > Tech Task"` issues read as tasks): it needs the repo's map at about eight label-reading sites, plus the GraphQL name filters in fc_delivery. Left out. A mapped label whose name is not the type's name is written correctly but is not read as that type.

## Watch out
- Linear does NOT ignore dropping a label the issue lacks: `removedLabelIds` with one refuses the whole update with "Label not on issue" (seen live on 2026-11-24; main had the same flaw, and its old docstring said otherwise). So drops are filtered to the issue's own labels, and a drop of nothing prints `unchanged`.
- Tested live on jobscanapp's Autofill project (team Engineering, NEB), which has both `Type > Story` and standalone Story/Task/Epic, with no `Type > Epic` or `Type > Task`.
- `team.labels` includes workspace labels (team null), and a group can be one of them. Linear's IssueLabel has `isGroup`, `groupType` (singleSelect|multiSelect) and `parent`. A group itself cannot be applied to an issue.
- `fc_linear_cmd.py` is close to the 600-line module cap in tests/test_modules.py, so new label logic goes in fc_labels.
