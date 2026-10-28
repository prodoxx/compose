# Verify

Verify gives you confidence that a stack works before anyone reviews it or pushes it.
It runs on your machine, pushes nothing, and makes no network calls except to model APIs.

Run it with `compose verify --project <name>`.

## Config

Verify reads `.compose/<project>/verify.json`.

```json
{
  "checks": ["uv run ruff check .", "node --test 'tests/**/*.test.js'"],
  "docs": ["README.md", "DESIGN.md"],
  "perspectives": ["correctness", "security", "spec", "docs"],
  "jev_acceptance": true, "acceptance_threshold": 0.6, "acceptance_gate": false,
  "e2e_setup": "python -m server.server --port $E2E_PORT &", "e2e_setup_wait": 3, "e2e_env": {"API_KEY": "..."},
  "e2e": [{"name": "signup", "url": "http://127.0.0.1:$E2E_PORT/", "goal": "Sign up as a new user.",
           "assert": "python .compose/<project>/e2e/check_signup.py"}],
  "review": true, "review_base": "main",
  "review_context": "Design facts reviewers must respect."
}
```

`commit_convention` (`{"types": [...], "example": "feat: ..."}`) makes every commit the CLI makes, and every teammate brief, start with a type prefix: tests are `test`, docs `docs`, dependencies and config `chore` or `build`, fixes `fix`, the rest `feat`.
`checks` also gate `compose stack --submit`: each layer is checked on its own tree, bottom up, and a failing layer refuses the submit.
The top layer runs every check and the `test` of the tasks it is the lowest layer for.
A task is tested on the lowest layer that holds its code and every test commit written for it.
That is the layer with its code, unless a test appended to a shared file had to follow an earlier test into a higher layer: then the task is tested there, where its test exists.
A lower layer runs the `test` of the tasks it is the lowest layer for and the cheap checks, the ones that are not a whole test suite, so the full suite runs once, on the stack as a whole.
A lower layer that does not define a check's package script yet (bun, npm, pnpm or yarn says the script is missing) skips that check with a `layers later` line, so groundwork below a tool's setup passes when its toolchain task could not go under it; the top runs every check, so a script no layer adds still refuses the submit.
A green result is kept by the git tree sha it ran on and its command, in `results/green/`: a check, the full suite or a task's `test`.
When the same command is asked for on the same tree, verify, the run's pass gate and the layer gate take the result and print `<check> green on tree <sha12> at <time>, not rerun` without running it.
A red or unknown result is never kept or reused, and a layer whose tree differs from every green one runs as before.
`--fresh` on `verify` and `stack --submit` runs everything again.
After a rebase or a stack rebuild, run one full suite on the top of the stack and the final verify, never one per layer.
A failing test that fails on the stack's base too, in a test or module the stack does not change, was red before this work: the submit goes on and the test is listed as accepted.
A test that passes on the base, or that the stack touches, refuses the submit, as does a failure that names no test or a base run that timed out.
A base run that timed out or names no failing test is never taken for red: the refusal names the failing tests and says they cannot be told from new ones.
In `compose run`, after each attempt whose own test passes, the checks that are not a whole suite and the `test` of every task whose `files` or `protected` the attempt changed run on the task's tree (`task_checks` in verify.json, a list of its own, replaces `checks` as the source of those checks, so a check only verify should run can stay under `checks`), and a check that passes there is never run on the base.
Only when a check fails after the task does `run` take the checks on the task's base, in a scratch clone of it, once per base commit; a base run that timed out is not kept.
Each check prints a `<id> checks  `<command>` after the task, up to <N>s` or `on its base` line before it starts, and a `timed out` line past the limit, so a long check never leaves the log silent.
When that base run is unknown, a failing test outside the task's reach is run on the base tree, and it is accepted only when it fails there too.
A task whose base run is unknown there as well is rejected as a regression, with a line naming the test and the reason, and gets no further attempt.
`land_method` (`squash`, `merge`, or `rebase`; default `squash`) is how `compose land` merges each PR.
`review_base` is the branch the project's work is reviewed and stacked against: `init` records the branch the project started from.
One that names no branch (a project made before init recorded it holds `main`, also in a repository whose trunk is `master`) reads as the repository's default branch.
`fix_model` overrides the fix model, which defaults to the implementer.
`database` records the database targets the project started on, one entry per git-ignored env file and variable (`{".env DATABASE_URL": "localhost:5433/dtt"}`: host, port and name, never the credentials); `init` writes it, or the first `run` or `verify` of an older project.
A start stops while a target differs from the record or an env file sets one the record lacks, until the user confirms the new target and the entry is updated, so no run touches a database the user did not confirm.
`env_file` replaces `~/.compose/.env` for this project.

