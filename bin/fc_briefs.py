import argparse, json, re, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import fc_core, fc_config, fc_claude, fc_tasks, fc_herdr, fc_worktree, fc_change, fc_crew, fc_toolbug, fc_testred


# ---------- groundwork: the boilerplate brief, then one test-writing teammate per task on a shared contract ----------

def detect_stacks(wt):
    """One line per toolchain the probe must cover, from the manifests at the root and one level down."""
    found = []
    for d in [wt] + sorted(c for c in wt.iterdir() if c.is_dir() and not c.name.startswith(".") and c.name != "node_modules"):
        rel = "." if d == wt else d.name
        if (d / "go.mod").exists():
            found.append(f"- Go module in {rel}: go mod download, go build ./..., go vet ./..., go test ./..., gofmt -l, and any codegen it uses")
        if (d / "package.json").exists():
            pm = ("pnpm" if (d / "pnpm-lock.yaml").exists() else "yarn (npx yarn@1 when yarn is not on PATH)" if (d / "yarn.lock").exists()
                  else "npm ci" if (d / "package-lock.json").exists() else "the package manager its README names")
            found.append(f"- Node package in {rel} with {pm}: install from the lockfile, its test, build, and start scripts")
        if (d / "pyproject.toml").exists() or (d / "requirements.txt").exists():
            found.append(f"- Python in {rel}: a venv, install, the test command, and how it starts")
        if (d / "Cargo.toml").exists():
            found.append(f"- Rust crate in {rel}: cargo build, cargo test")
    return "\n".join(found) or "- no manifest found: read the README for how it builds and starts"

GROUNDWORK_BRIEF = """You are the groundwork teammate for project {slug}. Worktree: {wt}, branch {trunk}. The task briefs are the contract:
read them first with `compose tasks --repo {wt} --project {slug}`. Use `trail ask "<question>" --source` for code context.
Commit on {trunk} in small commits with plain messages. Nothing is pushed.

Phase A, toolchain (probe and report; you never change dependencies):
{stacks}
1. Install from the lockfiles, then build, test, and start every part the way a user would (start the server and probe one URL; start the client with BROWSER=none and probe it). Unit tests passing is not proof the app runs.
2. For each breakage find the smallest fix that works (a documented setting, or an upgrade); try it on a scratch copy if needed, and leave the worktree's dependencies and lockfiles as they are.
3. Report `working "toolchain: <what breaks>; recommend <fix>, alternative <alt>"` and wait for the team lead's go in this pane.

Phase B, after the go (the settled choices reach the planner as tasks):
1. Dependency, lockfile, and toolchain changes are implementer tasks, never yours: the planner splits them like any other work, and a choice the probe found is a wayfinder card the product owner settles first. Change no dependency.
2. Write the schema, codegen, wiring, and stubs the task briefs need, with the exact signatures the briefs give: stubs compile and return "not implemented" errors, zero values, or null. No feature logic.
3. verify.json ({verify}, keep existing keys): `checks` = every smoke command that passed (build, vet, unit tests, formatter check, client build). For anything with a UI or server: `e2e_setup` starting the server on $E2E_PORT and the client on $E2E_CLIENT_PORT (the CLI hands out free ports and stops what the setup starts; never hardcode a port the app does not pin, never free ports with lsof or kill), `e2e_setup_wait`, and one `e2e` case per user flow with a goal and an `assert` script that checks the real log or DOM. Driver and assert scripts go in {e2e_dir}, never in ~/.compose or /tmp. Run the checks with `compose verify --project {slug} --skip-review --skip-e2e --fix 0` (no task sync needed), then prove the e2e cases with `compose verify --project {slug} --prove`: each must fail only because the feature is missing, and `run` refuses until that holds, so fix every case, seed, or setup it names. Run both directly: under `compose timed` jev has no TypeSafe key.
4. Commit.

Phase C, the contract (you write no tests: one teammate per task writes them in parallel from your contract):
Write {contract} (outside the repo; never commit it) with exactly what a test writer needs and must not guess:
- every type, struct, and generated model a test touches, with its package and whether it is a value or a pointer
- every stub's exact signature, and which task implements it
- the test helpers and fixtures to use (an in-memory DB, how to fake the client or network, a provider wrapper), with a short example
- per task: the test file path (its `protected:`), the test function or describe names its `test:` command selects, and that command
Signatures and exact lines are copied from the briefs, never added: implementers see the brief and the code, never the contract.
A test that would need a value, line, or rule no brief states is `blocked "<task>: brief lacks <what>"`, for the planner to add.
Report `working "stubs ready: <commit>; contract at {contract}"`. Report `done` only when the team lead says so.
"""

def cmd_groundwork(a):
    """Print the groundwork brief for this project, plus the team lead's project notes from --extra (or stdin with `-`)."""
    proj = fc_core.project_dir(a); wt = fc_herdr.project_worktree(proj)
    extra = sys.stdin.read() if a.extra == "-" else (a.extra or "")
    print(GROUNDWORK_BRIEF.format(slug=proj.name, wt=wt, trunk=fc_core.current_branch(wt), stacks=detect_stacks(wt),
                                  verify=proj / "verify.json", contract=proj / "results" / "contract.md", e2e_dir=proj / "e2e")
          + (f"\nProject notes from the team lead:\n{extra.strip()}\n" if extra.strip() else ""))

