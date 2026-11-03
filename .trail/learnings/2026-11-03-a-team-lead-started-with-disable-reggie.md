---
title: "A team lead started with --disable-slash-commands cannot /compact: Claude Code hides the built-in commands too"
author: Reggie
date: 2026-11-03
branch: main
cost: { minutes: 19, tokens: 225621 }
touches:
  - bin/fc_crew.py
  - bin/fc_mcp.py
  - docs/herdr.md
  - tests/test_crew.py
  - bin/fc_compact.py
  - bin/fc_hooks.py
  - bin/fc_watch.py
  - docs/cli.md
  - sections/0h.md
  - tests/test_compact.py
---

## Decided
- `claude --disable-slash-commands` (passed to every non-design Claude teammate since cd33d4717) hides `/compact` as well as skills: an interactive session answers `Unknown command: /compact` and reads the compactor's focus text as a prompt ("Args from unknown skill: The handoff at ..."); `claude -p` says "/compact isn't available in this environment". Same on 2.1.316 and 2.1.317.
- The team lead now keeps its commands (fc_mcp.keeps_commands: design roles and `orchestrator`); every other Claude teammate still starts without them, since those compact through the 150k auto window and never get `/compact` typed in.

## Tried and ruled out
- Blaming a Claude Code version change: the lead on this machine that did compact manually (4 of its 19 boundaries) had been restored after a restart as `claude --resume <id>`, without any of compose's start flags; the other 15 were Claude's own auto compactions at ~167k with no handoff written first.

## Watch out
- herdr's restore relaunches a teammate as bare `claude --resume <id>`: the restored lead runs without --settings, --disallowedTools or the flag, so it behaves differently from a fresh one.
- A lead started before this change keeps the flag until it is restarted (compose crew restart --role orchestrator); `compose update` alone does not change a live process.
