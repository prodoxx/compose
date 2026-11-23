---
title: Shaping follows superpowers brainstorming again
author: Reggie
date: 2026-11-23
branch: main
cost: { minutes: 1, tokens: 1140 }
touches:
  - sections/0b.md
  - SKILL.md
  - sections/0h.md
  - commands/planner.md
  - bin/fc_layout.py
---

## Decided
- The product owner wants compose's shaping to be superpowers:brainstorming. 0b now says so and follows the skill: "First: what do you actually know?" decides between the one-pass Intent and the conversation, so a feature whose request leaves something non-trivial open is shaped by default (this reverses the 2026-11-02 lean-defaults choice of "shape only when asked"; the needs-shaping label, a policy match and an irreversible guess still force it, and "just go" skips it).
- Restored from the skill: the ~200-word first message that echoes the request with what the code shows plus the "or I'll just start" line, recon without asking leave, approaches in a message of their own (kept under a new `## Approaches` section, a planning-log section in fc_layout.WORKING), the spike, playback with text sketches, and the red flags.
- The compose planner is the writing-plans step, not brainstorming; its batched "Q1: ... Recommendation" questions are planning gaps the engineering manager asks brainstorming style (0h), not the shaping conversation.

## Watch out
- When superpowers:brainstorming changes, 0b follows it; tests/test_shaping.py pins the moves that make it a conversation.