WAYFINDER_BRIEF = """You are the wayfinder teammate for project {slug}. Worktree: {wt}. Read SKILL.md and sections/0b.md first.
The map is the feature issue {feature} on Linear:
its description holds Destination, Notes, Decisions so far, Not yet specified, Out of scope. It is the one decision record.
1. Read the map, the spec or README named below, and the repo (`trail ask "<question>" --source`). Before asking anything, look
   for the answer in Linear: the map's Decisions so far and Notes, the feature's and its epic's comments, and a
   `compose linear list --search "<terms>"` search in the project for the question's key terms. A decision found there is
   settled: do not ask it again.
2. Chart the map body on the feature issue (`compose linear update <id> --section "## <heading>" --file <path>`), then write every
   open question as a card: the question, why it matters, the options with one line each, and your recommended option marked.
   Never choose for the product owner: every card
   carries a recommendation, the product owner answers. Decide the channel per card with one test: are the options pictures, or words?
   Words are text (the default): requirements, conceptual choices, tradeoffs, scope, toolchain; "where do the tokens live?" is text although tokens are UI.
   Pictures are visual: a Frame0 wireframe, a layout or flow comparison, a diagram, side-by-side designs; "which of these three onboarding layouts?" is visual.
   A visual card is never asked as text, and a text card is never put on the page. Visual cards go on one page: {page}
   For that page read `npx -y lavish-axi playbook input` and `npx -y lavish-axi design` first (the project's own design system if
   it has one, else the Lavish-recommended Tailwind and DaisyUI CDN). Set data-lavish-annotate="off" on <html>. Each card is a
   form with data-lavish-question, the recommended option preselected, a free-text box, and one answer button; the top of the page
   has one "accept every recommendation" button. Each sends exactly one window.lavish.queuePrompt with text "<question>: <answer>"
   and data {{question, answer}}; accept-all sends data {{question: "accept-all", answer: [{{question, answer}}, ...]}} with every
   card's recommendation.
3. Write every text card in full (question, why it matters, the options, recommendation) under Not yet specified on the map
   first, with a card id per card, never reusing an id; visual cards go on the page instead. Then ask once, all open cards in one
   line: open the page if there is one (`npx -y lavish-axi {page}`), and report, before any poll and whatever the CLI's next_step
   text says, one line:
   `compose report --project {slug} --as {name} needs-user "<issue>: <n> cards <ids>; recommendation <one line>; 'visual
   cards open in Lavish: <url>' when the page is open>"`, where <url> is the `url:` that `lavish-axi {page}` printed, copied and never
   retyped, in no second host form. `compose report` refuses a page without that link, a link that does not answer, and a card
   asked already and not answered (a card in Decisions so far is never asked), and it adds that the product owner may answer in
   text. The team lead forwards the line unchanged; the engineering manager reads the issue once and asks the product owner
   every card in full; answers travel by card id.
4. Wait. Text answers come back as a prompt in this pane, which a Bash call blocked in a poll cannot receive: poll in rounds,
   `npx -y lavish-axi poll {page} --owner {name} --takeover` in the foreground with the Bash call's timeout set to 180000 ms, and
   when a round ends with nothing run it again at once (a killed poll loses nothing: queued answers wait), so a forwarded text
   answer reaches you within three minutes. Never background it, never `end` it or `--reopen` a session the product owner ended.
   Never loop or poll `compose linear` while you wait (the shared hourly Linear budget): read the issue once, and the herdr prompt brings the answer.
   Answers arrive in the poll's `text` column (and as JSON after "Context data:" inside `prompt`), not as a data field.
   As soon as answers arrive, by poll or by text, acknowledge them on the page: `npx -y lavish-axi reply {page} --agent-reply
   "<card ids> received"`; the product owner sees their answers landed. If a poll returns `browser_disconnected`, the window
   closed but the session lives: run `npx -y lavish-axi {page}` once for its `url:` (it resumes the session), report once
   `needs-user "reminder: <issue>: <n> cards <ids>; ...; visual cards open in Lavish: <url>"` (the answers so far are
   acknowledged first), and poll again.
5. Record each answer in the map's Decisions so far as it arrives: one line each, starting with its card id and ending `(product owner, <date>)`.
   An answer that starts `away:` or `yolo:` is the engineering manager's recommendation taken while the product owner is away, never their answer:
   end its line `(away recommendation, unconfirmed, <date>)` or `(yolo recommendation, unconfirmed, <date>)`, the one its prefix names,
   and change that tag to `(product owner, <date>)` only when they confirm it. Read the section with `compose linear get <id> --body`, then write the whole section back, its lines plus the new one, with `compose linear update <id> --section "## Decisions so far" --file <path> --expect <hash>`, the hash `get --body` listed for that section; if it says the section changed since you read it, read it again and keep what changed; never a whole-description write.
6. New questions the answers open, and the groundwork teammate's toolchain findings when the team lead forwards them (which version, which option),
   are asked the same way, and the planner cuts what the product owner settles into implementer tasks: text cards in a new needs-user; visual cards added to the same page, then
   `npx -y lavish-axi poll {page} --agent-reply "<what changed>"` and wait again.
7. When every card is answered and Not yet specified is empty: `npx -y lavish-axi end {page}` if the page was opened, then report
   `done` with the decisions in one line each.
"""

