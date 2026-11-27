---
title: "A workspace's own names for Linear states (linear_states)"
author: Reggie
date: 2026-11-27
branch: main
cost: { minutes: 1, tokens: 2384 }
id: l-56a2f0db
scope: repo
status: active
src: agent
touches:
  - tests/test_states.py
---

## Decided
- State names are set per workspace, at the product owner's word: one compose install serves workspaces whose teams name states differently (Jobscan has Ready for Dev, Dev In Progress, PR In Review, Staged).
- A workspace's `linear_states` in ~/.compose/config.json maps compose's state (Backlog, Todo, Planning, In Progress, In Review, Done) to the team's name, or a list of names with the one compose writes first.
- The translation is at one place: fc_linearapi.linear_gql passes every answer through fc_states.read, which renames each `state { name }` and each team's `states { nodes }` to compose's names and keeps the team's under `team_name`. The rest of the CLI keeps comparing "In Review", "Done" and so on.
- A renamed state also takes compose's type for it, so Staged (a started state at Jobscan) mapped to Done reads as completed.
- In a team's states list, the state compose writes is the only one left under compose's name: with Done -> Staged, the team's real Done node is left out of the list, so every lookup by name finds Staged. An issue already in the real Done still reads Done and is not moved.
- For Jobscan, Done maps to Staged: merged work lands on staging there, and the team's Done is the release, which a person sets. That mapping is the agent's call, not the owner's word.

## Tried and ruled out
- Built-in synonyms for the Jobscan names: the owner wants it per workspace, and one Linear key can see teams with different names (the jobscan key sees JOB and NEB).
- Translating at each read site: over twenty places read `state.name` directly, and a new one would be missed.

## Watch out
- Tests that replace fc_linear.linear_gql skip the translation; tests/test_states.py fakes fc_linearapi._linear_post instead.
- A `sync --stdin` payload is canonicalised in fc_tasks.sync_tasks, since it does not come through linear_gql.
- Jobscan's "Design in progress" (unstarted) is in no entry: it keeps its own name, so compose does not treat it as open like Todo.
- fc_linear.py and fc_linear_cmd.py sit at 589 and 592 of 600 lines.
