---
title: "Question ids run per map: decide matches a question by its text, not its id"
author: Reggie
date: 2026-11-24
branch: main
cost: { minutes: 1, tokens: 1059 }
touches:
  - "bin/fc_decide.py#clear_elsewhere"
  - "bin/fc_decide.py#question_key"
  - "bin/fc_waiting.py#settle_asks"
---

Every map numbers its own open questions from Q1, so the same id on two issues of a feature is usually two different questions (#495).
`clear_elsewhere` used to drop every open question whose bare id was closed, across the feature and all its live stories, which wiped unanswered questions and closed their waiting-list asks.
It now takes {id: question text} and clears another map's question only when `question_key` (no markdown marks, case, spacing or end punctuation) matches: that keeps #419's case, a story copying the feature's question, working.
`settle_asks` had the same assumption: it treated the feature as settled for every id the decide closed, even when the decide never wrote the feature.
Now `settled_on` is only the decided map and the maps `clear_elsewhere` actually wrote, and an id settled on the ask's own map leaves that ask whatever the feature lists under the same id.
Ruled out: tracking questions by a global id; that would change the planner's numbering and every existing map.