PLANNER_BRIEF = """You are the planner teammate for project {slug}. Worktree: {wt}. Read SKILL.md and sections/1.md, 2.md, 3.md, 3b.md first.
Your ticket is {issue} on Linear; you are {name}.
You continue specing from where it stopped: the plan, the task issues, or a re-cut. You write Linear and the CLI's JSON only, never
code. You never run `compose run`: you never dispatch. You cut only for this ticket's feature, never for the whole project.
Every teammate you start runs in its own pane (`compose crew start`); never do its job inline.

1. Your level sets the job (read it from the ticket's label, else its parent chain):
   - Project link: list its epics and features with their story and task counts, and ask which one to continue (needs-user).
   - Epic: one planner per feature, in parallel. Fog (its features cannot be named yet) is wayfinder's: hand it to 0b and stop. Link a feature blocked by another feature only when it needs that feature's code.
   - Feature: check the map is settled (Not yet specified empty), the `### Codebase brief` is in its Notes, and it has a `## Plan`.
     No plan: write `## Plan` in the feature description with `compose linear update <id> --section "## Plan" --file <path>`:
     numbered steps, each with the files it touches, the test that proves it, and its dependencies; decisions already made are
     not reopened. Stories missing: draft the proposal on the map, in ship order, with a card id per story. A story is the
     thinnest slice that works end to end and unblocks the next story, with at most 5 criteria. Split cues: "all", "every",
     several When/Then pairs, several outside systems. When no split fits, a time-boxed probe answers the unknown first,
     then the split. Name the stories in one `needs-user "<issue>: <n> cards <ids>; recommendation <one line>"` line, and
     create them in Linear with `compose linear create` only on the product owner's go. Then check and cut each story.
   - Story: check and cut this story only; other stories' tasks are not touched.
   - Task: lint it against the format and the cutting rules below; re-split it if it fails.
   - A loose idea is not the planner's: say so and point at the compose skill's step 0.

2. The spec check, per story, in the order the gaps are cheapest to close:
   1. A Feature parent with a settled map: Destination, Decisions so far, Not yet specified empty, no open questions left.
   2. `### Codebase brief` in the feature's Notes; without it no file may be named.
   3. Acceptance criteria present, each a behavior a test can check, each traceable to a decision line or the spec's words.
   4. Every criterion maps to a file or symbol via `trail ask` or `trail callers` in the worktree; new files are listed as new.
   5. The test runner is known (verify.json `checks`, else the codebase brief's test setup), and a test path per task for `protected:`.
   6. No open how: every "how" is stated or obvious from the named files; events are named; rules link to their decision.
   7. The groundwork contract (contract.md) is not needed to cut: it is for the test writers, and implementers never see it.
      Name the test paths, and state in the brief every signature, export name, and exact line its test asserts; never point
      the brief at the contract.
      A test that pins a line width or exact text (a wrapped message, a limit on characters per line) gets that text, already
      wrapped as the test expects, in the brief, line by line: a model cannot count columns, and a task that misses its width fails every attempt.
   A Decisions line ending `(away recommendation, unconfirmed, <date>)` or `(yolo recommendation, unconfirmed, <date>)` is a recommendation
   the product owner has not confirmed:
   a story or task cut on one says so in its description (`Built on unconfirmed: <card ids>`) and in your report line, never silently.
   Checks 1, 3, 6 are decision gaps: each is a text card written in full (the question, why it matters, the options, your
   recommendation) under Not yet specified on the map first, with a card id per card, never reused; then report one line per issue,
   every open card in it (`compose report` refuses a card asked already and not answered):
   `compose report --project {slug} --as {name} needs-user "<issue>: <n> cards <ids>; recommendation <one line>"`.
   The team lead forwards the line unchanged; the engineering manager reads the issue once and asks the product owner every
   card in full; answers travel by card id. You never answer a card.
   Lavish only when a card is visual (a wireframe, layout comparison, or diagram): the wayfinder teammate draws it;
   start it as below with `wayfinder` in place of `explore`.
   Checks 2, 4, 5 are fact gaps: start an explore or groundwork teammate, never ask the product owner:
   `compose explore --project {slug} --extra - | compose crew start --role explore --project {slug} --brief -`
   (or `groundwork` in both places).
   A story that fails: report `blocked "<story>: not specced: <gap list>"`, naming the cards. The gap list is one comment on
   the story.

3. Cut each checked story into task issues with `compose linear create`. A task is right-sized when all of these hold:
   - one or two files in `files:` (a lockfile it names does not count), one test command, at most five acceptance criteria
   - its test command runs only its own test (its file, or a name filter), never the whole suite: verify runs the full checks
   - its brief (the issue description) is under 2500 characters
   - its branch stays within the CLI's 350-line limit (added plus deleted)
   - no documents: no .md, .txt, .rst, or docs/ files
   - `files:` names every file it may touch, new files too
   - no file shared by two tasks: a shared file means one task, or one is `after:` the other
   - `after:` only for code a task needs (it calls, imports, or edits what the other creates), set as the issue's blocked-by links
   - ids numbered in review order
   A story-sized criterion (one that spans several behaviors) is split, not copied.
   A story is never done by groundwork alone: every code, dependency, lockfile, and toolchain change is a task. Name the lockfile in
   `files:` (lockfiles do not count toward the 350 lines). A choice it needs (which version, which option) is a decision card
   (checks 1, 3, 6), settled before the task is cut. Only work that is not code (a manual browser run that needs the product
   owner's sign-in) stays teammate work, and it never exempts the story from tasks for its code.

4. The task issue: a sub-issue of its story, label Task, state Todo, its title the task's title. Its description, in this order:
   ## Goal
   One paragraph: what to build, where, exact export names, and any width-pinned or exact text the test asserts, verbatim.
   ## Acceptance criteria
   One bullet per behavior, each checked by the test.
   ## Test plan
   How to run (the `test:` command), what is covered in words, unlisted holdouts, then a `scenarios` block (SKILL.md).
   ## Constraints
   No new dependencies beyond what the goal names. Do not edit protected files.
   and last, the block the CLI reads:
   ```compose
   id: 001
   files: <the one or two paths>
   protected: <the test path>
   test: <the command that runs only this task's test>
   ```
   Its keys are id, files, protected, test; `model:` is optional, leave it unset. `after` and `title` are not block keys:
   `after` is the blocked-by links, `title` is the issue's own title.

5. After cutting: `compose sync` (it reads the feature and every issue under it from Linear) as sections/2.md says, then
   `compose spec-check --project {slug}`, then `compose tasks`; fix every FAIL and WARNING.
   A first cut and a re-cut are one diff: read the story's live task issues, compare them to the story and the map, then
   cancel, edit, add; ids stay monotonic, and a canceled task loses its `compose` block.
   Once a task of the feature was dispatched, the edits go inside `compose change start --why "<one line>"` (sync first)
   and `compose change done` (sync first). Before the first dispatch, no change.
   Each cut's judgment lands on the delivery plan: find the map's "<map id>: delivery plan" in `compose linear doc list`,
   then write its judgment sections (Outside waits, How this maps to compose, Priority when something has to give) at each
   cut with `compose linear doc update <id> --section "<heading>" --file <path>`. In it, priority is the ranked list under
   Priority when something has to give; Linear's priority field stays unused. After `compose sync` the CLI rewrites the
   data sections (Start today, Lanes, Waves, Critical path); never edit them.

6. Design stories (label Design) go to the designer role, not cut by you: report the hand-off in one line. Code stories do not
   wait: cut them now, and note on the feature the design stories still open.

7. Report the cut with each story's task issue ids.
"""

