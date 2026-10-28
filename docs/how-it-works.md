# How it works

The loop turns a feature idea into one gh stack of small branches, each green against its own test and reviewed before verify checks the whole stack.
A frontier model plans and checks the work, cheap fast models write the code in parallel, and nothing is pushed.

## The loop

```
  TEAM LEAD (Claude, frontier)
  explore the code ─▶ design with you ─▶ plan in Linear ─▶ task issues + failing tests ─▶ your go
                                                                                        │
                                                                             compose run│
  ┌─────────────────────────────────────────────────────────────────────────────────────▼───┐
  │ DISPATCH   one clone + one pi session per task (GLM 5.3 Flash); `after:` tasks start    │
  │            on their base task's branch                                                  │
  │   test RED on HEAD? ─ no ─▶ skip                                                        │
  │        yes ─▶ implement (edits only) ─▶ CLI runs test, protected untouched, ≤350 lines  │
  │                 ▲                │                                                      │
  │                 └── RED: Haiku's fix brief to the session, ≤3, fallback after 2         │
  │                                  │ GREEN                                                │
  │                                  ▼                                                      │
  │                         format, commit on <project branch>-NNN                          │
  └──────────────────────────────────┬──────────────────────────────────────────────────────┘
                                     ▼
  TEAM LEAD REVIEW of each layer's diff ─▶ accept, or re-split and re-run
  passed branches ─▶ one gh stack, built in a CLI worktree (nothing pushed)
                                     │ compose verify
  ┌──────────────────────────────────▼──────────────────────────────────────────────────────┐
  │ 1 checks   lint + unit tests                                                            │
  │ then, in parallel:                                                                      │
  │ 2 review   reviewer, one call per perspective ─▶ confirm each finding (must quote code) │
  │ 3 jev      one MET / NOT_MET / UNCLEAR question per acceptance criterion                │
  │ 4 e2e      jev drives a real browser to the goal, then YOUR assert checks it            │
  │ 5 fix      confirmed findings ─▶ fix model, rerun; still failing ─▶ escalate            │
  └─────────────────────────────────────────────────────────────────────────────────────────┘
```

Eight roles split the decisions.

| Who | Decides |
|---|---|
| Team lead (frontier; Claude Code, pi or Codex) | spec, review of the planner's cut, tests, layer review, escalations; never writes code or merges |
| Planner (frontier teammate; Claude Code, pi or Codex) | the plan, the stories when a feature has none, and the task issues; never writes code |
| Designer (Claude teammate) | design stories and tasks in Figma, built by its own crew; never writes repo files or decides a product question |
| Fast implementer (GLM 5.3 Flash in pi; any model id) | makes one test green in one session; never writes or runs tests |
| Test reader (Claude Haiku) | reads a failed test run and writes the implementer's short fix brief; never edits or runs anything |
| reviewer (`models.review`) | raises review findings, then confirms or drops them |
| Jev (TypeSafe) | scores acceptance criteria, drives the browser, screens settled decisions against new evidence |
| CLI | red check, every test run, protected-file guard, timing, cost ledger |