## Stages

1. **checks** runs lint, unit tests, and smoke checks.
   These are exact and cheap, so they run first.
   A fix round runs only the `test:` commands of the tasks whose files its fix changed, and the full checks once, after every other stage passed.
   A fix that changes a file no task names runs the full checks.
2. **review** makes one fast-model review per perspective.
   Each high or medium finding then gets a confirmation call that must quote the wrong line and say how it must read.
   A quote that is not in the code, or a line that must read as it already does, drops the finding, and only confirmed highs fail.
   The reply is read from the first object that holds the verdict, whatever prose or fence surrounds it, and asked once more when it has none.
   A claim whose reply is still unusable is shown as `unjudged` and dropped, and it does not fail the stage.
3. **jev** asks one MET / NOT_MET / UNCLEAR question per acceptance criterion.
   Failures above the threshold become findings.
   Then jev judges whether each task's evidence proves each criterion ([Evidence](#evidence)); that never changes the verdict.
4. **e2e** has jev drive a real browser to each case's goal.
   Then the case's `assert` checks the real DOM or log.
5. **fix** sends confirmed findings to the fix model.
   Protected files are reverted, and the stages rerun for up to `--fix` rounds before they escalate.
   A round is discarded when it answers no reproduced failure: it must change a failing check, or remove a line a confirmed finding cites.
   A round that answers a confirmed finding by adding code, with no failing check before it, is kept when it changes a file the finding names and its own new test fails without the rest of the fix and passes with it.
   A fix commit holds only the files the fixer changed: never the agent wiring (`.claude/settings.json`), which a session hook rewrites, nor files a check left behind.
   Uncommitted changes of your own skip the fix, so a bad fix can always be reverted.
   A verify passes only once the full checks pass on the final code, so the fixes' scoped tests never hide a regression.

Review, jev, and e2e run in parallel after checks.
Every run ends with time and cost per provider.

## Parts of the pipeline

Three flags run one part of the pipeline at the point in the work where it pays off.
None of them fixes anything or reconciles Linear, and none writes `results/verify.json`.

`compose verify --project <name> --prove` runs before `run`, once the acceptance tests are collected.
It runs every e2e case headless against the project branch as it is before `run` (trunk, the groundwork stubs and the tests, with each case's own setup and seed), once and without the retry.
Each case must fail, and only because the feature is missing.
A failure is classified from jev's stop reason, last steps, and final page: rules first (the page did not load, the user landed on a login page, jev skipped the password field, the assert script crashed, the harness failed, then the assert's own `ASSERT FAILED` line, which is "feature missing"), then a fast model that reads the rest and must find the app working up to the point the feature should appear.
A case whose assert prints that line gets the same class on every prove, whatever jev did on the way; the model only judges cases without it.
A case that fails for the environment (page did not load, login failed, a blocking overlay such as a cookie banner or tour, seed missing, wrong path), one the model cannot tell apart from that, and one that already passes all fail the step with the reason.
Prove refuses once the stack holds the feature.
After a re-plan, `--prove --only <ids>` (the tasks to redo) runs the cases on the stack cut below the first of those tasks: the layers under them are the app, and the redone work is what the cases must find missing.
It records `results/prove.json`.

Prove also lints the cases for the real flag path.
Every variant of a flag or experiment needs at least one case that reaches it through the real assignment (a seeded user, the flag service).
A page whose cases all carry a URL override (a query or fragment key such as `variant`, `experiment`, `flag`, `force`, `bucket`, or a whole-word `ab`, `ff`, `exp`) is reported, and so fails the step.
One case on the same path without an override covers it.
A case that must force the variant says why in `"url_override": "<reason>"`; an empty reason is itself reported.