TOOLSMITH_BRIEF = """You are the toolsmith teammate for project {project}: compose itself failed there, and the newest entry in the
tool-bug log is your assignment: {reporter} at {at}, compose {cli_commit} in {cli_repo}:
  {message}
Read that repo's SKILL.md first; it is the only skill file to read, never another.
1. Reproduce the bug from the report in your worktree, then find its root cause in the code.
2. Write the body file: line 1 is `# <command>: <symptom>`, then the sections issue #1 has (`gh issue view 1 --repo {repo}`).
3. File it: `compose tool-bug file --project {project} --entry "{at}" --body <file>`. Exit 2 means the search found an open issue already:
   `--comment <n>` comments there when it is the same bug, `--new` creates a new one when it is not.
{variant}"""

CHANGE_BRIEF = """You are the wayfinder teammate for change of course {cid} on project {slug}. Worktree: {wt}. The map is the feature issue
{feature} on Linear; its Decisions so far is the one decision record. {cid} is open because: {why}
The screen that raised it: {reflect}
1. Read the map, the evidence (the screen file above, its `evidence` field, and the team lead's notes at the end of this brief),
   and the code each piece of evidence cites (`trail ask "<question>" --source`).
2. Write one card per decision line the evidence calls into question: the line as recorded, the evidence quoted verbatim with
   its source, the options with one line each with "keep it as is" always among them, and your recommended option marked. Never
   choose for the product owner. Text by default; a card is visual only when its options are pictures (a screenshot, a layout or flow
   comparison, a diagram), and then it is never asked as text. Visual cards go on one Lavish page, never the wayfinder's earlier page: {page}
   Build it as the wayfinder does: read `npx -y lavish-axi playbook input` and `npx -y lavish-axi design` first, set
   data-lavish-annotate="off" on <html>, give each card data-lavish-question, the recommended option preselected, a free-text box,
   and one answer button, and put one "accept every recommendation" button at the top; each sends exactly one
   window.lavish.queuePrompt with text "<question>: <answer>" and data {{question, answer}}; accept-all sends
   data {{question: "accept-all", answer: [{{question, answer}}, ...]}}.
3. Write every text card in full (question, why it matters, the options, recommendation) under Not yet specified on the map
   first, with a card id per card, never reusing an id; visual cards go on the page instead. Then ask once, all open cards in one
   line: open the page if there is one (`npx -y lavish-axi {page}`), and report, before any poll and whatever the CLI's next_step
   text says, one line:
   `compose report --project {slug} --as {name} needs-user "change {cid}: <issue>: <n> cards <ids>; recommendation <one
   line>; 'visual cards open in Lavish: <url>' when the page is open>"`, where <url> is the `url:` that `lavish-axi {page}` printed,
   copied and never retyped, in no second host form. `compose report` refuses a page without that link, a link that does not
   answer, and a card asked already and not answered. The team lead forwards the line unchanged; the engineering manager reads
   the issue once and asks the product owner every card in full; answers travel by card id.
4. Wait as the wayfinder does: poll in rounds (`npx -y lavish-axi poll {page} --owner {name} --takeover`, the Bash call's timeout
   180000 ms, run again at once when a round ends with nothing) so a text answer reaches you within three minutes; acknowledge
   answers on the page with `npx -y lavish-axi reply {page} --agent-reply "<card ids> received"`; on `browser_disconnected` run
   `npx -y lavish-axi {page}` once for its `url:` and report once `needs-user "reminder: change {cid}: ..."` with it, then poll again.
   Never loop or poll `compose linear` while you wait (the shared hourly Linear budget): read the issue once, and the herdr prompt brings the answer.
   Never `end` or `--reopen` a session the product owner ended. Answers arrive in the poll's `text` column (and as JSON after
   "Context data:" inside `prompt`), not as a data field.
5. Record each answer as it arrives: read the section with `compose linear get <id> --body`, then write the whole section back, its lines plus the new one, with `compose linear update <id> --section "## Decisions so far" --file <path> --expect <hash>`, the hash `get --body` listed for that section; if it says the section changed since you read it, read it again and keep what changed; never a whole-description write. A line that changes is struck through
   and tagged, with the new line right after it:
   - ~~<old line>~~ ({cid})
   - <new line> ({cid}: <why, naming the evidence's source>)
   "keep it as is" changes nothing in the map.
   An answer that starts `away:` or `yolo:` is the engineering manager's recommendation, never the product owner's:
   end its line `(away recommendation, unconfirmed, <date>)` or `(yolo recommendation, unconfirmed, <date>)`, the one its prefix names.
6. Evidence the team lead forwards while {cid} is open becomes new cards, asked the same way; visual ones on the same page, then
   `npx -y lavish-axi poll {page} --agent-reply "<what changed>"` and wait again.
7. When every card is answered: `npx -y lavish-axi end {page}` if the page was opened, then report `done` with each answer in one line.
   Do not touch tasks, stories, or the plan: the team lead re-plans from your answers.
"""

EXPLORE_BRIEF = """You are the explore teammate for project {slug}. Worktree: {wt}. The feature issue is {feature} on Linear.
Map the existing code for this feature fast, through trail only: `trail map`, `trail ask "<question>" --source`, `trail callers <symbol>`,
`trail skeleton <file>`. Open a source file only at the exact file:line a node points to, and only when the node lacks a needed detail.
Time box: under 3 minutes. Write no code and change no file.
Write a codebase brief of at most 40 lines:
- structure and entry points (server, client, how they start)
- where this feature plugs in: files and symbols with file:line
- existing patterns and helpers to reuse instead of rewriting
- test setup: frameworks, where tests live, the exact commands
- risks and unknowns the plan must settle
Read the Notes section with `compose linear get <id> --body`, then write the whole section back, its lines plus the brief as `### Codebase brief`, with `compose linear update <id> --section "## Notes" --file <path> --expect <hash>`, the hash `get --body` listed for that section; if it says the section changed since you read it, read it again and keep what changed; never a whole-description write. Then report `done "<one-line summary>"`.
"""

