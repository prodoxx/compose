---
title: "The fleet in the Herdr app and Rex: one fleet module, a backend per terminal host"
author: Reggie
date: 2026-11-07
branch: worktree-hosts
cost: { minutes: 59, tokens: 698535 }
touches:
  - README.md
  - SKILL.md
  - bin/compose.py
  - bin/fc_briefs.py
  - bin/fc_change.py
  - bin/fc_clean.py
  - bin/fc_compactor.py
  - bin/fc_core.py
  - bin/fc_crew.py
  - bin/fc_decide.py
---

## Decided
- compose speaks to its terminal host through one module, fc_fleet (the verbs: agents, agent, prompt, read, wait, space, start_teammate, start_implementer, pane_label, close_*, name_manager, manager_claim, attach). fc_herdr keeps the herdr CLI calls, fc_hc the Herdr app (hc + herdr-app) and fc_rex Rex. Callers never run a host's CLI themselves; pure helpers (agent_name, agent_id, project_worktree, tab_label, PI_HERDR_EXT, STAGE_OBSERVERS) stay in fc_herdr.
- fc_host detects the host from the variables each sets in its terminals (HERDR_ENV+HERDR_SOCKET_PATH; HC_TERMINAL_ID+HC_TERMINAL_SOCKET; REX_SESSION+REX_BLOCK), herdr first; COMPOSE_HOST forces one, COMPOSE_NO_HERDR makes all headless. fc_core.session_id reads the three variables directly (no import of fc_host: every module may ask).
- The herdr path is always fc_herdr's own calls, even outside herdr: 112 tests stand in for `fc_herdr.herdr`, and herdr() answers None headless by itself. fc_fleet routes to a backend only for hc and rex; a test that mocks fc_herdr.herdr_env True wins over fc_host.
- The agent record is herdr's shape everywhere: {name, pane_id, workspace_id, tab_id, agent_status}; the backends map into it (hc: terminal address, app space/tab, OSC status; rex: block id, session, window, program_status).
- In the Herdr app an agent is a named hc terminal (`hc run --bg --no-parent --no-notify --name <agent> --label herdr.space=.. --label herdr.tab=..`), its tab found from `herdr-app ls` after placement and renamed; env goes as `env K=V ... prog` (hc/rex pass no env). In Rex an agent is a block whose label is `<name> · <title> · <state>`; the name is the identity.
- `compose crew prompt|read|keys` are the host-neutral commands the sections now spell out instead of `herdr agent prompt/read/send-keys`; `compose stages` is the one verify pane in the app and Rex (the hold file now carries `stage_states`), reporting OSC 7501 itself.
- Bare `compose` opens the first host installed: the Herdr app (hc + herdr-app on PATH), else herdr, else Rex (rex on PATH or REX_BIN_DIR); app bundles are never scanned, so the suite's controlled PATH decides.

## Tried and ruled out
- Scanning /Applications for Rex or Herdr bundles in fc_host.installed(): BareCompose's test expects exit 1 on a PATH without herdr, and the machine state would leak into it.
- One `prompt` path for herdr: crew tests mock `herdr` (check=True raises the code), the stalled-turn tests mock `herdr_error` for the resend; fc_fleet keeps both (check → herdr(check=True), the resend in stalled() → herdr_error).
- Appending `--json` at the end of an hc or rex command: after `--` it reaches the program (`cat: illegal option`); hc takes it right after the subcommand, rex before `--`, and `rex block call` takes none.

## Watch out
- `herdr-app` prints pretty multi-line JSON; hc one line. Parse the whole stdout first, then the last line.
- `hc send --wait` nests the wait's outcome under `wait` (`{"wait": {"reason": "timeout"}}`); `hc wait` answers `reason` at the top.
- `rex send` pastes (bracketed) without submitting: `rex send-key enter` follows. `hc send` submits by itself. pi 1.3 reports OSC 7501 natively; herdr's pi extension (herdr-agent-state.ts) talks only to herdr's socket.
- `rex block ls --json` needs `-s <session>`; list sessions first. A block id reaches any command without `-s`.
- Tests that fake a clock must patch `fc_fleet.time` too now (await_start, stalled, ready live there).
- Closing the probe space in the Herdr app leaves it under `recent` in the window; harmless.