A task that fails three attempts is re-split into smaller tasks and re-run, and the loop never falls back to typing its code by hand.
A provider that runs out of credit halts the run, and [workspaces.md](workspaces.md#billing-halts) explains billing halts.

## Finding the way

In a repo that already has code, an explore teammate maps it through trail first and adds a short codebase brief to the feature.
Questions and planning both wait for that brief.

The engineering manager shapes each feature with you in one conversation, one question at a time, and writes the design to the feature issue.
It starts with one open question about a real moment, and every message after it ends with one question that follows from your last answer.
As each part is understood it plays that part back, with its guesses marked, and you correct it.
A choice it makes for you is written under "My calls", never as something you agreed to.
Every decision you make is recorded at once with your words through `compose decide`, as a numbered line under the feature issue's "Decisions so far", so no decision is recorded that you did not make.
A builder check reads the design before you do.
It reports once, and the engineering manager brings you the few questions only you can answer in one message.
The design is approved only when you say so, and the plan starts after that.

A story or epic that needs a technical deep dive carries the Linear label `deep-dive`, which you can add or remove.
The engineering manager runs that deep dive with you the same way, and writes it as a Linear document titled `<issue id>: tech doc`, which a builder check reads like any design.
A question you would understand better by seeing it (a wireframe, a layout comparison, a diagram) is shown, on a Lavish page when it is a comparison.

A question about how a screen should look comes with a low-fi wireframe, drawn in Frame0 and shown to you.
Each project has its own Frame0 app on `.compose/<project>/design.f0`, so parallel sessions never draw into each other's window.
These commands manage it:

1. `compose frame0 up --project <name>` starts it.
2. `compose frame0 save --project <name>` writes the drawing to the file.
   `compose frame0 draw --project <name>` draws a whole page from one list of tool calls and saves it, so a page costs one agent turn rather than one per shape.
   `compose frame0 export --project <name> --page <page>` writes a page as a PNG, for a visual question or a designer's read.
3. `compose frame0 down --project <name>` saves and quits.
   `teardown` does this too.
4. `compose frame0 pages --project <name>` lists the pages with their shape counts (`--empty` shows only the empty ones).
   `move --page <page> --after <page>` (or `--first`) puts pages behind another, `delete --page <page>` or `delete --empty` removes pages and clears the links that pointed at them, and `links` checks every link between pages (`--retarget <old>=<new>` points them elsewhere, `--drop-dangling` clears those that lead nowhere).
   A page is named by its id, its name, or the start of its name, and `--dry-run` shows the changes without making them.
   A running app is changed through its own API, so it stays up and asks for no new permission; a closed file is edited directly.
   Either way the file is copied to `design.f0.bak-<time>` first (the newest five stay), and the saved drawing is checked against the plan afterwards.
5. `compose frame0 lint --project <name>` is the check before asking for a review.
   It fails on empty pages, pages out of flow order (a page name starts with its flow number, `05.3 ...`, and a journey's new page goes right behind that journey's pages, never at the end), and links to pages that were deleted, and it prints the command that fixes each.
   The designer draws a flow's pages only when it draws that flow, never a skeleton of every flow.

On the first drawing after each app start, Frame0 asks you to allow "Frame0 MCP Server".
Frame0 forgets its permissions when the app stops, so a session whose app was restarted asks again on its next drawing.
Sessions reach Frame0 only through `compose frame0` commands, so nothing of Frame0 runs until `frame0 up`, and no session carries a `frame0` MCP server that would start with it and idle.
If Frame0 does not answer `frame0 save` or `down`, a dialog is open in its window: answer it and run the command again.

## Planning

Once you approve the design, the team lead starts a planner teammate in its own tab instead of writing the plan itself.
The planner reads the feature's design and its Decisions so far, plus `DESIGN.md`, a PRD, or whatever the repo already had.
It writes the plan as a `## Plan` section in the feature issue's description, never as a file, and does not reopen a settled decision.
When the feature has no stories, it first proposes them as one list for you, one per user-visible slice with acceptance criteria, and creates them on your go.
Then it cuts the task issues under each story, and the team lead reviews that cut before anything is dispatched.

Handed a story, the planner writes that story's tasks only.
Handed a task, it lints and re-splits it, and that task is dispatched alone.
Like the team lead, the planner's tab is denied edits, so it plans and never writes code.

One product delivery plan in Linear sets the order across features, in parallel tracks; `compose delivery reconcile` makes Linear follow it and proposes anything destructive.

The factory (`/compose:factory`) runs in the background: it builds every ready story in delivery plan order at a day pace you set, pauses work a plan change rests on, and offers the next thing to plan while it builds, ahead into later waves.
At night (`/compose:factory night`) it keeps building inside the end time and dollar budget you set, answers questions on approved tickets with a one-time frontier answerer, prepares questions for your next planning session, and greets you with one morning digest.

You can also start it yourself from the engineering manager with `/compose:planner <issue or Linear project link>`.
Its model is the `planner` entry under `models.crew`, as [workspaces.md](workspaces.md#crew-models) describes.

## Design in Figma

The wireframes settle flow and content.
A designer teammate carries the design into Figma from wherever it stopped, as an entry into the same loop at the design step.
Start it with `/compose:designer <ticket> [--figma <file url or key>]`.
The team lead starts it for a feature or story whose project it runs, and the engineering manager starts it for an epic or when no team lead is live.

1. It reads before it writes: the ticket and its parents with their Decisions so far, the Frame0 wireframes, the repo's tokens and components through trail, `DESIGN.md`, and what the Figma file already has.
2. Before any screen story is built, it writes a visual direction for the surface as a Linear decision, with every rule citing research, a decision, or a measured value.
3. It files the design-system epic, design stories, and design tasks in Linear, never in the repo.
   Each design task names the Figma page and frames it may touch.
4. It starts one `design-NNN` builder per design task, each in its own tab, and gates each build with `compose figma tree` and `compose figma judge`: Jev judges each criterion on the node tree and on screens rendered from the same nodes.
5. A `design-review` teammate then checks one image per story, and the result is handed to the code tasks as Framelink data.

Every open design decision reaches you as a question with a recommendation, asked one at a time.
The designer and its crew are Claude sessions, because Figma serves writes only to its allowlisted clients.
Writes go through the official Figma connector, and every read goes through Framelink, which keeps reads off the connector's daily cap.
The plugin's `compose guard` hook enforces it in every teammate's session: a connector call other than `use_figma` and its `create_session` is denied and pointed at Framelink.
The designer requires Framelink; [requirements.md](requirements.md#for-the-designer) shows how to set it up, and the designer's start stops with the install steps when it is missing.
`compose figma check --project <name> --figma <file url or key>` stores the target file and prints the Framelink setup when it cannot read the file.
With no file stored, the designer asks you for one.
Before it plans anything it checks what your Figma plan allows (`compose figma check --pages --modes`, from one write probe on the stored file, since no read tells a plan): pages per file, modes per collection, and whether a library can be published.
The design system needs Light and Dark unless a recorded decision says otherwise (light only needs one mode), so a Starter plan blocks it only when two modes are needed, and that comes to you as one question, upgrade or design around it, before any task is written around the limit.
It asks you to publish a library only after `compose figma publish` shows a publish is pending, and once per review round.
Its model is the `designer` entry under `models.crew`, Opus 5.5 at high effort when the entry is missing.

## Linear

Work lives at five levels in Linear:

| Level | Is |
|---|---|
| project | the repo |
| epic | a short design, when the work is bigger than one feature |
| feature | one `compose` project and one stack; its description is the plan |
| story | one user-visible slice of the feature |
| task | one stack layer; see [tasks.md](tasks.md) |

You can hand `compose` a link to any of these levels, and it creates the missing ones.
It asks for the repo's Linear project link once and keeps it in `.compose/linear.json` in the repo's main checkout, which every worktree of the repo reads.
Each task moves to In Progress when it is dispatched, In Review when its layer passes, and Done when its PR merges.

## Cleaning up an existing project

Run `/compose:reconcile <Linear project link>` on a project whose tickets have piled up.
It researches the project, then sorts every open ticket into keep, cancel, or verify with you.
Nothing in Linear changes until you give your go in your own words.
A reconcile never deletes a ticket: every cancel carries its reason as a comment.
A cancel that merges a ticket into another also relates the two as duplicates.
A ticket that needs more shaping gets the `needs-shaping` label, and the ready check skips it.
When it is applied, each affected project's team lead hears what changed.
It never touches the order of tickets or what blocks what.

## Changing course

When the plan turns out to be wrong, the fleet notices and changes it in the open.

1. Evidence arrives when a task ends without passing, an implementer or teammate reports a contradiction with Linear, verify fails, or you say the plan changed.
2. Jev screens every line of the feature's Decisions so far against that evidence and marks each one HOLDS, BROKEN, or UNCLEAR.
3. The team lead holds only the tasks a flagged line touches, with `compose hold`, then confirms the flags.
   Everything else keeps running.
4. Complex or subjective calls, plus anything you settled yourself, reach you through the engineering manager, as text, or on a change page in Lavish when the evidence is visual.
5. The re-plan is one recorded change, started with `compose change start` and finished with `done`.
   The old decision is struck through and tagged, the new one sits beside it, and a comment on the feature says what changed and why.

To reverse a change, run `compose change undo C2`.
It puts back every issue and layer that change touched, and records the undo as a new change of its own.

## Projects

A project is one feature in its own worktree.

- `compose init --project <name>` creates branch `compose/<name>` from your current branch.
- The worktree is a [treehouse](https://github.com/kunchenguid/treehouse) slot from the repo's own git-ignored pool in `.treehouse/`.
  Without treehouse, it is `../compose-<name>`.
- `.compose/<name>/` sits beside it and holds the Linear link, the verify config, and results.
  `init` git-ignores it, `graft/`, and `.trail/` first.
- The layers stack on the review base, the branch the project started from (main, say), which `init` records as `review_base`, and `gh stack merge` lands them as one all-or-nothing merge, with no final PR.
- `init --after <other>` branches from another project's branch when the feature needs its code.

Your checkout is never touched, so several features can run at once.
A new request that continues an existing feature joins that feature's project.
Anything else gets a new project named for the feature.

## Task isolation

Each task runs in its own shared-object clone.
The prompt states the clone's absolute root and lists every editable file as an absolute path.
The absolute paths are there because project docs often name the original checkout, and models edit those instead.
The dispatcher reverts and flags any write into the main checkout.
Clones borrow the main checkout's git-ignored installs as symlinks, so `node_modules`, `vendor`, and `.venv` come along without a fresh install, and so do its git-ignored `.env` and `.env.*` files.
A `node_modules` that holds Vite, Vitest or Storybook is copied (copy-on-write where possible) instead of linked, because Vite will not serve a file whose real path is outside the clone.
A project whose build output depends on where `node_modules` lives sets `install` in verify.json, and each clone runs that command instead.

## Model choices

Since 2026-10-10, the default implementer is `pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast`, with no fallback.
With `models.fallback` set, a task gets `models.fallback_after` failed attempts (default 2) on its own model before the fallback takes the rest; a rate limit switches at once.
A provider too busy to start a turn counts as a rate limit: a 429, or a 5xx such as `overloaded_error`, before any token.
Without a fallback, such a turn gets a fresh session after 20 s, then 40 s, 60 s and 80 s, and spends no attempt.
A refusal that stays, such as a wrong model id, a missing preset, or a bad key, ends the task, or moves it to the fallback.
Implementers never run tests: an implementer waiting on a test run pays for the wait, and every later turn re-reads the output as input tokens.
The CLI runs the test after each turn, and on a failure the test reader (`models.test_reader`, default `claude:claude-haiku-5-5`) writes the implementer's fix brief.
One implementer turn may take 300 s; a turn that runs past it is cut, costs one attempt, and starts again in a fresh session.
An implementer reads only the files its context pack names, its task's files and its test, through a guard on every tool call of pi, OpenCode and Claude alike.
The Read, Grep and Glob tools work on those files.
In bash it may run `trail ask` and `trail callers`, and `wc`, `awk`, `cat`, `head`, `tail`, `sed -n 'a,bp'` and `grep` on those files, alone or ended by `| head` or `| tail`, which is the same view the Read tool gives.
Anything that writes, chains a second command, or names another file is refused, and each refusal is counted as `refused` in the task's result.
A team lead or teammate can run in Codex with a `codex:<model>` id, set by the `harnesses.<name>` block of the config.
[harnesses.md](harnesses.md) lists the harnesses and [config.md](config.md) the `harnesses.<name>` keys.

GLM 5.3 Flash earned that spot on a real seven-task project.
There, gpt-oss-120b on Cerebras missed the largest task four times and was rate-limited on two more, while GLM 5.3 Flash passed each on its first try in 9 to 20 s.

The default implementer runs in pi because pi was faster than OpenCode on the same host.
On that host, pi took 4.2 to 6.5 s per task at about 32K input tokens, and OpenCode took 8.5 to 11 s at about 100K.
OpenCode's fixed context is about 12K tokens per turn against pi's 1K, which accounts for the gap.
Both passed every task.

Reasoning has to be on.
With reasoning `none`, the model plans but never edits, and `low` fixed that at about twice the cost.

The reviewer is the config's `models.review`, such as `claude:claude-sonnet-5-5`.
Without it, the reviewer is whichever model `~/.compose/.env` names as `TEXT_MODEL`, which defaults to Mercury 2.5.
You set the planner and teammate models per role in the config, as [workspaces.md](workspaces.md#crew-models) describes.

These figures come from small benchmarks and one prototype.
Every real run prints its own time and cost, and those are the ones to trust.