def cmd_explore(a):
    """Print the explore teammate's brief: a fast trail-only map of an existing repo for the planners."""
    proj = fc_core.project_dir(a); wt = fc_herdr.project_worktree(proj)
    extra = sys.stdin.read() if a.extra == "-" else (a.extra or "")
    feature = (json.loads((proj / "linear.json").read_text()) if (proj / "linear.json").exists() else {}).get("feature", "<feature id>")
    print(EXPLORE_BRIEF.format(slug=proj.name, wt=wt, feature=feature)
          + (f"\nProject notes from the team lead:\n{extra.strip()}\n" if extra.strip() else ""))

def cmd_wayfinder(a):
    """Print the wayfinder teammate's brief for this project, plus the team lead's notes from --extra (or stdin with `-`). With
    --change, the brief for an open change of course: its own page, only what the evidence questions."""
    proj = fc_core.project_dir(a); wt = fc_herdr.project_worktree(proj)
    extra = sys.stdin.read() if a.extra == "-" else (a.extra or "")
    feature = (json.loads((proj / "linear.json").read_text()) if (proj / "linear.json").exists() else {}).get("feature", "<feature id>")
    cid, name = getattr(a, "change", None), fc_herdr.agent_name(proj.name, "wayfinder")
    if cid:
        rec = fc_change.load_change(proj, cid)
        if not rec or rec.get("done_at"):
            sys.exit(f"{cid} is not an open change of course; `compose change start --why ...` opens one")
        brief = CHANGE_BRIEF.format(slug=proj.name, wt=wt, feature=feature, cid=cid, why=rec["why"], name=name,
                                    reflect=proj / "results" / rec["reflect"] if rec.get("reflect") else "none recorded",
                                    page=proj / "lavish" / f"change-{cid}.html")
    else:
        brief = WAYFINDER_BRIEF.format(slug=proj.name, wt=wt, feature=feature, page=proj / "lavish" / "wayfinder.html", name=name)
    print(brief + (f"\nProject notes from the team lead:\n{extra.strip()}\n" if extra.strip() else ""))

def cmd_planner(a):
    """Print the planner teammate's brief for this project, plus the team lead's notes from --extra (or stdin with `-`)."""
    proj = fc_core.project_dir(a); wt = fc_herdr.project_worktree(proj)
    extra = sys.stdin.read() if a.extra == "-" else (a.extra or "")
    feature = (json.loads((proj / "linear.json").read_text()) if (proj / "linear.json").exists() else {}).get("feature", "<feature id>")
    issue = a.issue or feature
    role = "planner" if issue.lower() == feature.lower() else re.sub(r"[^a-z0-9_-]+", "-", f"planner-{issue.lower()}").strip("-")  # a story's planner runs beside the feature's
    print(PLANNER_BRIEF.format(slug=proj.name, wt=wt, issue=issue, name=fc_herdr.agent_name(proj.name, role))
           + (f"\nProject notes from the team lead:\n{extra.strip()}\n" if extra.strip() else ""))

def cmd_toolsmith(a):
    """Print the toolsmith's brief for the project's newest tool-bug entry: the maintainer's toolsmith files and fixes on a
    branch, anyone else's only reproduces, diagnoses, and files; exits non-zero when the project has no entry."""
    proj = fc_core.project_dir(a)
    entry, log = None, Path(fc_crew.TOOL_BUGS)
    if log.exists():
        for line in log.read_text().splitlines():
            d = json.loads(line)
            if d.get("project") == proj.name:
                entry = d
    if not entry:
        sys.exit(f"no tool-bug entry for project {proj.name}")
    repo, state = fc_toolbug.REPO, fc_toolbug.gh_status()[0]
    if state == "maintainer":
        variant = f"""4. You are a maintainer on {repo}: file or comment first, then fix the root cause on your own branch in your own worktree of
   the compose repo, with a test that fails without the fix and passes with it, and run the full suite, not only your test.
   Never commit on main of any checkout, in ~/.compose/src, or in the project worktree: the tool refuses there.
5. Commit the fix with a plain message and no `Fixes #` in it, then run
   `compose tool-bug pr --project {proj.name} --entry "{entry['at']}" --worktree <your worktree>`: it pushes your branch and opens the PR,
   which closes the issue when the product owner merges it. You never merge.
6. Report `done` with the PR's URL.
"""
    else:
        variant = f"""4. You are not a maintainer on {repo}: you reproduce, diagnose, and file only, so never edit or commit anything; this
   overrides the worktree line `crew start` puts first.
5. Without `--approved` the filing saves a draft instead of posting: report `needs-user` with the saved path and your
   diagnosis, and wait. On the go, run the same filing command with `--approved`, then report `done` with the issue's URL.
"""
        if state == "no-gh":
            variant += "   gh does not work here: if the filing prints a `gh auth login` hint, put that in your needs-user line too.\n"
    print(TOOLSMITH_BRIEF.format(project=proj.name, reporter=entry["reporter"], at=entry["at"], cli_commit=entry["cli_commit"],
                                 cli_repo=entry["cli_repo"], message=entry["message"], repo=repo, variant=variant))

TEST_BUDGET = 60  # lines of test per task, unless the task's block sets `test_lines:`; run 5's test-writing teammates wrote 2044 lines for 727 of code

