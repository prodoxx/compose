---
title: Testing compose end to end in Rex
author: Reggie
date: 2026-11-12
branch: main
cost: { minutes: 1, tokens: 1557 }
touches:
  - bin/fc_crew.py
  - bin/fc_fleet.py
  - bin/fc_rex.py
  - docs/hosts.md
  - tests/test_rex.py
---

## Decided
- Every `rex` call runs without REX_SESSION and REX_BLOCK (fc_rex.cli_env). With REX_SESSION set, Rex scopes `-b BLOCK`, `capture`, `send`, `block rename|inspect|close|call` and `window close` to that session, so from the manager's terminal every block in the project's session was "not found": teammates never got their label, `crew start` failed with agent_not_found, and its cleanup left an orphan tab.
- The project's Rex session id is kept in `.compose/<project>/results/rex.json`; find_space looks it up first and renames the session back when its label drifted. Rex's own SessionStart hook (`rex __internal session-start`, session naming on) tells every Claude Code session to `rex session rename "$REX_SESSION"` after its task, so a teammate renamed the shared project session to `rextest-architect` and compose lost it.
- Teammates in Rex get fc_rex.TEAMMATE_RULE appended to their system prompt (fc_fleet.teammate_rule): never rename the Rex session. Verified live: a Sonnet teammate started after the rule left the label alone.
- fc_fleet.start_implementer places panes under a lock (PLACING): `run` starts tasks on threads, and each one made its own `implementers` window.
- `crew focus` says when Rex.app did not switch: `rex do session.select` needs Remote Control on in the app (the person's setting, never ours to flip).
- The project's `shell` tab runs `env DISABLE_AUTO_UPDATE=true DISABLE_UPDATE_PROMPT=true $SHELL -l`: a bare `rex new` sat at oh-my-zsh's update question.

## Tried and ruled out
- Passing `-s <session>` on every block call: the backend would need the session for each block id everywhere; dropping REX_SESSION from the env lets a block id reach its block in any session, as the docs promise.

## Watch out
- The 2026-11-07 learning's "a block id reaches any command without -s" holds only when REX_SESSION is unset; a compose CLI started inside Rex always has it.
- pi 1.3 in `--tui-mode regular` reports `working`, then Rex clears it at once: pi writes OSC 133 prompt marks to the primary screen and Rex ends a working record at the next shell prompt. Fullscreen pi keeps `working`. compose follows implementer turns through the pi session file, so runs still pass; only the spinner is missing.
- Claude Code restores an interrupted prompt into its input box after Esc; the next `crew prompt` is pasted after it (`...then say done.Say only: pong`). Not Rex-specific.
- To E2E without touching Linear, use a scratch COMPOSE_CONFIG whose auth.json has no `linear` keys; `compose sync --stdin` then feeds tasks. A scratch Python repo needs `__pycache__/` ignored or every attempt fails on outside files.
