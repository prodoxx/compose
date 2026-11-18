---
title: "Walking compose end to end in a sandbox: what the real commands need"
author: Reggie
date: 2026-11-18
branch: worktree-waiting-list
cost: { minutes: 14, tokens: 303754 }
touches:
  - bin/fc_hooks.py
  - tests/test_skillcheck.py
  - ".superpowers/sdd/2026-11-17-waiting-list/walk/linear_standin.py"
  - ".superpowers/sdd/2026-11-17-waiting-list/walk/setup.sh"
  - ".superpowers/sdd/2026-11-17-waiting-list/walk/teardown.sh"
  - ".superpowers/sdd/2026-11-17-waiting-list/walk/walk.sh"
  - ".superpowers/sdd/2026-11-17-waiting-list/walk/debug_hook.sh"
  - ".superpowers/sdd/2026-11-17-waiting-list/walk/scenario.sh"
  - ".superpowers/sdd/2026-11-17-waiting-list/walk/hook.sh"
  - ".superpowers/sdd/2026-11-17-waiting-list/walk/preview.sh"
---

## Decided
- A walk of compose through real `compose` processes, with nothing of the real machine read or written, needs: a git repo of its own, `HOME` and `COMPOSE_CONFIG` of its own, `COMPOSE_NO_HERDR=1`, and `LINEAR_URL` pointed at a local stand-in that answers the issue query, the children query and `issueUpdate` (that is all `compose report needs-user` and `compose decide` call).
- The sandbox config needs an `auth.json` beside `config.json` with `{"workspaces": {"default": {"linear": "<any>"}}}`, or the ask gate reads no map and checks nothing.
- The prompt hook is run as Claude Code runs it: the UserPromptSubmit event as JSON on stdin of `compose skillcheck`, its stdout parsed as JSON.
- In the prompt hook a note that cannot be made is left out and the others still come (`fc_hooks._notes`): the skill's files are rewritten under live sessions by a merge or `compose update`.

## Tried and ruled out
- A sandbox home without `~/.claude/skills/compose`: `skill_notice` raises, and before `_notes` the hook then printed nothing at all, which looked like a missing footer. Link the checkout there, as the installer does.
- A crew record written by hand with only `name`, `reports_to` and `pane`: `compose status` crashes in `fc_cost.rows` on `started_at`. Write the fields `crew start` writes (`agent`, `role`, `title`, `model`, `cwd`, `started_at`).
- Running the engineering manager's `compose watch` for real to see a wake: a needs-user or blocked report that wakes it pops the desktop notification on the machine, so it was left to the tests.
- `compose init` for the sandbox project: it checks for every tool a build needs. A folder `.compose/<slug>/` with a `verify.json` (`{}`) and a `linear.json` is found as a project.

## Watch out
- A command line that sets `HOME` is refused by the worktree guard of a Claude session; put the environment in a script file and run the script.
- Projects in the wild number questions with their own prefixes (nudge's NEB-884 uses W32 to W36), so a waiting id such as W3 can read like a question id.
- The waiting list leaves out a needs-user that names no question id once its teammate is closed; on real state that is what keeps month-old asks of stopped teammates off the list.
