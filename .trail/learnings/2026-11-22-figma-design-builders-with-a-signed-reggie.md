---
title: "Figma design builders with a signed-out plugin server: check before start, fall back to the claude.ai connector (#493)"
author: Reggie
date: 2026-11-22
branch: worktree-agent-a3dce471b9ea18a1b
cost: { minutes: 60, tokens: 115403 }
touches:
  - bin/fc_claude.py
  - bin/fc_crew.py
  - bin/fc_figma.py
  - bin/fc_mcp.py
  - docs/config.md
  - docs/how-it-works.md
  - docs/requirements.md
  - tests/modules/rank.txt
  - tests/test_crew.py
  - tests/test_groundwork.py
---

## Decided
- `claude mcp list` prints `plugin:figma:figma: ... - ! Needs authentication` when the plugin server lost its stored token; a session then gets only `authenticate` and no `use_figma`. Machine-checked on 2.1.330: that is the live state here, so the bug reproduces from the real CLI.
- `claude --settings <json> mcp list` works (global option before the subcommand; `claude mcp list --settings` is not an option, which is why the issue's attempt printed nothing). `{"deniedMcpServers":[{"serverName":"plugin:figma:figma"}]}` removes only that server from the list and leaves the plugin enabled, so its skills stay. `{"serverName":"figma"}` does not match it.
- fc_figmaauth.check (design-NNN and designer only; design-review and the figma helper read through Framelink): list once; on Needs authentication list again with the plugin server denied; a Connected `claude.ai Figma` there means start with deniedMcpServers (fallback), none means refuse with the sign-in message. A pass is cached 10 minutes under `figma-signin` in model-probe.json; a refusal is never cached, and a list that fails or prints nothing never blocks.
- designcrews asks once before its parallel starts (one refusal, no builder started) instead of one per builder.

## Tried and ruled out
- Giving design roles the connector unconditionally (denying the plugin server always): not verifiable on this machine, whose account has no claude.ai Figma connector at all (it is absent from the list even with the plugin disabled), and it would cost the plugin when it works. So the deny is applied only when the second list shows the connector Connected.
- Refusing on a plugin server that Failed to connect (HTTP 5xx): transient, not a lost sign-in.

## Watch out
- Not verified here: that Claude Code loads the claude.ai connector in a real session once the plugin server is denied (the list says it would). The refusal path is verified against the real CLI.
- fc_crew.py is at the 600-line limit: the check rides the existing trust-check line through fc_claude.start_refusal.
- Why the token vanished, and why /mcp inside a running session does not write the one new sessions read, is still unknown.