def test_brief(meta, contract, budget=None, conv=None, rebuilt=False):
    """`rebuilt`: the task passed before a re-plan changed its brief (testcrews --redo), so its file holds the old brief's test."""
    paths = ", ".join(meta.get("protected") or []) or "the test path its brief names"
    args = " ".join(meta.get("protected") or []) or "<the test path>"  # git reads "a.py," as a path, so the command gets no commas
    budget = int(meta.get("test_lines") or budget or TEST_BUDGET)
    files = ", ".join(meta.get("files") or [])
    fails = (f"Its brief changed after it was built: rewrite the test {paths} holds to the TASK BRIEF. It must fail on the project "
             f"branch for what the brief changed, and pass once {files} implement the new brief.\n" if rebuilt else
             f"It must fail on the current stubs for the reason this task fixes, and pass once {files} implement the brief.\n")
    return (f"Write the acceptance test for task {meta['id']} ({meta.get('title', '')}) and nothing else.\n"
            f"Test file: {paths}. Test command: {meta.get('test')}\n" + fails +
            "Use exactly the types, signatures, helpers, and fixtures in the CONTRACT below; never guess a shape it does not give "
            "(report `blocked` naming what is missing instead).\n"
            "Assert only what the TASK BRIEF states or the code already shows: its implementer gets the brief, the stubs, and your test, "
            "never the contract, so a line or value only the contract gives is not asserted.\n"
            "Cover the brief's acceptance criteria plus holdout cases it does not list. One file, proportional to the code it tests; "
            "no CSS values, no tooling tests: never a test that asserts dependency lists, lint or tool configuration, or the contents of a config file; test behavior only.\n" +
            ("The TASK BRIEF's `scenarios` block gives one scenario per criterion: write a case for each scenario and for each of its negative_cases, then add holdout cases none of them lists, within the budget.\n" if fc_tasks.scenarios_block(meta.get("body")) else "") +
            f"Budget: at most {budget} lines, holdouts included. Pick the cases that would catch a wrong implementation; drop the rest.\n"
            "You work in your own worktree on your own branch; the CLI collects every test-writing teammate's commits onto the project branch. "
            "Run that command, never the whole suite, and confirm it fails for the right reason, format the file, then commit only your file: "
            f"`git add {args} && git commit {args} -m \"{fc_core.typed('<what the test covers>', 'test', conv) if conv else '<plain message>'}\"`.\n"
            "To trim or fix your test after that, make another plain commit the same way. Never run `git commit --amend`, `git reset`, "
            "`git rebase`, `git checkout`, or `git stash`: a plain new commit is all the collect step needs.\n"
            "Report `working \"test ready: <commit>; holdouts: <what they cover>\"`, and `done` only when the team lead says so.\n\n"
            f"TASK BRIEF:\n{meta.get('body', '')}\n\nCONTRACT:\n{contract}")

def cmd_testcrews(a):
    """Start one test-writing teammate per task that has a protected test, in parallel, each briefed with its task and the groundwork's shared contract.
    `--print` shows the briefs instead. `--redo` takes passed tasks too, as `run --redo` rebuilds them: a re-plan that changed
    their briefs needs their tests rewritten first. `--parallel N` starts up to N writers now, Claude Code or not, over the config's caps."""
    proj = fc_core.project_dir(a)
    if getattr(a, "collect", False):
        return collect_tests(proj, a.only)
    contract_file = Path(a.contract) if a.contract else proj / "results" / "contract.md"
    if not contract_file.exists():
        sys.exit(f"no contract at {contract_file}: the groundwork teammate writes it after the stubs (compose groundwork, phase C)")
    contract, budget, redo = contract_file.read_text(), getattr(a, "budget", None), bool(getattr(a, "redo", False))
    only = {x.strip() for x in a.only.split(",")} if a.only else None
    tasks = fc_tasks.load_tasks(proj, required=True)
    finished = lambda m: fc_tasks.task_finished(proj, m, redo) or (not redo and test_collected(proj, m["id"]))  # a finished task needs no test writer
    metas = [m for m in tasks if (not only or m["id"] in only) and not finished(m)]
    no_test = [m["id"] for m in metas if not (m.get("protected") or [])]  # nothing protected to write: no writer starts for it
    metas = [m for m in metas if m["id"] not in no_test]
    rebuilt = {m["id"] for m in metas if redo and fc_tasks.task_finished(proj, m)}  # built on the old brief: its file holds the old test
    for tid in sorted(only or no_test):  # a skipped task says why; with --only, only the named ones
        if tid in no_test:
            print(f"{tid} skipped: no protected test to write")
        elif not any(m["id"] == tid for m in metas):
            m = next((m for m in tasks if m["id"] == tid), None)
            why = finished(m) if m else "no such task"
            print(f"{tid} skipped: {why}" + ("; pass --redo to rewrite its test" if why.startswith(("its result", "its test")) else ""))
    brief = lambda m: test_brief(m, contract, budget, fc_core.commit_convention(proj), m["id"] in rebuilt)
    again = " --redo" if redo else ""
    if a.print_:
        for m in metas:
            print(f"===== tests-{m['id']}\n{brief(m)}\n")
        return
    blocked = test_blockers(proj, tasks, metas)
    metas = [m for m in metas if m["id"] not in blocked]
    model = "tests" if "tests" in fc_config.CREW_MODELS else "groundwork" if "groundwork" in fc_config.CREW_MODELS else None
    cap = getattr(a, "parallel", None) or fc_config.PARALLEL  # unset: every writer starts at once
    key, chain = fc_crew.crew_chain("tests", model)
    if not getattr(a, "parallel", None) and (key is None or fc_claude.is_claude(chain[0][0])):  # Claude Code writers are heavy: they stay within claude_parallel whatever `parallel` is, unless --parallel says otherwise
        cap = min(cap or fc_config.CLAUDE_PARALLEL, fc_config.CLAUDE_PARALLEL)
    metas, queued = cap_split(proj, tasks, metas, cap)
    if fc_worktree.use_treehouse():  # the whole set fits, or nothing is leased and no teammate starts
        held = fc_worktree.slots(proj)
        spare = sum(k.startswith("spare-") for k in held)  # slots a collect kept for these writers
        short = fc_worktree.pool_shortfall(fc_herdr.project_worktree(proj), sum(not (held.get(f"tests-{m['id']}") and Path(held[f"tests-{m['id']}"]).exists()) for m in metas) - spare)
        if short:
            sys.exit(f"testcrews: {short}")
    asked = only or {m["id"] for m in tasks}
    how = {"contract": str(contract_file), "budget": budget, "redo": redo}
    waiting_writers(proj, {**{t: None for t in asked}, **{t: how for t in blocked}, **{m["id"]: how for m in queued}})  # a queued writer counts as an earlier one
    for tid, by in blocked.items():
        print(f"tests-{tid} waits for {', '.join('tests-' + b for b in by)}, which write its test file too; --collect starts it once they are collected")
    def say_queued():
        if queued:
            ids = ",".join(m["id"] for m in queued)
            print(f"{len(queued)} test writer(s) queued past the cap of {cap} (`parallel`, and `claude_parallel` for Claude writers, in {fc_config.CONFIG_PATH}): {ids}\n"
                  f"--collect starts them, in task order, as earlier ones are collected; or start them yourself: compose testcrews --project {proj.name} --only {ids}{again}")
    if not metas:
        say_queued()
        return
    fc_herdr.herdr_space(proj)  # once, before the parallel starts
    wts = {m["id"]: test_worktree(proj, m["id"]) for m in metas}  # serially: worktree adds share one lock
    def start(m):
        try:
            fc_crew.cmd_crew(argparse.Namespace(repo=a.repo, project=proj.name, action="start", role=f"tests-{m['id']}", title=f"tests {m['id']}",
                                        brief=brief(m), force=False, cwd=str(wts[m["id"]]), leased=True, model=model))
        except (SystemExit, Exception) as e:  # noqa: BLE001  one teammate's failure must not stop the rest, and is never silent
            failed.append(m["id"])
            print(f"tests-{m['id']} not started: {e}")
    failed = []
    with ThreadPoolExecutor(max_workers=len(metas) or 1) as ex:
        list(ex.map(start, metas))
    if failed:
        print(f"not started: {','.join(sorted(failed))}; start them again: compose testcrews --project {proj.name} --only {','.join(sorted(failed))}{again}")
    say_queued()