`compose verify --project <name> --review-only` runs right after `run`.
It runs the review and the jev acceptance, with no checks, e2e, or fix rounds, and records `results/review.json`.
Findings then arrive while the stack is fresh, as tasks to fix, instead of after the whole pipeline.

`compose verify --project <name> --prototype` runs right after `run` too.
`verify.json` lists the screens that must match the prototype, one entry each:

```json
"prototype": [{"name": "settings", "url": "http://127.0.0.1:$E2E_PORT/settings", "goal": "Open the settings page.",
               "reference": "Frame0 page Settings", "must_match": ["icons", "empty state copy", "row spacing"]}]
```

It drives jev to each screen of the built app at the top of the stack and keeps the screenshots in `results/prototype/e2e/`.
It writes one report, `results/prototype.md`: per screen, the reference, the capture, and a `- [ ]` line for each `must_match`.
The reader opens both images and ticks the lines, and a mismatch becomes a task before the first full verify.
It fails only when a screen cannot be reached; whether it matches is for the reader to decide.

## Evidence

Every task issue on Linear has an `## Evidence` section that only the CLI writes.
It is the table of the evidence that counts for the task: a `Verdict:` line with each criterion's latest jev answer (or `not judged`), then the 60 newest rows.
The CLI rebuilds it from the issue's comments after each write, so a hand edit is lost.
`compose sync` leaves it out of the task's brief.
With no Linear key, or on a task with no issue, nothing is recorded.

Each proof is one comment on the issue.
Its first line is `Evidence: <source> <phases>`, and a fenced `evidence` block holds one JSON row per line:

````
Evidence: run red, attempt, final

```evidence
{"event_id": "3f9a1c0b7d42", "run_id": "a1b2c3d4e5f6", "commit_sha": "9c1e0f...", "task_ids": ["003"], "criterion_id": ["003.1:5d41402a"], "phase": "red", "source": "run", "result": "fail_expected", "fails_on_base": true, "detail": "test fails before the change"}
```
````

The comments are the record.
A row saved twice is read once.
A write that timed out may have landed anyway, so before every retry the CLI reads the issue's comments and writes only the rows that are not there yet.
If that read fails too, it writes anyway, and the reader still keeps one row per `event_id`.

| Key | Holds |
|---|---|
| `event_id` | The first 12 hex of the sha256 of the other keys |
| `run_id` | The task's run id; empty on a row about the whole feature |
| `commit_sha` | The commit the row was recorded on; empty when it has none |
| `task_ids` | The tasks the row covers; empty when it covers none in particular |
| `criterion_id` | `<task id>.<order>:<first 8 hex of the sha256 of the criterion text>` for each criterion covered; empty means all of the task's criteria |
| `phase` | `red`, `attempt`, `final`, `case`, `checks`, `scoped`, `e2e`, `review`, `acceptance`, `sufficiency`, `done`, or `cost` |
| `source` | `run`, `prove`, `verify`, or `done` |
| `result` | `pass`, `fail`, `fail_expected`, `error`, `skipped`, `unclear`, `proven`, `unproven`, or `contradicted` |
| `fails_on_base` | `true` for a test that must fail before the change, `false` for one that passes, else `null` |
| `detail` | One line, scrubbed of secrets, at most 160 characters |

A `fail_expected` is a test that failed as it should before the change (the red check, or a prove case that fails because the feature is missing); any other failure is `fail`.
An `error` is a tool error or a jev that gave up: it is never a `pass`, and never counts as proof.
`skipped` is a stage that did not run, and `unclear` is a criterion jev could not call.
`proven`, `unproven`, and `contradicted` belong to `sufficiency` rows, which also carry `cites` (event ids) and `needs`.
`cost` is informational: it is never evidence for a criterion.

### Who writes rows

| Writer | Rows | On |
|---|---|---|
| `run` | `red`, `attempt` (one per test run), and `final`, when a task finishes | the task issue |
| `verify --prove` | `case`, one per e2e case | the feature issue |
| `verify` | `checks`, `e2e`, and `review` | the feature issue |
| `verify` | `scoped` and `acceptance` (one per criterion), then `sufficiency` | each task issue |
| `compose done` | `done` | the task issue |

### What counts for a task

A row counts for a task when all of these hold:

