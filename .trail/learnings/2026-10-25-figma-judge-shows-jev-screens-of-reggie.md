---
title: Figma judge shows Jev screens of the design (Jev 1.13+ images)
author: Reggie
date: 2026-10-25
branch: main
cost: { minutes: 19, tokens: 299293 }
touches:
  - bin/fc_figma.py
  - bin/fc_figmanodes.py
  - bin/fc_models.py
  - docs/cli.md
  - docs/how-it-works.md
  - docs/requirements.md
  - docs/tasks.md
  - jev/pyproject.toml
  - jev/uv.lock
  - sections/0d.md
---

## Decided
- `compose figma tree` also saves screens: Figma REST renders (scale 2, then 1 for nodes too big) cut by jev/jev_ultrafast/tiles.py (Pillow, run with `uv run --project jev`) into PNG tiles of 280-1568 px a side, at most 6 per task and 4 per tall frame, in results/NNN.screens/ with screens.json.
- fc_models.Image in any Jev state becomes an `assets` entry plus its reference, sent with the `TypeSafe-Danger-Zone-Images: true` header (docs.typesafe.ai/images.md).
- The screens are a separate Jev request folded in by fc_figmashots.apply_screens, never put into the tree chunks: a text-only confirm cannot see screens, and mixing them broke the issue-87 chunk logic.
- A screen violation counts only when Jev gives it twice; a screen MET only settles a tree UNCLEAR; screens never lift a confirmed tree violation; a screen error blocks.
## Tried and ruled out
- macOS sips for tiling: --cropOffset 0 silently centers the crop, and a crop reaching the bottom edge exactly is ignored.
- Telling Jev in the rules that tokens, styles and component keys are UNCLEAR on screens: live, it still called a token criterion NOT_MET twice (p 0.6-0.7) and a style one MET. Fix: look_criteria sorts criteria on their text alone first (12/12 right live), and only LOOK at p >= 0.9 go to the screens; a named style scores 0.6-0.7.
## Watch out
- A clipped label scored p_not_met 0.56-0.66 live, right at the 0.6 threshold, so subtle visual defects can pass; that fails safe (tree verdict stands).
- The Figma render path was not run live (no project with a Figma file at the time).