def cap_split(proj, tasks, metas, cap):
    """`metas` split into the writers to start now and those queued past `cap`. A writer whose branch is open is still writing
    or not collected yet, and holds a slot: the cap counts those too, so --collect never runs more than `cap` at once."""
    if not cap:
        return metas, []
    ids = {m["id"] for m in metas}
    room = max(cap - sum(test_open(proj, o["id"]) for o in tasks if o["id"] not in ids), 0)
    return metas[:room], metas[room:]

def test_branch(proj, tid):
    return fc_worktree.task_prefix(fc_herdr.project_worktree(proj), proj) + f"tests-{tid}"

def test_open(proj, tid):
    """Whether a test writer's branch is still to be collected."""
    return fc_core.sh(["git", "rev-parse", "--verify", "-q", test_branch(proj, tid)], fc_herdr.project_worktree(proj), check=False).returncode == 0

def test_collected(proj, tid):
    """Why a task's test writer is over, or None: it reported done and its branch is collected, so a plain `testcrews` leaves it be.
    Only --redo starts a writer again."""
    last = (fc_crew.crew_reports(proj, fc_herdr.agent_name(proj.name, f"tests-{tid}")) or [{}])[-1]
    return "its test is collected" if last.get("state") == "done" and not test_open(proj, tid) else None

def waiting_writers(proj, update=None):
    """The test writers waiting for another writer of the same test file, by task id, with the contract, budget, and --redo to start
    them with."""
    f = proj / "results" / "tests-waiting.json"
    have = json.loads(f.read_text()) if f.exists() else {}
    if update is not None:
        have = {k: v for k, v in {**have, **update}.items() if v}
        fc_core.atomic_write(f, json.dumps(have, indent=2))
    return have

def test_written(meta, wt):
    """The protected files a task's test writer writes: those not on the project branch yet, and those its test command runs
    (named by path, file name, or module), or every one when the command names none. A file already there that the command
    does not run (groundwork's pins, say) is only read."""
    paths = meta.get("protected") or []
    ran = [p for p in paths if re.search(rf"(?<![\w-]){re.escape(Path(p).stem)}(?![\w-])", meta.get("test") or "")]
    return set(ran or paths) | {p for p in paths if fc_core.sh(["git", "cat-file", "-e", f"HEAD:{p}"], wt, check=False).returncode}

def test_blockers(proj, tasks, starting):
    """The writers each task in `starting` waits for: of the tasks writing one of the files it writes, those whose branch is not
    collected yet, and earlier ones that start now or wait. Started together off one commit, two writers of one file each created
    it, or each appended to it, and the second collect conflicted; one after another, each starts off the project branch that
    holds the test before it. A task whose own branch is open started long ago and waits for nothing."""
    queued = {m["id"] for m in starting} | set(waiting_writers(proj))
    wt = fc_herdr.project_worktree(proj)
    written = {o["id"]: test_written(o, wt) for o in tasks}
    out = {}
    for m in starting:
        by = [o["id"] for o in tasks if o["id"] != m["id"] and written[m["id"]] & written[o["id"]]
              and ((o["id"] < m["id"] and o["id"] in queued) or test_open(proj, o["id"]))]
        if by and not test_open(proj, m["id"]):
            out[m["id"]] = by
    return out

def test_ready(report):
    return report.get("state") == "done" or (report.get("state") == "working" and "test ready" in report.get("message", "").lower())

