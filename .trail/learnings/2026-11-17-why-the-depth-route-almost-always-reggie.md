---
title: Why the depth route almost always answers STANDARD
author: Reggie
date: 2026-11-17
branch: main
cost: { minutes: 19, tokens: 375079 }
touches:
  - docs/superpowers/specs/2026-11-17-look-pack-waiting-design.md
  - bin/fc_crew.py
  - bin/fc_clean.py
  - bin/fc_lead.py
---

## Decided
- Read all 176 recorded `## Route` sections on this machine (compose 53, emotion-apps 92, dtt-app 31) on 2026-11-17: depth STANDARD 128, DEEP 42, FAST 6.
- Jev itself chose DEEP 5 times and FAST 5 times in 103 answers above the bar; 34 of the 42 DEEP are the policy floor in the compose repo.
- The bar `route_threshold` 0.6 means the top choice needs p >= 0.73 with three choices (confidence = (p - 1/3) / (2/3) fits every record).
- Likely causes: STANDARD's wording describes a normal compose task (1-2 files, a protected test); DEEP's triggers (callers, more than three files, unclear how) are not in the state Jev sees; an unsure answer always becomes STANDARD and the record drops Jev's choice and probabilities.
- The look/pack/waiting design therefore does not hang anything on DEEP; fixing the depth route is its own spec, starting with recording the choice and probabilities on every route.

## Tried and ruled out
- "The retry's caller context pushes depth to unsure": replayed 5 tasks with the retry state, the answers did not move.
- "Depth is weak today": replayed 14 past fallback tasks in multi-harness, depth only; STANDARD was top on all 14 (DEEP at most 0.39). The 49 whole fallbacks in the records predate commit 183561bf8 (2026-10-24), which made each answer fall back on its own.

## Watch out
- `fc_routectx.dependency_context` finds no cross-file callers in this Python repo: `trail callers crew_dir` lists only bin/fc_crew.py while bin/fc_clean.py and bin/fc_lead.py call `fc_crew.crew_dir`. Not tested in TypeScript repos.
- A replay needs `fc_config.apply_config(move=False)` first, or jev_ask finds no TypeSafe key.
- Replaying today is not the original ask: the task's files exist now, so `exists` flags and line counts differ.
