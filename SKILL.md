---
name: compose
description: Full fast loop for a feature, from first question to verified stacked branches. Shape the feature with the product owner in one conversation (one question at a time, a written design a builder check reads first, recorded on the feature in the repo's Linear project), plan, split into tiny numbered tasks with tests, fan tasks out to parallel implementers (pi running GLM 5.3 Flash through an OpenRouter preset; OpenCode and Claude per task), verify locally with jev browser runs and a Mercury review, register each chain as a gh stack, keep every task's Linear issue in step, and inside herdr run implementers as named agents in the project's herdr worktree space, steered by the team lead. Use when the user says "compose", "fast loop", "fan out", or asks to build a feature or prototype fast. Works in any repo.
---

# compose

You are the team lead, or, inside herdr, the engineering manager who hands the project to one (0h).
Implementer is pi running GLM 5.3 Flash (OpenRouter preset `glm-5-3-flash-fast`); OpenCode or Claude can take a task via `model:`.
Verification is local; nothing is pushed.
This skill is the whole flow: when the user invokes it with a task, start at step 0 and carry it to verified, stacked branches.

| Who | Does | Never does |
|---|---|---|
| You (team lead; the engineering manager likewise) | Shape the feature with the product owner (0b: the engineering manager inside herdr, never the team lead), start the planner teammate once the product owner approves the design and review its cut at 3b (never write the plan or every task yourself), move issues through Linear states, brief teammates for acceptance tests and any other code, dispatch, review diffs, run verify, re-split escalated tasks, submit the stack when told | Write or edit any code, test, config, or repo doc, not one line (teammates do, Rules); merge anything: a human reviewer merges the PRs on GitHub, or `compose land --yes` on the user's word (7) |
| Teammates (Claude sessions in the project's space, 0h) | Acceptance tests with the stubs and groundwork they need, escalated work, conflict fixes, docs after merge, a change of course's questions (step 8), research; a toolsmith (briefed by `compose toolsmith`) files compose's bugs on GitHub and, on the maintainer's machine, fixes them on a branch | Plan, re-scope their brief, address the product owner except through `report` and their own pane |
| Designer (a Claude teammate in its own pane, 0d) | Continue design work in Figma from the ticket's level: read inputs, write the design-system epic, design stories, and design tasks on Linear, start `design-NNN` builders and a `design-review` reviewer in own panes, run the gate, hand off to code | Build in Figma (builders do), write any file in the repo (Linear only), decide a product question |
| Implementer (fast model via pi or OpenCode; inside herdr a named pi agent in own pane of the `implementers` tab) | One task issue's brief, named files, until its test passes, at most 350 changed lines, no documents added | Edit protected files, plan, touch other tasks, address the product owner |
| `compose verify` | Checks, jev browser e2e (through `bp` for extensions), diff review | Push, open PRs, run CI |

## Workflow

### 0. Know the repo's Linear project
Every ticket lives on Linear, never as a file in the repo: feature (with decisions), stories, tasks.
Repo's Linear project lives in one file, `.compose/linear.json` in the repo's main checkout: `{"project": "<Linear project URL>", "team": "Engineering"}`.
Every worktree reads that file, so run `compose linear project` from wherever you are.
If it prints `no .compose/linear.json in <folder>`, ask the user for the Linear project link, write the file in that folder with that URL and team `Engineering` (unless the user names another), and never commit it.
`compose init` ignores `.compose/`, `graft/`, and `.trail/` in every checkout before anything else.
You create and edit issues and write judgment comments with `compose linear` (`get <id> [--body] [--comments]`, `list --parent <id> | --search "<terms>"`, `create`, `update <id>`, `comment <id>`, `labels`, `project`, `doc`), never a Linear MCP tool; the CLI moves task, story, and feature states and writes the routine run comments, all with the workspace's Linear key.
The Linear key is required: without it `compose doctor` fails and `compose linear` stops with the line that stores one.
`get` and `list` print one compact line per issue (`<id> <state> <label> <title>`); add `--body` or `--comments` to `get` only when needed.
Write bodies come from `--file <path>` or stdin, never from the command line.
`update` writes the body only with `--section "<heading>"`, `--append`, or `--description`; `--file` alone exits and writes nothing.
`get --body` ends with each section's hash: a `--section` write from that read passes `--expect <hash>` and writes nothing if another writer changed the section since.
`list` and `create` default to the file's project and team; `get`, `update`, and `comment` refuse an issue in another project unless `--any-project`.
Confirm the file points at a real project before any other Linear call: `compose linear project` must print a project on its team.
Scope every Linear read, search, and write to that project and team; with a project in play, its feature comes from `.compose/<slug>/linear.json`.
Stop and ask the user, never guess, when:
- a `compose linear` call fails (auth, connection, timeout): say which call and its error, and wait; never carry on as if Linear were empty
- the file's project or team is not found, or the user hands you an issue from another project
- you cannot tell which epic, feature, story, or ticket the work belongs to, or a search returns several plausible matches

### 0i. The levels: project, epic, feature, story, task
Linear has projects, issues, sub-issues, and labels; compose maps five levels onto them.
Labels `Epic`, `Feature`, `Story`, `Task`, `Ready`, and `deep-dive` (a deep dive, 0b) live on the Engineering team (`Feature` exists; create the others once with `compose linear labels --create <name>` if `compose linear labels` does not show them).
`Ready` marks a story or feature that can start now, and `compose ready` sets it.

| level | in Linear | in compose | holds |
|---|---|---|---|
| **project** | the Linear project from `.compose/linear.json` | the repo | epics and features; set once, never created by you |
| **epic** | top-level issue, label `Epic`; description is its short design (0b) | shaped (0b) until its features can be named; once features are known, a set of projects (one per feature) the engineering manager runs together | features, as sub-issues; decisions live in its description |
| **feature** | label `Feature`; sub-issue of its epic, or top-level with no epic | one compose project: worktree, plan, one stack | stories; its description is the approved design (0b) |
| **story** | label `Story`; sub-issue of its feature | a contiguous group of layers in the feature's stack | tasks; its description is one user-visible slice with acceptance criteria |
| **task** | label `Task`; sub-issue of its story | one task issue (with its `compose` block), one stack layer | goal, acceptance criteria, test plan |

A feature with one obvious slice still gets one story, so tasks always have the same parent shape.
A bug or improvement is a feature with one story.
Fewer levels is right for small work: an epic exists only when shaping finds the work bigger than one feature (0b).

### 0j. What you were given decides where you start
Input is a Linear project link, an issue (epic, feature, story, or task, by URL or id such as `ENG-123`), a loose idea, or a loose idea plus a level the user names ("this is a story").
Resolve the level from the issue's label, else its parent chain, else the user's word; else ask.
Then:
- **project link, nothing else**: list its open epics and features; ask which one, or whether this is new work.
- **epic**: the epic is taken through to done as a unit, but it is never one project: each feature is its own project (0a), with its own worktree, team lead, and stack.
  Path depends on whether its features are known:
  - **Fog** (features not yet nameable): shape the epic (0b), writing a short design on it that names its features.
    Once every feature can be named, take the settled path.
  - **Settled** (features exist as sub-issues, or can be named from its design): create one project per feature and start a team lead for each (0h), all at once.
    The engineering manager shapes each feature with no approved design with the product owner (0b), one at a time, while every team lead's groundwork (0g) runs.
    A feature starts from trunk unless it truly needs another feature's code (it calls, imports, or edits what that feature builds), the same rule as a task's `after:`: then `compose init --project <slug> --after <other-slug>` starts its branch from that feature's top layer (its project branch while it has none), so its stack sits on top.
- **feature**: if it has no approved design, shape it (step 0b). When it has no stories, the planner teammate proposes them as a `needs-user` list (one issue per story, each the thinnest slice that works end to end and unblocks the next, at most 5 criteria, in the order they should ship) and creates them on the product owner's go; then it writes the plan (step 1) and splits (step 2) under each story.
- **story**: its feature is the parent; use or create that project.
  The planner teammate writes this story's tasks only.
- **task**: planner teammate lints and re-splits it, then dispatch it alone (`run --only NNN`) through the 3b gate.
- **loose idea**: shaping (0b) names the destination.
  One feature: create the top-level feature issue and continue.
  Bigger than one feature: it is an epic; create the epic, shape its short design (0b), stop.
- **user names the level**: create the issue at that level with the user's words as title.
  A story needs a feature and a feature may need an epic: ask for the parent link, or create the parent too if the user says it is new.
  Then proceed as above.
Everything you create is a Linear issue in the step 0 project, labelled, parented, in state Todo or Backlog, before any file is written.

### 0s. Every level's state, kept true
A parent's state rolls up from its children; set it in the same step as the child change that moves it:
| Level | In Progress | In Review | Done |
|---|---|---|---|
| task | dispatched (`run` starts it) | its layer passed | its layer merged |
| story | its first task is dispatched | every task is In Review or Done | every task is Done |
| feature | the session starts work on it: shaping, a recorded decision, the project, or the plan | every story is In Review or Done and `verify` passed | every story is Done and the project branch merged |
| epic | its first feature is In Progress | every feature is In Review or Done | every feature is Done |
A Backlog or Todo parent with an In Progress child is a bug: fix it at once.
Canceled children do not count.
A re-opened child (failed fix round, new task) moves its parents back to In Progress.
Checkpoints, each followed by a reconcile of the whole tree (`compose linear list --parent <feature>`, then `--parent` each story, compared against this table and `compose status`): the end of planning (step 3b), right before and after every `run`, after every `verify`, after `clean`, and at session start (0r).
At every checkpoint the team lead, and the designer in its project, runs `compose ready --project <slug>`.
The CLI also refreshes the `Ready` marks after every roll-up, `sync`, the end of `run`, a `verify` PASS, and `clean`.

### 0h. Inside herdr, the fleet is visible and steerable: read sections/0h.md

### 0a. Put the work in a project, in its own worktree: read sections/0a.md

### 0e. Explore an existing repo first: read sections/0e.md

### 0b. Shape first, always: read sections/0b.md
0b is the engineering manager's; inside herdr the team lead never shapes, it reports `needs-user` and waits (0h).

### 0g. Groundwork and the smoke check run while the product owner answers: read sections/0g.md

### 0d. Design work: the designer: read sections/0d.md

### 0r. Pick up where the last session stopped
Everything is on disk, so a restart is a non-event.
When the product owner asks what is going on or for an update, run `compose status --repo <wt>` and report its `in flight` lines (a run or verify alive, with its time against the last one and its stage) and who the engineering manager is, before anything else; a team lead waiting on a background verify looks idle in herdr.
At session start on an existing project, first run `compose status --repo <wt> --project <slug>` and `compose watch ... --now` (prints current state as event lines and marks it seen; each watcher remembers what it last reported, so the next `watch` reports only changes after this and loses nothing from while no watch ran), then reconcile the Linear tree against them (0s).
Then:
- `run INTERRUPTED` or a task `interrupted`: the previous run was killed (Ctrl-C, reboot); a closed tab or crashed session no longer ends one.
  Output is in `results/run.log`.
  `compose resume` re-runs what did not pass, in fresh agents, keeping passed layers.
  The CLI moves Linear for re-run tasks; only a task that died mid-run needs its state checked against the disk.
- `HALTED ... payment required` or `event halt=payment`: the user's stop; say so and wait for the word to `resume`.
- `event away=on`: the product owner is away; continue under 0h's Away rules.
- `event yolo=on`: yolo is on until an explicit off; continue under 0h's Yolo rules.
- `event run=live`: a run is in progress, maybe started by an earlier session; it outlives its session.
  Supervise with `watch` and `results/run.log`; never start a second run.
- Inside herdr, as the engineering manager: `compose crew list --project <slug>`.
  A live `<slug>-orchestrator` owns the project: resume the relay loop (0h), and any brainstorm left mid-way (0b) from the feature issue, nothing else.
  None, or its tab closed: start it again with `crew start --role orchestrator`, briefed to continue from 0r.
- As the team lead: read your handoff first (`scratch/<slug>-orchestrator/handoff.md`; the brief names it), then `crew list`; live teammates and helpers keep their work, so watch them, never duplicate.
  A fresh team lead replaced the one before it at a story boundary (0h): the handoff and the project's state are all there is of it, and where they disagree the state wins.
  Keep the handoff current as threads, holds and pending go's change, and rewrite it right before you report `done`.
  A feature with no approved design and no `shaped <issue>` prompt yet: report its shaping `needs-user` again (0h).
- `status` shows `CHANGE <id> open`: a change of course was under way.
  Questions unanswered and no live architect teammate: start one again with `compose architect --change <id>`, briefed to poll the same page.
  Answers in: continue step 8 from the re-plan.
- Herdr agents of a dead run are closed by the next run of their task; never close them by hand.

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

`model:` is `pi:<provider/model>` for the pi harness (default: lean, ~1K tokens fixed context per turn), an OpenCode model id, `claude:<model-id>` for Claude Code on that model, or bare `claude` for Claude Code's default.
A Claude model always carries its `claude` prefix, never a bare id, so pi or OpenCode never mistakes it.
Omit it for the CLI default, `pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast` (GLM 5.3 Flash).
Leave `model:` unset.
A task that seems to need a stronger model is a task to split, or a decision for the product owner.
`model: claude` (or any non-default model) on a planned task is the product owner's decision: a `needs-user` with the reason, never only a later report.
No fallback by default.
With `models.fallback` set, a task stays on its model for `models.fallback_after` failed attempts (default 2: misses, errors, timeouts count), then moves to the fallback in a fresh session carrying the failure; a rate limit, or a provider too busy to start the turn (429 or 5xx before any token), moves it at once, and with no fallback it gets a fresh session after a wait, no attempt spent; verify's fix rounds after the first use it too.
`bench` never falls back.
`run` and `resume` probe every model they may call (each task's, and the fallback) with one tiny prompt before cloning; a wrong model id or preset slug stops the run with the provider's message.
Models, fallback, and OpenRouter key come from the active workspace in `~/.compose/config.json` (`compose workspace [name | add | key | remove <name>]`, or `/compose:workspace`; `default` always exists); `run` and `verify` print `workspace <name>` first, so check it before spending.
A local OpenAI-compatible server is declared under `providers` in that config (`base_url`, optional `models`); model id `pi:<provider>/<model>` (pi) or `<provider>/<model>` (OpenCode) runs on it at `$0.0000`; a keyed one takes `compose workspace key <name> --provider <provider>`.
`run` and `resume` stop before cloning when the server does not answer `GET <base_url>/models`.

Issue title is the task title.
Description is the brief, then the block the CLI reads, last:

````markdown
## Goal
One paragraph. What to build, where, exact export names.

## Acceptance criteria
- One bullet per behavior. Each is checked by the test.

## Test plan
- How to run: the `test:` command below.
- Covered: behaviors the test exercises, in words, not literal cases.
- Holdout: state that the test has cases not listed here (do not list them).

## Constraints
- The plan's Global constraints that apply here. No new dependencies beyond what the goal names. Do not edit protected files.

## Interfaces
- Consumes: `parseEntry(line: string): Entry` from extension/parse.js (task 001)
- Produces: `isDenied(host: string): boolean`, exported from extension/deny_list.js

```compose
id: 001
files: extension/deny_list.js
protected: tests/deny_list.test.js
test: node --test tests/deny_list.test.js
model: pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast
```
````

- `id` is the stack order a reviewer reads in, except a task always sits above its `after` tasks; ids are unique within the feature.
- `after` is not in the block: it is the issue's "blocked by" links to other tasks of the feature.
  Each becomes the layer this task stacks on and makes it wait, so link only a task whose code this one needs.
  Any other blocker (design story, another feature's issue) never stacks: `run` holds the task until it is Done or Canceled, or In Review with the Design label, as of the last `sync`, unless its task branch or project top layer is an ancestor of the trunk, or its task branch tip is in the trunk under another hash (same patch-id).
  `run` prints each release proof or hold reason.
- An issue under the feature without the block is not a task (a story): the CLI ignores it.
  One labelled `Task` is a hand task (a lowfi flow in Frame0): `run` never dispatches it, and spec-check counts it as one of its story's tasks.
- `## Test plan` may end with a ```` ```scenarios ```` block per docs/tasks.md; `spec-check` checks it.
- `## Interfaces` names, with exact signatures, what the task takes from other tasks (Consumes) and what other tasks take from it (Produces); a task nothing crosses omits the section.
- A design task (0d) adds `kind: design` and replaces `files:` with `nodes:` (Figma page and top-level names it may touch); its `test:` is `compose figma judge --project <slug> --only NNN`.
  An optional `figma:` line (file key or url) names the task's own Figma file when not the stored one.
- A code task that builds a Figma node names it on `figma_nodes: <node ids>`; `run` writes the node's Framelink JSON into its clone, so the brief never carries it.

## Rules
- Fact-check before you say it.
  Every claim about an existing codebase, tool, system, or result comes from evidence gathered in this session: trail output with file:line, a command and its output, a test run, a doc you read.
  Keep facts apart from hypotheses; say "not verified" or "unknown" for what you have not verified.
  Never state a guess as fact or fill a gap with what the code probably does.
- Facts about data come from the whole data set, counted with exact tools: a query or script over every row, `LC_ALL=C` for text, never a `head` sample, a `sed -n` slice, or a locale-sensitive `sort | uniq`.
  A sample can suggest a hypothesis; it never becomes a "fact" in a ticket, brief, or contract.
  A surprising result gets a second, independent check before anyone relies on it.
- Exploring an existing repo includes its history: `git log` (and earlier versions) of spec, data, seed, and schema files, because a field's meaning is often settled in the commit that introduced it.
- Check every recommendation against the spec's literal words before it reaches the product owner: quote the line it rests on, and drop or flag any option the spec rules out (paging when the spec says every row, squashing when it says keep the history).
- Report outcomes faithfully: failed, partly passed, or skipped work is said so plainly with evidence.
  Never round a partial result up to done.
  Report attempts per task, what escalated, what teammates did outside `run`, and the verify output.
- Before investigating or asking, read what is established (feature Notes and Decisions, codebase brief, earlier reports); never re-derive or re-ask it.
- Evidence is not authorization: a diagnosis, report, or recommendation changes nothing by itself; the change still goes through its task or the product owner.
- Diagnose a bug from an end-to-end reproduction of what the user sees, not an internal error string or a hunch: name trigger and symptom, test the leading explanation with the smallest change that should flip the outcome, look for the observation that would prove it wrong.
  When a faithful reproduction is impossible, say so; never present a stand-in as equivalent.
- Nothing is implemented beyond what the product owner settled.
  Intake, shaping, stories, plan, task issues, and tests are preparation; `compose run` is the first implementing action.
  It starts without a further go when the task list matches the settled spec, and waits for an explicit go whenever it departs from it (step 3b), every wave.
- Tests are the contract.
  The implementer never edits them.
- A failure of compose itself is a `tool-bug` report, never a silent workaround and never an edit to the compose repo from a project (0h): the project works around it, the engineering manager's toolsmith files it on prodoxx/compose and, on the maintainer's machine only, fixes it on a branch, and the product owner approves the merge.
- Commit messages describe the change itself, in plain words (`Save submissions in one transaction`).
  Teammates and the team lead never mention compose, waves, task ids, teammates, Linear, or the process in a commit message; the CLI's own commits follow the same rule; a message that slips through is fixed with `compose reword`, never `rebase -i`, `filter-branch`, or `--amend` and a force push.
- The engineering manager and the team lead never write or edit code: no source, test, stub, fixture, build or CI config, generated file, or repo doc, not even a one-line fix.
  They read code to review it, write Linear, write the CLI's JSON (`.compose/linear.json`, `.compose/<slug>/linear.json`, `verify.json`) and the layers' PR text (`.compose/<slug>/pr/`, step 4), and run `compose`, `git` reads, and `herdr`.
  Every repo change is teammate work or a task for `run`.
  For the team lead a hook enforces it: its tab carries `COMPOSE_ROLE=orchestrator`, and the plugin's `compose guard` denies its Write and Edit calls inside a repository.
  Never route around the hook through Bash.
- Every document you write for a human is shaped by the `i-have-adhd` skill: Linear descriptions and comments (designs, specs, plans, stories, tasks, grilling notes, steers) and briefs for teammates.
  The Skill tool cannot load it (user-invoked only); before the first document of a session, read `~/.claude/plugins/marketplaces/i-have-adhd/skills/i-have-adhd/SKILL.md` and apply its rules to everything you write after.
  Writing-plans and the task issue format fix the sections; i-have-adhd fixes the shape inside them: first line is the next action, multi-step work is numbered, lists stop at five, no preamble, no recap.
- You steer, you do not type.
  Talk to the product owner in outcomes, not mechanics; empty polls and no-change updates are not progress.
  Your final message stands alone: what finished, the stack, the cost, the decision needed.
- Every round-trip to the product owner stalls the project, so shape in one conversation (0b): start groundwork and the smoke check (0g) at once so toolchain findings join the brainstorm.
  Settle there every decision you can see coming (scoring, toolchain, dependency bumps, an unclear quality bar), never after a run.
  Keep groundwork running while the product owner answers.
  Ask mid-run only when the run cannot continue without the answer.
- The CLI's own config is yours to fix without asking: a wrong model id, preset slug, or typo in `~/.compose/config.json` `models`, `verify.json`, or `linear.json`.
  Fix it, re-run, mention it in your next report.
  Switching model or provider, keys, and anything that costs money are still the product owner's.
- Linear is the source of truth.
  What it records as decided is decided: read it before asking the user anything (0b), and write every new answer back to it.
  Nothing written in words is ever a file in the repo: no plan, task, ticket, design, decision, grilling note, or task list, not under `.compose/` and not anywhere else (the only files there are the CLI's JSON: `linear.json`, `verify.json`, and `results/`, plus the layers' PR text under `pr/` (step 4), the Lavish pages under `lavish/` (0b, step 8), and the Frame0 wireframes in `design.f0` (app data in `frame0/`), all ignored and never committed); everything a human would read as a ticket is an issue under the feature issue, its state kept true at every step and level (0s): a task is Todo when written, In Progress when dispatched, In Review when its layer passed, Done when merged, Canceled when dropped or re-split, and its story, feature, and epic roll up from it in the same step.
- Rule, do not stall: a judgment call that is not a product decision (which split, which `after:`, whether a finding is a defect, which of two equal libraries) is the team lead's, made in the wake that raised it and logged as one comment on the issue it touches.
  Only a choice the product owner must make (one they settled, scope, money, anything user-facing they have not seen) goes up, as one `needs-user` line with its recommendation (0h), never as a question buried in another report.
- After the first dispatch, a change to the approved design, plan, or a task brief is a change of course (step 8): opened with `change start`, closed with `change done`, never an untracked edit, so it can be undone.
  That includes step 7's re-split of an exhausted task and the product owner's edits to the task list in 3b.
- A `PAYMENT REQUIRED` stop is the user's, not yours.
  When `run` or `verify` exits 3 with that banner, stop everything and tell the user in one short message: which provider refused, its own words, the top-up link the banner printed, and that `compose resume --repo <path> --project <name>` continues exactly where it stopped (passed tasks keep their branches, remaining tasks run, a halted verify reruns).
  Never switch providers, re-dispatch, hand-implement, or retry around a billing stop.
  When the user says the account is fixed, run `resume`.
- A task branch changes at most 350 lines and never adds a document.
  The CLI enforces both; plan tasks that fit.
- Never push or submit a stack without the user's explicit permission for that push; `compose away --push` or `compose yolo --push` is that permission for the project's stack, given in advance; neither posture without --push ever pushes.
  A project whose `verify.json` has `"submit_after_verify": true` carries that go for good: a passed verify submits the stack with a plain push, never `--update` or `land`, and nobody asks.
  Without it, ask once per verified stack, in the `verify passed` line the watch gives you, and ask again only when something new would be pushed (a later verify PASS, tasks added); never repeat it in a later report.
  `compose land --yes` merges on GitHub only when the user said to land; neither posture covers it. `run` registers stacks locally only.
- While the product owner is away (`results/yolo.json`, `event away=on` or `event yolo=on`), decisions follow 0h's Away or Yolo paragraph: the recommendation, logged; the never-set still waits.
- Parallelize by default: before every step ask what else can start now.
  Whatever has no real dependency (tasks with no `after:`, test writers, groundwork, reviews, merges of independent branches, toolsmith fixes, file checks) runs at once, on as many agents as the machine and configured parallel limits allow.
  Wait only on a real dependency.
- Fresh session per task.
  Never chain tasks in one session.
- Keep the target repo's fixed context small: short AGENTS.md, no unneeded MCP servers, no duplicate skills.
- Every turn re-sends the whole session, and every Claude teammate compacts at 150k tokens, so spend turns and context on purpose.
  Look at an image once, downscaled or cropped first (`compose shrink <png>`); export or read it again only after it changed.
  Draw Frame0 in `compose frame0 draw` lists, a page per call, never shape by shape; reorder, delete, and lint pages with `compose frame0 move|delete|links|lint`.
  A harness subagent gets a named model (`model: "sonnet"` for searches and fixes), never the session's by inheritance.
- Build a trail graph in the repo (`trail build .`, git-ignored). The CLI prepends a `trail ask` context pack with exact `file:line` and source to every implementer and fixer prompt, and tells the model to use `trail ask` and `trail callers` instead of exploring. Without trail the fast model reads whole files.
- Always show timing and cost.
  Paste the CLI's `run start` and `run done` lines, per-task table, `verify` stage lines, and final `cost` line into your report.
