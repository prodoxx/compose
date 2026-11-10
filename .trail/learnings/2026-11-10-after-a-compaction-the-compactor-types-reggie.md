---
title: After a compaction the compactor types the prompt that resumes the session
author: Reggie
date: 2026-11-10
branch: compact-resume
cost: { minutes: 80, tokens: 947241 }
touches:
  - bin/fc_compactor.py
  - bin/fc_hooks.py
  - docs/cli.md
  - docs/herdr.md
  - sections/0h.md
  - tests/test_compactor.py
  - tests/test_skillcheck.py
  - bin/fc_hc.py
  - bin/fc_host.py
  - bin/fc_rex.py
---

## Decided
- Claude Code starts no turn by itself after a typed `/compact`: the session sits at an empty prompt once the compaction and the file restores are done, and the SessionStart (compact) hook's re-entry note only rides along with the next prompt. So the compactor (bin/fc_compactor.py `resume`) types a resume prompt once the compact boundary landed: wait for the pane to be quiet (REQUIET_LIMIT), skip it when the product owner prompted since `/compact` went in (their prompt carries the note already), log the text as typed before it lands, and log the step (`resume`: sent, refused, owner-typing, not-quiet).
- The resume prompt tells the session to read its handoff, run the `compose status` and `compose watch --now` lines, act, and end the turn quietly: the manager says nothing to the product owner unless something waits on them. The Stop hook after that turn judges and parks the watches again, so the wake mechanism is re-armed whatever `/compact` did to the pending async Stop hook.
- fc_hooks.return_note gives no "open with what happened" note to a prompt compose typed itself (was_typed on the submitted prompt), so the resume and handoff prompts never make the manager report unprompted.

## Tried and ruled out
- Relying on the parked watch to wake the session after a compaction: the compact log on this Mac (15 landed compactions) shows the session idle for 1 to 25 minutes until a watch event or the owner's "What's going on?" / "What's the update?" / "updates?", and sometimes idle for good.
- Judging a status from the summary record: `is_owner_prompt` already drops isCompactSummary records, so the post-compaction summary never counts as the owner typing.

## Watch out
- The compaction record is no longer the compact log's last line: the resume step follows it. Tests that read `read()[-1]` for the compaction must look for kind == "compaction".
- The Typist fake in tests/test_compactor.py must key its handoff bookkeeping on "compose: before a compaction", since the resume prompt also starts with "compose:".
- `trail check` flags "prompt herdr" and "session compact" word matches in the new test (20 in all); none is a real conflict.
