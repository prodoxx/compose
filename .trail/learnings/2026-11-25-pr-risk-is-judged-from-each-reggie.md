---
title: "PR risk is judged from each layer's diff, as no-mistakes does"
author: Reggie
date: 2026-11-25
branch: pr-risk-assessment
cost: { minutes: 40, tokens: 1457 }
touches:
  - bin/fc_prrisk.py
  - "bin/fc_layergate.py#layer_pass"
  - "bin/fc_stack.py#layer_body"
  - sections/4.md
  - docs/verify.md
---

## Decided
- The product owner asked for PR risk the way no-mistakes does it, so the route-based `## Risk` section added earlier the same day is replaced. no-mistakes' reviewer returns risk_level (low, medium, high) and a one-sentence risk_rationale, and its PR carries `## Risk Assessment` with one line: a mark, the level, the sentence.
- compose now does the same per layer: when a verify passes and every layer passes its own checks, fc_prrisk.assess makes one review-model call per layer on that layer's own diff and keeps the answer in results/risk.json. `compose stack` only reads it, so writing PR text still calls no model.
- The three level definitions in the prompt are no-mistakes' own words (internal/pipeline/steps/review.go), so a level means the same in both tools' PRs.
- The model is told each task's route depth and reason (which names the verification policies on its files) and the confirmed review findings on the layer's files, so what the route-based section showed is now an input and not the answer.
- The level is never written by hand: a PR file with a Risk section is refused, as any unknown section is.

## Ruled out
- One assessment for the whole stack, shown on every PR: a stack's PRs differ, and a level that is true of the top layer misstates the groundwork.
- Asking the model at `compose stack` time: the stack runs many times and must stay free and deterministic.
- Keeping the team lead's own Risk bullets: no-mistakes takes none, and the sentence the model writes says what could break.

## Watch out
- An entry is keyed by the layer's PR file name and holds the tree it was judged at; a layer whose tree changed shows no section until the next passing verify. A verify with layer_checks false, review false, or parts left out judges nothing.
- A high level does not stop a submit. no-mistakes does not gate on it either; it says a person should approve.
- fc_prrisk sits just below fc_stack in tests/modules/rank.txt because it calls fc_models.