def test_worktree(proj, tid):
    """Each test-writing teammate writes on its own branch in its own worktree off the stubs commit: teammates sharing one
    branch raced (an amend landed on another teammate's commit). The collect step brings their commits back in task order."""
    wt = fc_herdr.project_worktree(proj)
    branch, held = test_branch(proj, tid), fc_worktree.slots(proj).get(f"tests-{tid}")
    if held and Path(held).exists():
        return Path(held)
    if fc_worktree.use_treehouse():
        fc_core.sh(["git", "branch", "-f", branch, "HEAD"], wt)
        spare = next((k for k in fc_worktree.slots(proj) if k.startswith("spare-")), None)  # a collected writer's slot, still leased
        if spare:
            path = Path(fc_worktree.slots(proj)[spare])
            fc_worktree.checkout_in_slot(path, branch)
        else:
            path = fc_worktree.slot_on(wt, branch, f"compose:{proj.name}:tests-{tid}")
        fc_worktree.slots(proj, {f"tests-{tid}": str(path), **({spare: None} if spare else {})})
    else:
        path = proj / "worktrees" / f"tests-{tid}"
        if path.exists():
            return path
        fc_core.sh(["git", "worktree", "add", "-q", "-B", branch, str(path), "HEAD"], wt)
    fc_worktree.ready_checkout(wt, path, install=fc_core.project_config(proj).get("install"))
    return path

def collect_tests(proj, only=None):
    """Cherry-pick the own commits of every test-writing teammate that reported its test ready onto the project branch in task order,
    remove their worktrees and branches, then start the writers that waited for them. A writer not yet ready is skipped: returning
    its slot ends the session still in it. A conflict keeps that writer's branch and worktree for a look, the rest are still
    collected, and the collect exits naming every conflict."""
    wt = fc_herdr.project_worktree(proj)
    want = {x.strip() for x in only.split(",")} if only else None
    by_id = {m["id"]: m for m in fc_tasks.load_tasks(proj, required=True) if not want or m["id"] in want}
    ids = sorted(by_id)
    conflicts = []
    for tid in ids:
        branch = test_branch(proj, tid)
        if not test_open(proj, tid):
            continue
        writer = fc_herdr.agent_name(proj.name, f"tests-{tid}")
        last = (fc_crew.crew_reports(proj, writer) or [{}])[-1]
        if not test_ready(last):
            fc_core.say(f"tests-{tid} skipped: it has not reported test ready (last: {last.get('state', 'nothing')} {last.get('message', '')!r}); its branch and worktree are kept")
            continue
        if fc_testred.gate(proj, by_id[tid], writer):  # a test that cannot load goes back to its writer once
            continue
        # a commit already on the branch is skipped: the same patch, or picked by hand (a cherry-pick keeps the author, the author
        # date, and the subject through a conflict resolution and any later edit, where no patch or content check would match)
        fmt = ["--no-merges", "--date=raw", "--format=%H%x00%an <%ae> %ad %s"]
        picked = {l.split("\0")[1] for l in fc_core.sh(["git", "log", *fmt, f"{branch}..HEAD"], wt).stdout.splitlines()}
        ahead = fc_core.sh(["git", "rev-list", "--count", "--no-merges", f"HEAD..{branch}"], wt).stdout.strip()
        commits = [l.split("\0") for l in fc_core.sh(["git", "log", "--reverse", "--right-only", "--cherry-pick", *fmt, f"HEAD...{branch}"], wt).stdout.splitlines()]
        commits = [c for c, who in commits if who not in picked]
        for c in commits:
            p = fc_core.sh(["git", "cherry-pick", "--allow-empty", c], wt, check=False)
            if p.returncode:
                fc_core.sh(["git", "cherry-pick", "--abort"], wt, check=False)
                conflicts.append(f"tests-{tid}: {c[:7]} conflicts with the project branch; its branch {branch} is kept.\n{(p.stdout + p.stderr)[-600:]}")
                break
        else:
            held = fc_worktree.slots(proj).get(f"tests-{tid}")
            if not held and fc_worktree.use_treehouse():  # the record is gone: git still knows where the branch is checked out
                there = fc_worktree.checked_out(wt, branch)
                held = str(there) if there and fc_worktree.is_slot(wt, there) else None
            if held and not waiting_writers(proj) and fc_worktree.release_slot(wt, held):
                fc_worktree.slots(proj, {f"tests-{tid}": None})
            elif held:  # the chained next writer takes this slot as it is: returning it leaves a gap for another project's lease. A slot treehouse cannot return stays leased the same way
                fc_worktree.park_slot(held, fc_core.sh(["git", "rev-parse", "HEAD"], wt).stdout.strip())
                fc_worktree.slots(proj, {f"tests-{tid}": None, f"spare-{tid}": held})
            else:
                fc_core.sh(["git", "worktree", "remove", "--force", str(proj / "worktrees" / f"tests-{tid}")], wt, check=False)
            fc_core.sh(["git", "branch", "-D", branch], wt, check=False)
            if last.get("state") != "done":  # its session ended with its slot: it is finished, not a teammate that went missing
                fc_crew.write_report(proj, writer, "done", "test collected onto the project branch")
            fc_core.say(f"tests-{tid} collected  {len(commits)} commit(s)" + (f", {int(ahead) - len(commits)} already on the branch" if int(ahead) > len(commits) else ""))
    fc_core.sh(["git", "worktree", "prune"], wt, check=False)
    groups = {}
    for tid, how in waiting_writers(proj).items():
        groups.setdefault((how["contract"], how["budget"], bool(how.get("redo"))), []).append(tid)
    try:
        for (contract, budget, redo), tids in groups.items():
            cmd_testcrews(argparse.Namespace(repo=str(wt), project=proj.name, contract=contract, only=",".join(tids), budget=budget, print_=False, collect=False,
                                             redo=redo))
    finally:  # slots kept for writers that did not start (still blocked, failed, or past the cap) go back to the pool
        for role, held in {k: v for k, v in fc_worktree.slots(proj).items() if k.startswith("spare-")}.items():
            if fc_worktree.release_slot(wt, held):
                fc_worktree.slots(proj, {role: None})
    if conflicts:
        sys.exit("\n".join(conflicts))
