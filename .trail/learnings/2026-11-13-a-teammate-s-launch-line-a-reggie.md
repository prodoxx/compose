---
title: "A teammate's launch line, a blocked agent's pane, and herdr 0.9.5's start errors"
author: Reggie
date: 2026-11-13
branch: worktree-agent-a47bc129ab67737d9
cost: { minutes: 150, tokens: 53265 }
touches:
  - bin/fc_herdr.py
  - bin/fc_claude.py
  - bin/fc_fleet.py
---

## Decided
- A Claude teammate's typed launch command stays short: --settings (with the --disallowedTools patterns as permissions.deny) is a file, results/crew/<name>.settings, not inline JSON. herdr types the command into a new pane's shell, and until zsh starts its line editor the tty keeps 1024 bytes of a line. herdr 0.9.3 dropped the rest silently; 0.9.5 answers agent_start_input_failed, which herdr_start_crew now retries like agent_pane_busy.
- The settings file has no .json suffix: crew list, clean, watch and others read every *.json in results/crew as a teammate record.
- permissions.deny in --settings removes the same MCP tools from a session as the --disallowedTools flag (checked with the stream-json init message's tool list).
- herdr agent read --source recent-unwrapped on a blocked fullscreen Claude fails with agent_not_idle; herdr_text retries it as --source visible, so every reader of a blocked pane gets the dialog.

## Tried and ruled out
- Waiting for the shell prompt on the screen before agent start: a prompt painted by an instant-prompt theme is drawn before zshrc finishes, with the tty still canonical.
- Using herdr(check=True) for every non-wait prompt in fc_fleet.prompt to get the error code: tests across the suite stand in for herdr with lambdas that take no check keyword; a refused prompt asks the agent's status instead (blocked means agent_blocked).

## Watch out
- To reproduce the 1024-byte race on 0.9.5, a scratch HOME whose .zshrc waits in a zsh builtin (zmodload zsh/zselect; zselect -t 600) makes herdr see an available shell while the tty is still canonical; sleep in zshrc is an external process and gets agent_pane_busy instead.
- herdr 0.9.5 unregisters an agent as soon as its process exits, so the listed-with-an-exited-process state of the designcrews restart (#465) cannot be made on it; crew stop on a record with no tab is the closest live repro.
- This Mac's python3 is 3.14 on PATH; /usr/bin/python3 is the 3.9.6 one. On 3.9, test_harness, test_figma, test_doctor and test_testrun fail on tomllib and a tempfile quirk, unrelated to compose.
