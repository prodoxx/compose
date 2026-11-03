---
title: "The engineering manager compacted mid-conversation: the compaction judge was a pass-through for it"
author: Reggie
date: 2026-11-03
branch: main
cost: { minutes: 24, tokens: 177788 }
touches:
  - bin/fc_compact.py
  - bin/fc_hooks.py
  - bin/fc_watch.py
  - docs/cli.md
  - docs/herdr.md
  - sections/0h.md
  - tests/test_compact.py
  - tests/test_skillcheck.py
  - bin/fc_accept.py
  - bin/fc_models.py
---

## Decided
- The compact judge (bin/fc_compact.py, compact-adviser's policy) asks whether the latest unit of work is finished; waiting on a person counts as finished. Every engineering manager turn ends handed to the product owner, so the manager scored 0.92-0.99 against floors of 0.5-0.7 on all 20 asks in 3 days and compacted at the first quiet moment past the 40k minimum (62k-125k tokens, four times in 13 hours on one pane, 4 of 14 runs disarmed because the owner was typing).
- Fix: a per-role `start` usage in PROFILES; the manager is not judged below 65% of compact.budget (verdict `below-start`), the lead keeps 0.0. The lead has the opposite problem: it never qualified in the log and only compacted at the ceiling.
- The product owner's first prompt after 30 minutes of silence, or after a compaction since their previous prompt, gets a UserPromptSubmit note (fc_hooks.return_note) telling the manager to open with what happened and what waits on them. Silence is measured from owner prompts in the transcript, minus what compose typed into the pane (fc_compactlog.was_typed).

## Tried and ruled out
- A global compact.minimum raise: it would push the team lead, which already only compacts at the ceiling, further into ceiling compactions.
- Restarting the manager session instead of compacting: the manager's context is the product owner conversation and there is no in-place swap of an interactive claude process; a fresh-session restart fits the team lead (compose crew restart --role orchestrator exists) at a judged moment, not the manager.

## Watch out
- fc_hooks ranks before fc_manager in tests/modules/rank.txt, so the hook cannot ask fc_manager.is_manager; "a session without a role that loaded the skill" (SKILL_SEEN) is the manager test there.
- At UserPromptSubmit the transcript may or may not already hold the prompt being submitted; return_note drops the last owner prompt when its text equals the submitted one.
- The ~/.compose/compact/log.jsonl records (turn, verdict, step, compaction) are the evidence for any judge tuning; `compose compactions` summarizes a week.
