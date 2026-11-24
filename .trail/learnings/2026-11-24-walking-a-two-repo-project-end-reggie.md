---
title: Walking a two-repo project end to end in a sandbox
author: Reggie
date: 2026-11-24
branch: multi-repo-projects-step-1
cost: { minutes: 9, tokens: 5668 }
touches:
  - "bin/fc_repolist.py#ensure"
  - "bin/fc_spec.py#cmd_spec_check"
  - "bin/fc_linear.py#refresh_tasks"
  - "bin/fc_blockers.py#judge"
  - "bin/fc_init.py#key_problems"
---

## Decided
- A two-repo walk needs, per `compose` process, an `env -i` environment with `HOME`, `COMPOSE_CONFIG`, `COMPOSE_NO_HERDR=1`, `COMPOSE_NO_PREFLIGHT=1`, `COMPOSE_NO_TREEHOUSE=1`, `GIT_CONFIG_GLOBAL` and `LINEAR_URL` at a local stand-in, with a fake key in the sandbox's own `auth.json`.
- `COMPOSE_NO_PREFLIGHT=1` is not optional: `fc_init.key_problems` posts to `https://api.linear.app/graphql` itself, ignoring `LINEAR_URL`, from `init`, `doctor`, `run` and `verify`.
- Put a stub `gh` first on PATH that exits 1: `fc_repolist.about` runs `gh repo view` on every first listing, and the about line then falls back to the README.
- The test repos are `git init` folders with `origin` set to `git@github.com:acme/<name>.git`, never fetched; the path map lands beside the config as `repos.json`.
- The stand-in must answer, matched on the whitespace-normalised query text: the project by slug, the project's content read, `projectUpdate`, the paged open-issues query, `issueUpdate`, the description read-back after each write (`issue(id) { description }`), and sync's `LINEAR_FEATURE` and `LINEAR_CHILDREN`.
- Apply every write at once in the stand-in: `fc_linear_cmd.await_write` reads the text back until it changes.
- Log each request with its operation and whether it wrote: that log is how a rerun is shown to write nothing.
- A one-shot failure switch on `issueUpdate` (a file naming the issue) is enough to walk a run that dies halfway.
- A task fixture that only the repo rules may fail needs a feature with `## Plan` and `### Codebase brief`, a story with one bullet under `## Acceptance criteria`, and tasks with `files:`, `protected:` and `test:`.

## Tried and ruled out
- `compose init --project walk` in the sandbox: it stops on the missing tools; a hand-made `.compose/walk/` with `verify.json` (`{}`) and `linear.json` (`{"feature": "<id>"}`) in the repo stands in for `init --here`.
- `compose run` to see a cross-repo hold: it detaches into its own session, and its preflight stops before the `waits` lines without gh auth and real harnesses.
- `compose tasks` and `compose status` for the hold reason: neither names the other repo; the reason comes from `fc_blockers.released`, which a short script can call with run's arguments.

## Watch out
- Once a link is marked `"listed"`, `compose linear project` never records the repo's path again (`fc_repolist.ensure` returns before `fc_repos.record_path`), so after `compose doctor` drops a path the advice to run it in that checkout does not bring the repo back.
- With a key and no `"project"` in the link, the refresh before `spec-check` syncs with no repo name, so every repo's tasks land in this repo's `tasks.json` and are checked as its own.
- Sync's Ready-mark refresh asks a query the walk does not need; an unknown-query error there prints one `linear ready:` line and nothing breaks.
- A finished task of a repo that is not on this machine still fails `spec-check`.