- Its run id is the task's current one.
  A row on the feature issue that names the task, or no task, counts by revision alone.
- Its revision is current: the commit is the task branch's tip, or the task's `files` are unchanged between that commit and the tip, so a rebase alone voids nothing.
  A `fails_on_base` row counts while the task's `protected` files are unchanged since its commit.
  A row with no commit counts by run id alone.
- Its criteria are current.
  Editing a criterion changes its id, so rows that named only the old text stop counting.

`run --redo` mints a new run id for every task it dispatches, so earlier rows leave the table.
No comment is deleted.
A `resume` keeps the run id.

### When Linear does not take a row

A row Linear does not take is tried 3 times, 2 seconds apart, then kept in memory with one line naming it.
The run goes on, and the next write for that issue sends the waiting rows first.
Rows are never written to a file: if the process dies first, repeat the verification.

`verify` tries the waiting rows once more before it decides.
If any is still unsaved it never passes: it prints `verify FAIL  evidence not saved on Linear: <ids>` and exits 1.
`compose done` marks nothing done while its `done` event is unsaved: it prints `<tid> not done: its done event is not saved on Linear; run compose done again`, tries every other id, and exits 1.

### Jev sufficiency

After the acceptance stage of each round, jev reads the rows that count for each task and answers per criterion: `proven`, `unproven`, or `contradicted`.
Each answer is a `sufficiency` row on the task issue, citing the rows jev ties to the criterion in `cites`.
`needs` lists what further verification would help, from `test`, `prove`, `check`, `e2e`, `review`, `acceptance`, `manual`, and `none`; anything else is read as `manual`.
jev reads at most the 30 newest rows per task.
It runs unless `--skip-review` is set or `jev_acceptance` is `false`.

A `proven` answer is stored as `unproven` when it cites no row, when it cites only `error` rows, or when a counted `attempt`, `final`, `checks`, `scoped`, or `e2e` row failed; those failed rows are then added to `cites`.
A missing or unknown answer is `unproven`.
Sufficiency never changes verify's pass or fail, whatever `acceptance_gate` says.

### Linear endpoint

`LINEAR_URL` replaces the Linear GraphQL endpoint (`https://api.linear.app/graphql`) for the CLI's Linear calls, except the key check of `doctor` and the preflight.
An e2e case points it at a fake server on `$E2E_PORT` to drive `run` with nothing written on Linear.

## e2e rules

- Each stage gets fresh ports `$E2E_PORT` and `$E2E_CLIENT_PORT`.
  Verify expands them in every value of a case (`url`, `goal`, its `env`) and in `e2e_env`, and sets them for `e2e_setup`, `setup`, `driver`, and `assert`.
  Never hardcode a port.
- Start servers in `e2e_setup` with a plain `&`.
- The cases start once every port `e2e_setup` names (`$E2E_PORT`, `$E2E_CLIENT_PORT`) accepts a connection, waiting up to 60 s, or `e2e_setup_wait` if longer.
  A setup that names neither, such as a browser of your own on a pinned port, waits a fixed `e2e_setup_wait` seconds.
  A case's own `setup` works the same way with `setup_wait`.
- A case's own `setup`, for a jev case as for a driver case, starts before that case and ends after its `assert`.
  Use it for what one case needs alone, such as logging in a different user.
- Keep driver and assert scripts under `.compose/<project>/e2e/`.
- Each stage launches its own Chromium with a throwaway profile, so verify never touches your real Chrome.
  The browser is headless, because an unattended verify runs while the display sleeps, and a headed Chromium then stops painting.
  Set `"e2e_headed": true` to watch the browser; on macOS the whole verify keeps the Mac awake with `caffeinate -dims -w <pid>`, headless or not, so the machine does not sleep under it.
- When the stage ends, it stops everything it started.
  That covers setup processes, its browser and that browser's harness daemons, and anything listening on its ports.
  A SIGTERM or SIGHUP to verify ends them too.
  A verify killed outright ends nothing, so the next stage ends the browser of any verify that is gone.
- When verify runs at the stack top in a slot under the main checkout, every absolute path in `checks` and the e2e config that points into the project worktree or the main checkout is moved to that slot, once.

## jev cases

