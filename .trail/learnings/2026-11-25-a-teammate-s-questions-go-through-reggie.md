---
title: "A teammate's questions go through superpowers brainstorming, not a paraphrase"
author: Reggie
date: 2026-11-25
branch: main
cost: { minutes: 8, tokens: 112914 }
touches:
  - sections/0h.md
  - "bin/fc_decide.py#ASK_STYLE"
  - tests/test_shaping.py
---

## Decided
- The product owner saw a planner's question (NEB-231 Q9) asked bare, with no choices and no recommendation until they asked for it twice, and no superpowers:brainstorming call during planning.
- Cause: only shaping (0b) invoked the skill. 0h's relay of planner, designer and change architect questions, and the line `compose decisions` prints (fc_decide.ASK_STYLE), were a paraphrase that held back the recommendation "until they are stuck" and banned showing options. The skill says the opposite once the why is known: sketch the choices with their trade-offs and your pick, and a proposal they can correct beats a bare question.
- 0h and ASK_STYLE now say: invoke superpowers:brainstorming and follow The Conversation as written; the feature was shaped, so each question's recommendation and why are the proposal and reason put to the product owner.
- The question line format (`- Q1: <question> - Recommendation: ... - <why>`, no lettered options in the question) is storage and stays.

## Watch out
- tests/test_shaping.py pins that 0h invokes the skill and that the held-back rules do not come back.
