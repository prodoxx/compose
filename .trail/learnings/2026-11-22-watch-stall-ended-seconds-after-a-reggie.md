---
title: "watch stall=ended seconds after a launch (#486) and a design builder's ready report held as a ping (#491)"
author: Reggie
date: 2026-11-22
branch: worktree-agent-a0db614b9a6fb70f3
cost: { minutes: 60, tokens: 91451 }
touches:
  - bin/fc_fleet.py
  - bin/fc_watch.py
  - bin/fc_briefs.py
  - bin/fc_crew.py
---

## Decided
- #486 real cause: not a stale list. `crew start` writes the booting record with no `tab` until start_teammate returns (tab made, `herdr agent start` up to 60 s plus retries, so longer with parallel starts). herdr answers `tab_not_found` for `herdr tab get ""`, so fc_fleet.tab_gone(None) said the tab was gone and a booting teammate with no tab yet read as closed. #81 only covered a booting record that has a tab; #95 the cached list.
- The fix: a booting record with no tab is open while its `start_pid` (written by crew start, dropped by crew_up) is alive; a dead or missing pid closes it as before, so a killed start does not stay open forever. tab_gone(None) is False like the Herdr app and Rex backends. tab_closed moved from fc_crew to fc_fleet (fc_crew was at the 600-line cap).
- #491: one predicate, fc_briefs.ready_message (READY_REPORTS = test ready, design ready), names the `working` reports that wake a watcher alone; fc_watch.is_info uses it. Add a new kind of ready report there, not in is_info.
- Designer-started builders record reports_to: crew and have no entry in crew_owners; that changes nobody's wake: the lead and designer watches run with COMPOSE_ROLE (watch_scope returns nothing), and the manager's quiet `--from orchestrator` watch filters builder lines either way.

## Watch out
- Reproduce #486 with the real CLI: write results/crew/<n>.json with booting true, a space and no tab, plus a `working started` .status, then `compose watch --timeout 6 --interval 1` inside herdr; before the fix it prints stall=ended at once. A scratch repo needs git; the worktree itself works as the repo with a throwaway project (remove .compose after).
- Left open: the `closable` stall (idle, last report working) also nudges an idle design builder that reported design ready to "report done", which its brief forbids until the designer says so.
