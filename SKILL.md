---
name: compose
description: The full fast loop for a feature, from first question to verified stacked branches. Find the way with wayfinder until the decisions are settled, asked once, as text or on a Lavish page when visual, and recorded on the feature in the repo's Linear project, plan, split into tiny numbered tasks with tests, fan the tasks out to parallel implementers (pi running GLM 5.3 Flash through an OpenRouter preset; OpenCode and Claude available per task), verify locally with jev browser runs and a Mercury review, register each chain as a gh stack, keep every task's Linear issue in step, and inside herdr run the implementers as named agents in the project's own herdr worktree space, grouped under the repo's, that the team lead steers. Use when the user says "compose", "fast loop", "fan out", or asks to build a feature or prototype fast. Works in any repo.
---

# compose

You are the team lead, or, inside herdr, the engineering manager who hands the project to one (0h). The implementer is pi running GLM 5.3 Flash (through the OpenRouter preset `glm-5-3-flash-fast`); OpenCode or Claude can take a task via `model:`. Verification is local; nothing is pushed.
This skill is the whole flow: the moment the user invokes it with a task, start at step 0 and carry it through to verified, stacked branches.

| Who | Does | Never does |
|---|---|---|
| You (team lead; the engineering manager likewise) | Find the way with the user (wayfinder), write the spec, start the planner teammate once wayfinder reports done and review the planner's cut at 3b instead of writing the plan and every task yourself, move the issues through their Linear states, brief teammates for the acceptance tests and any other code, dispatch, review diffs, run verify, re-split escalated tasks into smaller ones, submit the stack when told | Write or edit any code, test, config, or repo doc, not one line (teammates do, Rules); merge anything: a human reviewer merges the PRs on GitHub, or `compose land --yes` on the user's word (7) |
| Teammates (Claude sessions in the project's space, 0h) | Acceptance tests and the stubs and groundwork they need, escalated work, conflict fixes, docs after merge, wayfinder questions, research; a toolsmith (briefed by `compose toolsmith`) files compose's bugs on GitHub, and on the maintainer's machine fixes them on a branch | Plan, re-scope their brief, address the product owner except through `report` and their own pane |
| Designer (a Claude teammate in its own pane, 0d) | Continue design work in Figma from the ticket's level: read the inputs, write the design-system epic, design stories, and design tasks on Linear, start `design-NNN` builders and a `design-review` reviewer in their own panes, run the gate, hand off to code | Build in Figma itself (builders do), write any file in the repo (Linear only), decide a product question |
| Implementer (fast model via pi or OpenCode; inside herdr a named pi agent in its own pane of the `implementers` tab) | One task issue's brief, the named files, until its test passes, at most 350 changed lines, adding no documents | Edit protected files, plan, touch other tasks, address the product owner |
| `compose verify` | Checks, jev browser e2e (through `bp` for extensions), Mercury diff review | Push, open PRs, run CI |

## Workflow

### 0. Know the repo's Linear project
Everything that would be a ticket lives on Linear, never as files in the repo: the feature (with its decisions), its stories, its tasks.
The repo's Linear project lives in one file, `.compose/linear.json` in the repo's main checkout: `{"project": "<Linear project URL>", "team": "Engineering"}`.
Every worktree of the repo reads that file, so run `compose linear project` from wherever you are rather than looking for the file in your own checkout.
If it prints `no .compose/linear.json in <folder>`, ask the user for the link to the Linear project this repo's work belongs to, write the file in that folder with that URL and the team `Engineering` (unless the user names another), and never commit it.
`compose init` ignores `.compose/`, `graft/`, and `.trail/` in every checkout before anything else.
You create and edit issues and write judgment comments with `compose linear` (`get <id> [--body] [--comments]`, `list --parent <id> | --search "<terms>"`, `create`, `update <id>`, `comment <id>`, `labels`, `project`, `doc`), never a Linear MCP tool; the CLI moves task, story, and feature states and writes the routine run comments, all with the workspace's Linear key.
The Linear key is required: without it `compose doctor` fails and `compose linear` stops with the line that stores one.
`get` and `list` print one compact line per issue (`<id> <state> <label> <title>`); add `--body` or `--comments` to `get` only when you need them.
Write bodies come from `--file <path>` or stdin, never from the command line.
`update` writes the body only with `--section "<heading>"`, `--append`, or `--description`; `--file` without one of them exits and writes nothing.
`get --body` ends with each section's hash: a `--section` write whose text came from that read passes it as `--expect <hash>`, and writes nothing when another writer changed the section since.
`list` and `create` default to the project and team in the file; `get`, `update`, and `comment` refuse an issue in another project unless `--any-project`.
Confirm the file points at a real project before any other Linear call: `compose linear project` must print a project on its team.
Scope every Linear read, search, and write to that project and team; when a project is in play, its feature comes from `.compose/<slug>/linear.json`.
Stop and ask the user, never guess, when any of these happen:
- a `compose linear` call fails (auth, connection, timeout): say which call and its error, and wait; do not carry on as if Linear were empty
- the file's project or team is not found, or the user hands you an issue that lives in another project
- you cannot tell which epic, feature, story, or ticket the work belongs to, or a search returns more than one plausible match

### 0i. The levels: project, epic, feature, story, task
Linear has projects, issues, sub-issues, and labels; these five levels are how compose uses them. Labels `Epic`, `Feature`, `Story`, `Task`, and `Ready` live on the Engineering team (`Feature` exists; create the others once with `compose linear labels --create <name>` if `compose linear labels` does not show them).
`Ready` marks a story or feature that can start now, and `compose ready` sets it.

| level | in Linear | in compose | holds |
|---|---|---|---|
| **project** | the Linear project from `.compose/linear.json` | the repo | epics and features; set once, never created by you |
| **epic** | top-level issue, label `Epic`; its description is the wayfinder map | a wayfinder map while in fog; once its features are known, a set of projects (one per feature) the engineering manager runs together | features, as sub-issues; its decisions live in its description |
| **feature** | label `Feature`; sub-issue of its epic, or top-level when there is no epic | one compose project: worktree, plan, one stack | stories; its description is the settled spec (or the map, when the feature is small enough to be its own map) |
| **story** | label `Story`; sub-issue of its feature | a contiguous group of layers in the feature's stack | tasks; its description is one user-visible slice with acceptance criteria |
| **task** | label `Task`; sub-issue of its story | one task issue (with its `compose` block), one stack layer | goal, acceptance criteria, test plan |

A feature with one obvious slice still gets one story, so tasks always have the same parent shape. A bug or improvement is a feature with one story. Fewer levels is always right when the work is small: an epic exists only when wayfinder finds fog (more than one feature's worth of open decisions).

### 0j. What you were given decides where you start
The `compose` skill accepts a Linear project link, an issue (epic, feature, story, or task, by URL or identifier such as `ENG-123`), a loose idea, or a loose idea plus the level the user names ("this is a story"). Resolve the level from the issue's label, else from its parent chain, else from the user's word; when none of these settle it, ask. Then:
- **project link, nothing else**: list its open epics and features and ask which one, or whether this is new work.
- **epic**: the epic is taken through to done as a unit, but it is never one project: each feature is its own project (0a), with its own worktree, team lead, and stack. Which path depends on whether its features are known:
  - **Fog** (you cannot yet name the features the epic needs): work the map with wayfinder (0b: one card set for the epic's open decisions), graduating features out of the fog. Once every feature can be named, switch to the settled path.
  - **Settled** (the features exist as sub-issues, or can be named from the map): create one project per feature and start a team lead for each (0h), all at once. Each team lead runs its own wayfinder (0b) with its groundwork (0g) feeding it; the engineering manager relays every open card set across the epic to the product owner at once (Rules: ask once). A feature starts from trunk unless it truly needs another feature's code (it calls, imports, or edits what that feature builds), the same rule as a task's `after:`: then `compose init --project <slug> --after <other-slug>` starts its branch from that feature's project branch, so its stack sits on top.
- **feature**: if its map is not clear, wayfinder it (step 0b). When it has no stories, the planner teammate proposes them as a `needs-user` list (one issue per story, each the thinnest slice that works end to end and unblocks the next, at most 5 criteria, in the order they should ship) and creates them on the product owner's go; then it writes the plan (step 1) and splits (step 2) under each story.
- **story**: its feature is the parent; use or create that project. The planner teammate writes this story's tasks only; other stories' tasks are not touched.
- **task**: it belongs to a story that belongs to a feature; the planner teammate lints and re-splits it, then dispatch that task alone (`run --only NNN`) through the 3b gate.
- **loose idea**: wayfinder names the destination. No fog means it is a feature: create the feature issue (top-level) and continue. Fog means it is an epic: create the epic, chart it, stop.
- **user names the level**: create the issue at that level with the user's words as the title. A story needs a feature and a feature may need an epic: ask for the parent link, or create the parent too if the user says it is new. Then proceed as above.
Everything you create is a Linear issue in the project from step 0, labelled, parented, in state Todo or Backlog, before any file is written.

### 0s. Every level's state, kept true
A parent's state rolls up from its children; set it in the same step as the child change that moves it, never later:
| Level | In Progress | In Review | Done |
|---|---|---|---|
| task | dispatched (`run` starts it) | its layer passed | its layer merged |
| story | its first task is dispatched | every task is In Review or Done | every task is Done |
| feature | the session starts work on it: wayfinder, a recorded decision, the project, or the plan | every story is In Review or Done and `verify` passed | every story is Done and the project branch merged |
| epic | its first feature is In Progress | every feature is In Review or Done | every feature is Done |
A Backlog or Todo parent with an In Progress child is a bug: fix it the moment you see it.
Canceled children do not count. A re-opened child (a failed fix round, a new task) moves its parents back to In Progress.
Checkpoints, each followed by a reconcile of the whole tree (`compose linear list --parent <feature>`, then `--parent` each story, compared against this table and `compose status`): the end of planning (step 3b), right before and after every `run`, after every `verify`, after `clean`, and at session start (0r).
At every checkpoint the team lead, and the designer in its project, runs `compose ready --project <slug>`.
The CLI also refreshes the `Ready` marks after every roll-up, `sync`, the end of `run`, a `verify` PASS, and `clean`.

### 0h. Inside herdr, the fleet is visible and steerable: read sections/0h.md

### 0a. Put the work in a project, in its own worktree: read sections/0a.md

### 0e. Explore an existing repo first: read sections/0e.md

### 0b. Find the way first, always: read sections/0b.md

### 0g. Groundwork and the smoke check run while the product owner answers: read sections/0g.md

### 0d. Design work: the designer: read sections/0d.md

### 0r. Pick up where the last session stopped
Everything is on disk, so a restart is a non-event. At the start of a session on an existing project, before anything else: `compose status --repo <wt> --project <slug>` and `compose watch ... --now`, which prints the current state as event lines and marks it seen (each watcher remembers what it last reported, so the next `watch` reports only what changes after this, and nothing that happened while no watch ran is lost), then reconcile the Linear tree against them (0s). Then:
- `run INTERRUPTED` or a task `interrupted`: the previous run was killed (Ctrl-C, a reboot); a closed tab or a crashed session no longer ends one. Its output is in `results/run.log`. `compose resume` re-runs what did not pass, in fresh agents, keeping passed layers. The CLI moves Linear for the tasks it re-runs; only a task that died mid-run needs its state checked against the disk.
- `HALTED ... payment required` or `event halt=payment`: the user's stop; say so and wait for the word to `resume`.
- `event away=on`: the product owner is away; continue under 0h's Away rules.
- `event yolo=on`: yolo is on until an explicit off; continue under 0h's Yolo rules.
- `event run=live`: a run is in progress, possibly started by the session before you; it outlives the session that started it. Supervise with `watch` and read `results/run.log`; do not start a second run.
- Inside herdr, as the engineering manager: `compose crew list --project <slug>`. A live `<slug>-orchestrator` owns the project: resume the relay loop (0h) and do nothing else. None, or its tab is closed: start it again with `crew start --role orchestrator`, briefed to continue from 0r.
- As the team lead: `crew list` too; a live wayfinder teammate and helpers keep their work, so watch them rather than starting duplicates. Unanswered wayfinder cards and no live wayfinder teammate: start one again, briefed to ask them again, and to run `npx -y lavish-axi poll` on the same file when a visual page is open (queued answers are still waiting there).
- `status` shows `CHANGE <id> open`: a change of course was under way. Its questions unanswered and no live wayfinder teammate: start one again with `compose wayfinder --change <id>`, briefed to poll the same page. Its answers in: continue step 8 from the re-plan.
- Herdr agents of a dead run are closed by the next run of their task; do not close them by hand.

### 1. Plan from the spec: read sections/1.md

### 2. Split into tasks: read sections/2.md

### 3. Write the tests first (red): read sections/3.md

### 3b. The task list goes to the product owner; it waits only when it departs from what they settled: read sections/3b.md

### 4. Dispatch: read sections/4.md

### 5. Review: read sections/5.md

### 6. Verify: read sections/6.md

### 6a. Browser work goes through `bp`: read sections/6a.md

### 7. Escalate and clean: read sections/7.md

### 8. Change course: read sections/8.md

## Task issue format

`model:` is `pi:<provider/model>` for the pi harness (the default: lean, ~1K tokens of fixed context per turn), an OpenCode model id, `claude:<model-id>` for Claude Code on that model, or bare `claude` for Claude Code's default. A Claude model is always written with its `claude` prefix, never as a bare id, so it is never mistaken for a pi or OpenCode model. Omit it for the CLI default, `pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast` (GLM 5.3 Flash; the preset pins Together, then CoreWeave). gpt-oss-120b on Cerebras was the default until 2026-10-10 and was dropped for missing too often.
Leave `model:` unset. A task that seems to need a stronger model is a task to split, or a decision card. Setting `model: claude` (or any non-default model) on a planned task is the product owner's decision: it is a wayfinder card with the reason (0b), never only into a later report.
There is no fallback by default. When `models.fallback` is set, a task stays on its model for `models.fallback_after` failed attempts (default 2: misses, errors, and timeouts count) and then moves to the fallback in a fresh session that carries the failure; a rate limit, or a provider too busy to start the turn (a 429 or a 5xx before any token), moves it at once, and with no fallback it gets a fresh session after a wait without spending an attempt; verify's fix rounds after the first use it too. `bench` never falls back.
`run` and `resume` probe every model they may call (each task's, and the fallback) with one tiny prompt before cloning anything; a wrong model id or preset slug stops the run there with the provider's message.
Models, the fallback, and the OpenRouter key come from the active workspace in `~/.compose/config.json` (`compose workspace [name | add | key | remove <name>]`, or `/compose:workspace`; `default` always exists); `run` and `verify` print `workspace <name>` first, so check it before spending.
A local OpenAI-compatible server is declared under `providers` in that config (`base_url`, optional `models`), and a model id `pi:<provider>/<model>` (pi) or `<provider>/<model>` (OpenCode) runs on it at `$0.0000`; a keyed one takes `compose workspace key <name> --provider <provider>`. `run` and `resume` stop before cloning when the server does not answer `GET <base_url>/models`.

The issue's title is the task's title. Its description is the brief, then the block the CLI reads, last:

````markdown
## Goal
One paragraph. What to build, where, exact export names.

## Acceptance criteria
- One bullet per behavior. Each is checked by the test.

## Test plan
- How to run: the `test:` command below.
- Covered: which behaviors the test exercises, in words, not the literal cases.
- Holdout: state that the test has cases not listed here (do not list them).

## Constraints
- No new dependencies beyond what the goal names. Do not edit protected files.

```compose
id: 001
files: extension/deny_list.js
protected: tests/deny_list.test.js
test: node --test tests/deny_list.test.js
model: pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast
```
````

- `id` is the stack order a reviewer reads in, except that a task always sits above its `after` tasks; ids are unique within the feature.
- `after` is not in the block: it is the issue's "blocked by" links to other tasks of the feature. Each one becomes the layer this task stacks on, and makes it wait, so link only a task whose code this one needs. Any other blocker (a design story, another feature's issue) never stacks: `run` holds the task until it is Done or Canceled, or In Review with the Design label, as of the last `sync`, unless its task branch or project top layer is an ancestor of the trunk. `run` prints each release proof or hold reason.
- An issue under the feature without the block is not a task (a story): the CLI ignores it. One labelled `Task` is a hand task (a lowfi flow in Frame0): `run` never dispatches it, and spec-check counts it as one of its story's tasks.
- A design task (0d) adds `kind: design` and replaces `files:` with `nodes:`, the Figma page and top-level names it may touch; its `test:` is `compose figma judge --project <slug> --only NNN`. An optional `figma:` line (file key or url) names the task's own Figma file when it is not the stored one.

## Rules
- Fact-check before you say it. Every claim about an existing codebase, tool, system, or result comes from evidence gathered in this session: trail output with file:line, a command and its output, a test run, a doc you read. Keep observed facts apart from hypotheses, and when you have not verified something, say "not verified" or "unknown" instead of stating a guess. Never present a likely-enough answer as fact or fill a gap with what the code probably does.
- Facts about data come from the whole data set, counted with exact tools: a query or a script over every row, `LC_ALL=C` for text, never a `head` sample, a `sed -n` slice, or a locale-sensitive `sort | uniq`. A sample can suggest a hypothesis; it never becomes a "fact" in a ticket, brief, or contract. A surprising result gets a second, independent check before anyone relies on it.
- Exploring an existing repo includes its history: `git log` (and the earlier versions) of the spec, data, seed, and schema files, because what a field means is often settled in the commit that introduced it, not in today's file.
- Every recommendation is checked against the spec's literal words before it reaches the product owner: quote the line it rests on, and drop or flag any option the spec rules out (paging when the spec says every row, squashing when it says keep the history).
- Report outcomes faithfully: if work failed, partly passed, or was skipped, say so plainly with the evidence. Never round a partial result up to done.
  Report attempts per task, what escalated, what teammates did outside `run`, and the verify output.
- Before investigating or asking, read what is already established (the feature's Notes and Decisions, the codebase brief, earlier reports); do not re-derive or re-ask it.
- Evidence is not authorization: a diagnosis, a report, or a recommendation changes nothing by itself; the change still goes through its task or the product owner.
- A bug is diagnosed from an end-to-end reproduction of what the user sees, not from an internal error string or a hunch: name the trigger and the symptom, test the leading explanation with the smallest change that should flip the outcome, and look for the observation that would prove it wrong. When a faithful reproduction is impossible, say so instead of presenting a stand-in as equivalent.
- Nothing is implemented beyond what the product owner settled. Intake, wayfinder, stories, the plan, task issues, and tests are all preparation; `compose run` is the first implementing action. It starts without a further go when the task list matches the settled spec, and waits for an explicit go whenever it departs from it (step 3b), every wave.
- Tests are the contract. The implementer never edits them.
- A failure of compose itself is a `tool-bug` report, never a silent workaround and never an edit to the compose repo from a project (0h): the project works around it, the engineering manager's toolsmith files it on prodoxx/compose and, on the maintainer's machine only, fixes it on a branch, and the product owner approves the merge.
- Commit messages describe the change itself, in plain words (`Save submissions in one transaction`). Teammates and the team lead never mention compose, waves, task ids, teammates, Linear, or the process in a commit message; the CLI's own commits follow the same rule, and a message that slips through is fixed with `compose reword`, never `rebase -i`, `filter-branch`, or `--amend` and a force push.
- The engineering manager and the team lead never write or edit code: no source, test, stub, fixture, build or CI config, generated file, or repo doc, not even a one-line fix. They read code to review it, write Linear, write the CLI's JSON (`.compose/linear.json`, `.compose/<slug>/linear.json`, `verify.json`) and the layers' PR text (`.compose/<slug>/pr/`, step 4), and run `compose`, `git` reads, and `herdr`. Every change to the repo is teammate work or a task for `run`. For the team lead a hook enforces it: its tab carries `COMPOSE_ROLE=orchestrator`, and the plugin's `compose guard` denies its Write and Edit calls inside a repository. Do not route around the hook through Bash.
- Every document you write for a human is shaped by the `i-have-adhd` skill: Linear descriptions and comments (maps, specs, plans, stories, tasks, grilling notes, steers), and the briefs you give teammates. That skill is user-invoked only, so the Skill tool cannot load it; before writing the first document of a session, read `~/.claude/plugins/marketplaces/i-have-adhd/skills/i-have-adhd/SKILL.md` and apply its rules to everything you write from then on. Writing-plans and the task issue format fix the sections; i-have-adhd fixes the shape inside them: first line is the next action, multi-step work is numbered, lists stop at five, no preamble, no recap.
- You steer, you do not type. Talk to the product owner in outcomes, not mechanics; empty polls and no-change updates are not progress. Your final message stands alone: what finished, the stack, the cost, and the decision needed.
- Every round-trip to the product owner stalls the whole project, so ask once, through wayfinder (0b): start groundwork and the smoke check (0g) at once so its toolchain findings join the map, put every decision you can see coming (scoring, toolchain, dependency bumps, an unclear quality bar) in the wayfinder's card set, every card with a recommendation. Keep groundwork running while the product owner answers. Ask mid-run only when the run cannot continue without the answer.
- The CLI's own config is yours to fix without asking: a wrong model id, preset slug, or typo in `~/.compose/config.json` `models`, `verify.json`, or `linear.json`. Fix it, re-run, and mention it in your next report. Switching to a different model or provider, keys, and anything that costs money are still the product owner's.
- Linear is the source of truth. What it records as decided is decided: read it before asking the user anything (0b), and write every new answer back to it, so the next session finds it. Nothing written in words is ever a file in the repo: no plan, task, ticket, map, decision, grilling note, or task list, not under `.compose/` and not anywhere else (the only files there are the CLI's JSON: `linear.json`, `verify.json`, and `results/`, plus the layers' PR text under `pr/` (step 4), plus the wayfinder's Lavish page under `lavish/` and the Frame0 wireframes in `design.f0` (with its app's data in `frame0/`), all ignored and never committed); everything a human would read as a ticket is an issue under the feature issue, and its state is kept true at every step, at every level (0s): a task is Todo when written, In Progress when dispatched, In Review when its layer passed, Done when merged, Canceled when dropped or re-split, and its story, feature, and epic roll up from it in the same step.
- After the first dispatch, a change to the map, the plan, or a task brief is a change of course (step 8): opened with `change start` and closed with `change done`, never an untracked edit, so it can be undone.
  That includes step 7's re-split of an exhausted task and the product owner's edits to the task list in 3b.
- A `PAYMENT REQUIRED` stop is the user's, not yours. When `run` or `verify` exits 3 with that banner, stop everything and tell the user in one short message: which provider refused, the provider's own words, the top-up link the banner printed, and that `compose resume --repo <path> --project <name>` continues from exactly where it stopped (passed tasks keep their branches, remaining tasks run, a halted verify reruns). Do not switch providers, re-dispatch, hand-implement, or retry around a billing stop. When the user says the account is fixed, run `resume`.
- A task branch changes at most 350 lines and never adds a document. These are CLI-enforced; plan tasks that fit.
- Never push or submit a stack without the user's explicit permission for that push; `compose away --push` or `compose yolo --push` is that permission for the project's stack, given in advance; neither posture without --push ever pushes. `compose land --yes` merges on GitHub only when the user said to land; neither posture covers it. `run` registers stacks locally only.
- While the product owner is away (`results/yolo.json`, `event away=on` or `event yolo=on`), decisions follow 0h's Away or Yolo paragraph: the recommendation, logged; the never-set still waits.
- Parallelize by default: before every step ask what else can start now.
  Whatever has no real dependency (tasks with no `after:`, test writers, groundwork, reviews, merges of independent branches, toolsmith fixes, file checks) runs at once, on as many agents as the machine and the configured parallel limits allow.
  Wait only when a real dependency says so.
- Fresh session per task. Never chain tasks in one session.
- Keep the target repo's fixed context small: short AGENTS.md, no unneeded MCP servers, no duplicate skills.
- Every turn re-sends the whole session, and every Claude teammate compacts at 150k tokens, so spend turns and context on purpose.
  Look at an image once, downscaled or cropped first (`compose shrink <png>`), and export or read it again only after it changed.
  Draw Frame0 in `compose frame0 draw` lists, a page per call, never shape by shape; reorder, delete, and lint its pages with `compose frame0 move|delete|links|lint`.
  A harness subagent gets a named model (`model: "sonnet"` for searches and fixes), never the session's by inheritance.
- Build a trail graph in the repo (`trail build .`, git-ignored). The CLI prepends a `trail ask` context pack with exact `file:line` and source to every implementer and fixer prompt, and tells the model to use `trail ask` and `trail callers` instead of exploring. Without trail the fast model reads whole files.
- Always show timing and cost. Paste the CLI's `run start` and `run done` lines, the per-task table, the `verify` stage lines, and the final `cost` line into your report.