A case without a `driver` runs jev to its `goal`, then runs its `assert`.
The `assert` runs after every jev run, also one that gave up, crashed, or hit its limit, so an assert that resets the app for the next case always runs.
It runs while jev's tab is still open, so it can read the page jev left over `BU_CDP_URL`; jev closes the tab once it ends.

An assert checks that the app is in the state the case needs first (signed in, no banner or overlay, seed present).
When it is and the feature's part is absent, it prints a line starting `ASSERT FAILED` (then one line per missing thing) and exits non-zero; `prove` reads that as "feature missing" without asking a model.
When the app is not in that state, or the script itself broke, it exits non-zero without that line, so the environment rules and the model decide.

jev never sends a password to a model.
To log in through a form, name the field's value in `secrets`, keyed by the field's label, and keep the value in the environment, for example in `e2e_env` or `~/.compose/.env`:

```json
{"name": "billing", "url": "http://127.0.0.1:$E2E_PORT/login", "goal": "Log in as e2e@example.com and open Billing.",
 "secrets": {"Password": "$E2E_PASSWORD"}, "assert": "python .compose/<project>/e2e/check_billing.py"}
```

jev types the value itself, so it never reaches a model, jev's history, its result, or verify's output.
A literal value is refused; name an environment variable.
Without a matching secret, jev skips the password field, and its stop reason names the field it skipped.

A jev run that does not reach its goal ends `blocked` (or `error`, for a crash) and reports `TOOL ERROR`.
The `e2e` line then names jev's stop reason, and the next lines show the page it stopped on and its last steps.
`results/verify.json` keeps every step of the run, and `results/e2e/<n>-<case>/` keeps a screenshot of each step of a case that did not pass.

A failing check keeps its whole output in `results/checks/` (named on its `check` line and as `output_file` in `results/verify.json`, whose `output` is only the last 3000 characters).
The `check` line in the verify log also shows the last 20 lines of that output.
`check_artifacts` in verify.json lists folders or files, relative to the repo (for example `e2e/test-results`), that a failing check's tool writes; each is copied beside the saved output into `results/checks/<name>-artifacts/`, so the next round's run does not wipe it.
The reflect and every fix round read the check's failing tests (unittest's `FAIL:` and `ERROR:` lines), then the end of the output, then that path.

When the harness itself fails, for example a jev or browser crash, a missing tool, or a port in use, verify reports `TOOL ERROR` and retries once.
It never sends that failure to a fix round.
A review whose confirmation call fails, with no confirmed high, is the same: the `review` line says `TOOL ERROR` and names the error, and `FAIL` stays for a confirmed high.

A case with a `driver` command runs the project's own browser driver instead of jev.
That driver still gets the stage's browser in `BU_CDP_URL`.
When no case needs a browser, for example drivers of a terminal app, set `"e2e_browser": false`: the stage then launches none and sets no `BU_CDP_URL`.

## A browser of your own

When the stage's own browser is not enough, for example a page that needs a browser extension loaded, launch that browser in `e2e_setup` and point the cases at it with `e2e_env`.

```json
{
  "e2e_setup": "BP_PORT=9333 bp launch about:blank --ext path/to/dist --profile .compose/<project>/bp-profile",
  "e2e_setup_wait": 4,
  "e2e_env": {"BU_CDP_URL": "http://127.0.0.1:9333", "BU_NAME": "bp9333"}
}
```

Drivers and asserts get `BP_PORT` set to that browser's port, so a plain `bp` call reaches it.

## jev

`jev/` is the browser agent the e2e stage drives.
It came from [jev-ultrafast](https://github.com/browser-use/jev-ultrafast) at `05b841f`, and it holds the library (`jev_ultrafast/`) and its tests, nothing else.
verify and `bp goal` both run it as `python -m jev_ultrafast <url> <goal>`, which ends with a `JEV_RESULT` line and exits 0 only when the goal is done.

- It is its own uv project (Python 3.12+, `browser-harness`, `httpx`), so `bin/compose` stays standard library only.
- The CLI runs it with `uv run` from `jev/` and passes `~/.compose/.env` when it exists.
- You run its tests with `cd jev && uv run pytest`.

jev-ultrafast keeps the demo CLI, its web UI, the examples, and the scripts.
