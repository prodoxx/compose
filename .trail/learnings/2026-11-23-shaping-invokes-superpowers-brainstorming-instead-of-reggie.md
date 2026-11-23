---
title: Shaping invokes superpowers brainstorming instead of paraphrasing it
author: Reggie
date: 2026-11-23
branch: main
cost: { minutes: 13, tokens: 53561 }
touches:
  - sections/0b.md
  - bin/fc_steps.py
  - tests/test_shaping.py
---

## Decided
- The product owner said twice that shaping must be like superpowers:brainstorming. Two paraphrase rounds (2026-11-22 and -23) still drifted: each added compose's own rules on top. 0b now invokes the skill itself and follows it as written, and lists only what compose changes: who holds the conversation, the one-pass Intent as the skill's quick task, Linear decisions as context, compose decide, the design on the Linear issue, compose buildercheck as its subagent, Frame0/Lavish as its visual companion, spikes on a throwaway branch, and the planner instead of writing-plans.
- superpowers is a required setup row (installed with `claude plugin install superpowers@claude-plugins-official`), Claude-only like the herdr skill.

## Watch out
- Never paraphrase the skill back into 0b: tests/test_shaping.py fails on the old paraphrase headings.
