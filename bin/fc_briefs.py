import argparse, fcntl, json, re, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import fc_core, fc_config, fc_claude, fc_tasks, fc_herdr, fc_worktree, fc_change, fc_crew, fc_crewpick, fc_toolbug, fc_testred, fc_planner_brief, fc_prd


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

GROUNDWORK_BRIEF = """You are groundwork teammate for project {slug}. Worktree: {wt}, branch {trunk}. Task briefs are the contract:
read them first with `compose tasks --repo {wt} --project {slug}`. Use `trail ask "<question>" --source` for code context.
Commit on {trunk} in small plain commits. Nothing is pushed.

Phase A, toolchain (probe and report; never change dependencies):
{stacks}
1. Install from lockfiles, build, test, and start every part as a user would (probe server URL; start client with BROWSER=none).
2. For each breakage find the smallest fix (documented setting, or upgrade); leave dependencies and lockfiles as they are.
3. Report `working "toolchain: <what breaks>; recommend <fix>, alternative <alt>"` and wait for team lead's go in this pane.

Phase B, after the go:
1. Dependency, lockfile, and toolchain changes are implementer tasks, never yours: planner splits them; a choice the probe found is a decision for the product owner (0b).
2. Write schema, codegen, wiring, and stubs the task briefs need, with the exact signatures briefs give: stubs compile and return "not implemented" errors, zero values, or null. No feature logic.
3. verify.json ({verify}):`checks` = every smoke command that passed. For UI or server: `e2e_setup` starting server on $E2E_PORT and client on $E2E_CLIENT_PORT (CLI hands out free ports and stops what setup starts; never hardcode unpinned ports, never free ports with lsof or kill), `e2e_setup_wait`, one `e2e` case per user flow with a goal and an `assert` script checking real log or DOM: once it sees the app in the state the flow needs (signed in, no overlay, seed present) and the feature absent, it prints a line starting `ASSERT FAILED` and exits non-zero; if it cannot tell, it exits non-zero without that line. Scripts go in {e2e_dir}, never ~/.compose or /tmp. Run checks with `compose verify --project {slug} --skip-review --skip-e2e --fix 0`, then prove e2e cases with `compose verify --project {slug} --prove`: each must fail only because the feature is missing, and `run` refuses until that holds; fix what it names. Run both directly: under `compose timed` jev has no TypeSafe key.
4. Commit.

Phase C, contract (you write no tests; one teammate per task does, from your contract):
Write {contract} (outside repo; never commit it) with what a test writer needs and must not guess:
- every type, struct, generated model a test touches, with package and value or pointer
- every stub's exact signature, and which task implements it
- test helpers and fixtures
- per task: test path (its `protected:`), test names its `test:` command selects, that command
Format: shared sections first (types, stubs, helpers), then one `## task <id>` section per task (e.g. `## task 001`) with only that task's test path, names, command, and fixtures; each writer gets the shared sections and its own section.
Signatures and exact lines are copied from the briefs, never added: implementers see brief and code, never contract.
A test needing a value, line, or rule no brief states is `blocked "<task>: brief lacks <what>"`, for the planner to add.
Report `working "stubs ready: <commit>; contract at {contract}"`. Report `done` only when team lead says so.
"""

def cmd_groundwork(a):
    """Print the groundwork brief for this project, plus the team lead's project notes from --extra (or stdin with `-`)."""
    proj = fc_core.project_dir(a); wt = fc_herdr.project_worktree(proj)
    extra = sys.stdin.read() if a.extra == "-" else (a.extra or "")
    print(GROUNDWORK_BRIEF.format(slug=proj.name, wt=wt, trunk=fc_core.current_branch(wt), stacks=detect_stacks(wt),
                                  verify=proj / "verify.json", contract=proj / "results" / "contract.md", e2e_dir=proj / "e2e")
          + (f"\nProject notes from the team lead:\n{extra.strip()}\n" if extra.strip() else ""))

TOOLSMITH_BRIEF = """You are toolsmith for project {project}: compose failed there. Newest tool-bug log entry is your assignment:
{reporter} at {at}, compose {cli_commit} in {cli_repo}:
  {message}
Read that repo's SKILL.md first; no other skill file.
1. Reproduce bug in your worktree, find root cause.
2. Write body file: line 1 `# <command>: <symptom>`, then sections of issue #1 (`gh issue view 1 --repo {repo}`).
3. File: `compose tool-bug file --project {project} --entry "{at}" --body <file>`. Exit 2: open issue exists;
   `--comment <n>` if same bug, `--new` if not.
{variant}"""

CHANGE_BRIEF = """You are architect teammate for change of course {cid} on project {slug}. Worktree: {wt}. Map is feature issue
{feature} on Linear; its Decisions so far is the one decision record. {cid} is open because: {why}
Screen that raised it: {reflect}
1. Read the map, the evidence (screen file above, its `evidence` field, team lead's notes below),
   and the code it cites (`trail ask "<question>" --source`).
2. Write one question per decision line the evidence questions:
   `- {cid}-1: <question> (replaces <the D id it would replace>) - Recommendation: <recommendation> - <the evidence, quoted verbatim, with its source>`,
   ids {cid}-1, {cid}-2 and on; never reuse an id. Keeping the line is always an answer.
   `compose report` refuses a question without a recommendation, or one that lists lettered options: ask it open, and weigh the
   options in the recommendation. Never choose for the product owner.
   Text by default; visual only when the choices are pictures, then never asked as text. Visual questions go on one Lavish page, never the architect's earlier page: {page}
   The page shows pictures only, with no answer controls; answers come through the engineering manager.
3. Write every question under Not yet specified on the map first, visual ones too; visual ones go on the page as well.
   Then ask once, all open questions in one line: open the page if any
   (`npx -y lavish-axi {page}`), and report one line:
   `compose report --project {slug} --as {name} needs-user "change {cid}: <issue>: <n> questions <ids>; recommendation <one
   line>; 'visual questions open in Lavish: <url>' when the page is open>"`, where <url> is the `url:` that `lavish-axi {page}` printed,
   copied, never retyped. The team lead forwards the line unchanged; the engineering manager reads them with
   `compose decisions --project {slug}` and asks the product owner one question at a time; answers travel by question id.
4. Wait for the herdr prompt that brings each answer; the page takes no answers, so never poll it for them.
   Never loop or poll `compose linear` while you wait (shared hourly Linear budget): read the issue once, the herdr prompt brings the answer.
5. You never write a Decisions so far line. Answers land through `compose decide --change {cid}`, run by the engineering manager with
   the product owner's words attached: each answer is `--new "<decision>" --closes <id>` and becomes a line
   `D<n>: <decision> (closes <id>) ({cid}; product owner, <date>; said: "<their words>")`. An answer that changes a decision also names it,
   `--replaces <D id>`, and that line is struck through, `~~<old line>~~ ({cid})`; an answer that keeps it strikes nothing (`D4 stands: ...`).
   An answer that starts `away:` or `yolo:` is the engineering manager's recommendation, never the product owner's:
   `compose decide` ends its line `(away recommendation, unconfirmed, <date>)` or `(yolo recommendation, unconfirmed, <date>)`, the one its prefix names.
   When the team lead says a decision landed, read the map once (`compose linear get <id> --body`) and go on from it.
6. Evidence the team lead forwards while {cid} is open becomes new questions, asked the same way; visual ones on the same page, then wait again.
7. When every question is closed: `npx -y lavish-axi end {page}` if the page was opened (never `end` or `--reopen` a session the product owner ended), then report `done` with each decision in one line.
   Do not touch tasks, stories, or the plan: the team lead re-plans from your answers.
"""

EXPLORE_BRIEF = """You are explore teammate for project {slug}. Worktree: {wt}. Feature issue: {feature} on Linear.
Map existing code fast, through trail only: `trail map`, `trail ask "<question>" --source`, `trail callers <symbol>`, `trail skeleton <file>`.
Open source file only at exact file:line a node points to, when node lacks detail.
Time box: under 3 minutes. Write no code, change no file.
Brief of at most 40 lines:
- structure, entry points
- where feature plugs in: files, symbols, file:line
- patterns, helpers to reuse
- tests: frameworks, locations, exact commands
- risks, unknowns
Read Notes with `compose linear get <id> --body`, then write the whole section back, its lines plus brief as `### Codebase brief`, with `compose linear update <id> --section "## Notes" --file <path> --expect <hash>` (hash from `get --body`); if section changed, reread, keep changes; never a whole-description write. Then report `done "<one-line summary>"`.
"""

def cmd_explore(a):
    """Print the explore teammate's brief: a fast trail-only map of an existing repo for the planners."""
    proj = fc_core.project_dir(a); wt = fc_herdr.project_worktree(proj)
    extra = sys.stdin.read() if a.extra == "-" else (a.extra or "")
    feature = (json.loads((proj / "linear.json").read_text()) if (proj / "linear.json").exists() else {}).get("feature", "<feature id>")
    print(EXPLORE_BRIEF.format(slug=proj.name, wt=wt, feature=feature)
          + (f"\nProject notes from the team lead:\n{extra.strip()}\n" if extra.strip() else ""))

def cmd_architect(a):
    """Print the change-of-course architect's brief for the open change --change names, plus the team lead's notes from --extra
    (or stdin with `-`): its own page, only what the evidence questions. Shaping a feature is the engineering manager's brainstorm (0b)."""
    proj = fc_core.project_dir(a); wt = fc_herdr.project_worktree(proj)
    extra = sys.stdin.read() if a.extra == "-" else (a.extra or "")
    feature = (json.loads((proj / "linear.json").read_text()) if (proj / "linear.json").exists() else {}).get("feature", "<feature id>")
    cid, name = a.change, fc_herdr.agent_name(proj.name, "architect")
    rec = fc_change.load_change(proj, cid)
    if not rec or rec.get("done_at"):
        sys.exit(f"{cid} is not an open change of course; `compose change start --why ...` opens one")
    brief = CHANGE_BRIEF.format(slug=proj.name, wt=wt, feature=feature, cid=cid, why=rec["why"], name=name,
                                reflect=proj / "results" / rec["reflect"] if rec.get("reflect") else "none recorded",
                                page=proj / "lavish" / f"change-{cid}.html")
    print(brief + (f"\nProject notes from the team lead:\n{extra.strip()}\n" if extra.strip() else ""))

def cmd_planner(a):
    """Print the planner teammate's brief for this project, plus the team lead's notes from --extra (or stdin with `-`)."""
    proj = fc_core.project_dir(a); wt = fc_herdr.project_worktree(proj)
    extra = sys.stdin.read() if a.extra == "-" else (a.extra or "")
    feature = (json.loads((proj / "linear.json").read_text()) if (proj / "linear.json").exists() else {}).get("feature", "<feature id>")
    issue = a.issue or feature
    role = "planner" if issue.lower() == feature.lower() else re.sub(r"[^a-z0-9_-]+", "-", f"planner-{issue.lower()}").strip("-")  # a story's planner runs beside the feature's
    brief = fc_planner_brief.PLANNER_BRIEF.format(slug=proj.name, wt=wt, issue=issue, name=fc_herdr.agent_name(proj.name, role))
    print(fc_planner_brief.with_prd(brief, _prd(wt)) + (f"\nProject notes from the team lead:\n{extra.strip()}\n" if extra.strip() else ""))

def _prd(wt):
    """The repo's PRD for the planner brief, or None: with no Linear key, no link, or Linear down the brief goes out without it."""
    if not fc_config.LINEAR_KEY:
        return None
    try:
        return fc_prd.read_prd(wt)
    except fc_prd.LOOKUP_ERRORS:
        return None

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
    same = len(fc_toolbug.entries_at(proj.name, entry["at"]))  # several at one time: the entry is named by its number
    at = f"{entry['at']}#{same}" if same > 1 else entry["at"]
    if state == "maintainer":
        variant = f"""4. Maintainer on {repo}: file or comment first, then fix root cause on own branch in your own worktree of compose repo,
   with test failing without fix, passing with it; run only test files of what you changed, never whole suite.
   Never commit on main of any checkout, in ~/.compose/src, or in project worktree: tool refuses.
5. Commit with plain message, no `Fixes #`, then run
   `compose tool-bug pr --project {proj.name} --entry "{at}" --worktree <your worktree>`: pushes branch, opens PR,
   closing issue when product owner merges. You never merge.
6. Report `done` with the PR's URL.
"""
    else:
        variant = f"""4. Not maintainer on {repo}: reproduce, diagnose, file only; never edit or commit; this
   overrides worktree line `crew start` puts first.
5. Without `--approved` filing saves draft: report `needs-user` with saved path, diagnosis; wait. On the go, run the same filing command with `--approved`, then report `done` with issue's URL.
"""
        if state == "no-gh":
            variant += "   gh fails here: if filing prints `gh auth login` hint, put it in needs-user line too.\n"
    print(TOOLSMITH_BRIEF.format(project=proj.name, reporter=entry["reporter"], at=at, cli_commit=entry["cli_commit"],
                                 cli_repo=entry["cli_repo"], message=entry["message"], repo=repo, variant=variant))

TEST_BUDGET = 60  # lines of test per task, unless the task's block sets `test_lines:`; run 5's test-writing teammates wrote 2044 lines for 727 of code
def contract_slice(contract, tid): return "".join(p for p in re.split(r"(?m)^(?=## )", contract) if not (m := re.match(r"## task (\S+)", p)) or m.group(1) == tid)
def test_brief(meta, contract, budget=None, conv=None, rebuilt=False):
    """`rebuilt`: the task's test is on the project branch already (its task passed before a re-plan changed the brief, or the team
    lead sent the collected test back) and `testcrews --redo` starts this writer to replace it."""
    paths = ", ".join(meta.get("protected") or []) or "the test path its brief names"
    args = " ".join(meta.get("protected") or []) or "<the test path>"  # git reads "a.py," as a path, so the command gets no commas
    budget = int(meta.get("test_lines") or budget or TEST_BUDGET)
    files = ", ".join(meta.get("files") or [])
    fails = (f"The test {paths} holds is being replaced (the brief changed after it was written, or the team lead sent it back): rewrite it to "
             f"the TASK BRIEF. It must fail on the project branch for what the brief asks, and pass once {files} implement the brief.\n" if rebuilt else
             f"It must fail on current stubs for the reason this task fixes, and pass once {files} implement the brief.\n")
    # a shared test file is run through a name filter so another task's red cases never fail this one's gate (see sections/3.md)
    scoped = (f"The command has a name filter: name every case so the filter matches it and matches no other task's case, "
              "and run the command once to see the filter picks up each of yours (a run of 0 tests proves nothing).\n" if fc_tasks.name_filter(meta.get("test")) else "")
    return (f"Write the acceptance test for task {meta['id']} ({meta.get('title', '')}) and nothing else.\n"
            f"Test file: {paths}. Test command: {meta.get('test')}\n" + fails + scoped +
            "Use exactly the types, signatures, helpers, and fixtures in the CONTRACT below; never guess a shape it lacks "
            "(report `blocked` naming it).\n"
            "Assert only what the TASK BRIEF states or the code shows: implementer never sees the contract, "
            "so a line or value only it gives is not asserted; a behavior the brief does not ask for (an element that disappears, "
            "a count, a layout) is not asserted either.\n"
            "Find elements by role and accessible name or by the text the brief or design gives, never by a label or class you "
            "guessed; await what renders after a fetch or an effect before asserting on it.\n"
            "Cover the brief's acceptance criteria plus holdout cases it does not list. One file; "
            "test behavior through the interface the brief names, one behavior per case, each named for the behavior it checks, "
            "expected and actual in every failure; never mock what the task owns, and never assert an implementation detail "
            "(a private helper, call order, a log line) the brief does not state; "
            "never a test that asserts dependency lists, lint or tool configuration, or config file contents.\n" +
            ("The TASK BRIEF's `scenarios` block gives one scenario per criterion: write a case per scenario and per negative_case, then holdouts none lists, within the budget.\n" if fc_tasks.scenarios_block(meta.get("body")) else "") +
            f"Budget: at most {budget} lines, holdouts included.\n"
            "You work in your own worktree and branch; the CLI collects every teammate's commits onto the project branch. "
            "Run that command, never the whole suite, confirm it fails for the right reason, format the file, commit only your file: "
            f"`git add {args} && git commit {args} -m \"{fc_core.typed('<what the test covers>', 'test', conv) if conv else '<plain message>'}\"`.\n"
            "To fix your test, make another plain commit. Never run `git commit --amend`, `git reset`, "
            "`git rebase`, `git checkout`, or `git stash`.\n"
            "Report `working \"test ready: <commit>; holdouts: <what they cover>\"` and end the turn: compose collects it and records your `done`; never report `done`.\n\n"
            f"TASK BRIEF:\n{meta.get('body', '')}\n\nCONTRACT:\n{contract_slice(contract, meta['id'])}")

def cmd_testcrews(a):
    """Start one test-writing teammate per task that has a protected test, in parallel, each briefed with its task and the groundwork's shared contract.
    `--print` shows the briefs instead. `--redo` takes passed tasks too, as `run --redo` rebuilds them: a re-plan that changed
    their briefs needs their tests rewritten first. `--parallel N` starts up to N writers now, Claude Code or not, over the config's caps. `--force`: as `crew start --force`."""
    proj, force = fc_core.project_dir(a), bool(getattr(a, "force", False))
    if getattr(a, "collect", False):
        return collect_tests(proj, a.only, force)
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
    rebuilt = {m["id"] for m in metas if redo and (fc_tasks.task_finished(proj, m) or test_collected(proj, m["id"]))}  # its file holds the old test
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
    key, chain = fc_crewpick.crew_chain("tests", model)
    cap = writer_cap(getattr(a, "parallel", None), fc_config.PARALLEL, fc_config.CLAUDE_PARALLEL, key is None or fc_claude.is_claude(chain[0][0]))
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
                                        brief=brief(m), force=force, cwd=str(wts[m["id"]]), leased=True, model=model))
        except (SystemExit, Exception) as e:  # noqa: BLE001  one teammate's failure must not stop the rest, and is never silent
            failed.append(m["id"])
            print(f"tests-{m['id']} not started: {e}")
    failed = []
    with ThreadPoolExecutor(max_workers=len(metas) or 1) as ex:
        list(ex.map(start, metas))
    if failed:
        print(f"not started: {','.join(sorted(failed))}; start them again: compose testcrews --project {proj.name} --only {','.join(sorted(failed))}{again}")
    say_queued()

def writer_cap(parallel, default, claude_cap, claude):
    """How many test writers may be open at once, 0 for no cap. Unset, every writer starts at once; Claude Code writers are heavy:
    they stay within claude_parallel whatever `parallel` is, unless --parallel (`parallel`) says otherwise."""
    cap = parallel or default
    return min(cap or claude_cap, claude_cap) if not parallel and claude else cap

def split_at_cap(metas, cap, held):
    """`metas` split into the writers to start now and those queued, when `held` other writers already hold slots of the `cap`."""
    if not cap:
        return metas, []
    room = max(cap - held, 0)
    return metas[:room], metas[room:]

def cap_split(proj, tasks, metas, cap):
    """`metas` split into the writers to start now and those queued past `cap`. A writer whose branch is open is still writing
    or not collected yet, and holds a slot: the cap counts those too, so --collect never runs more than `cap` at once."""
    ids = {m["id"] for m in metas}
    return split_at_cap(metas, cap, sum(test_open(proj, o["id"]) for o in tasks if o["id"] not in ids) if cap else 0)

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

def written_files(paths, test, missing):
    """The files of `paths` a writer writes: those its `test` command runs by stem (all of them when it names none), and those `missing` from the branch."""
    ran = [p for p in paths if re.search(rf"(?<![\w-]){re.escape(Path(p).stem)}(?![\w-])", test)]
    return set(ran or paths) | set(missing)

def test_written(meta, wt):
    """The protected files a task's test writer writes: those not on the project branch yet, and those its test command runs
    (named by path, file name, or module), or every one when the command names none. A file already there that the command
    does not run (groundwork's pins, say) is only read."""
    paths = meta.get("protected") or []
    return written_files(paths, meta.get("test") or "", {p for p in paths if fc_core.sh(["git", "cat-file", "-e", f"HEAD:{p}"], wt, check=False).returncode})

def test_blockers(proj, tasks, starting):
    """The writers each task in `starting` waits for: of the tasks writing one of the files it writes, those whose branch is not
    collected yet, and earlier ones that start now or wait. Started together off one commit, two writers of one file each created
    it, or each appended to it, and the second collect conflicted; one after another, each starts off the project branch that
    holds the test before it. A task whose own branch is open started long ago and waits for nothing."""
    wt = fc_herdr.project_worktree(proj)
    return writer_blockers([m["id"] for m in starting], [o["id"] for o in tasks], {o["id"]: test_written(o, wt) for o in tasks},
                           {m["id"] for m in starting} | set(waiting_writers(proj)), lambda t: test_open(proj, t))

def writer_blockers(starting, ids, written, queued, is_open):
    """Writers (task ids) each of `starting` waits for: of `ids`, those writing a file it writes that are open, or earlier and in `queued`.
    A writer whose own branch is open (`is_open` asks) waits for nothing."""
    out = {}
    for m in starting:
        by = [o for o in ids if o != m and written[m] & written[o] and ((o < m and o in queued) or is_open(o))]
        if by and not is_open(m):
            out[m] = by
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

def not_ready_line(tid, last):
    """The skip line for a writer whose last report is not test ready."""
    return f"tests-{tid} skipped: it has not reported test ready (last: {last.get('state', 'nothing')} {last.get('message', '')!r}); its branch and worktree are kept"

def to_pick(picked, log):
    """The commits to cherry-pick, oldest first: the shas of `log` lines (sha NUL who) whose `who` is not on a `picked` line of the project branch."""
    seen = {l.split("\0")[1] for l in picked}
    return [c for c, who in (l.split("\0") for l in log) if who not in seen]

def conflict_line(tid, sha, branch, out):
    """The conflict report naming the writer's task, the commit, and the branch kept, with the tail of git's output."""
    return f"tests-{tid}: {sha[:7]} conflicts with the project branch; its branch {branch} is kept.\n{out[-600:]}"

def foreign_writes(files, tid, tasks):
    """{file: owners} for the `files` a writer of task `tid` committed that another task protects and its own task does not. A test
    commit goes to the layer of the task whose `protected:` it fills, so one written into another task's file lands in that task's
    layer and fails its `test` there, where the code it tests does not exist yet (stack --submit is the first to run it)."""
    mine = set(next((m.get("protected") or [] for m in tasks if m["id"] == tid), []))
    owners = {f: sorted(m["id"] for m in tasks if m["id"] != tid and f in (m.get("protected") or [])) for f in sorted(set(files) - mine)}
    return {f: o for f, o in owners.items() if o}

def foreign_line(tid, branch, foreign):
    """The collect report for a writer that wrote into other tasks' test files: its branch is kept and nothing of it is collected."""
    wrote = "; ".join(f"{f} (protected by task {', '.join(o)})" for f, o in foreign.items())
    return (f"tests-{tid}: nothing collected; it wrote {wrote}, not its own `protected:` file. Its branch {branch} is kept: "
            f"give task {tid} a test file of its own (re-plan its `protected:`) or have the writer move the cases into it, then collect again")

def collect_tests(proj, only=None, force=False):
    """Cherry-pick the own commits of every test-writing teammate that reported its test ready onto the project branch in task order,
    remove their worktrees and branches, then start the writers that waited for them. A writer not yet ready, or on a turn again, is skipped: returning
    its slot ends the session still in it. A conflict keeps that writer's branch and worktree for a look, the rest are still
    collected, and the collect exits naming every conflict. `force` goes on to the writers it starts (a low battery)."""
    wt = fc_herdr.project_worktree(proj)
    (proj / "results").mkdir(parents=True, exist_ok=True)
    with open(proj / "results" / "collect.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # two writers ready at once (each report starts a collect) cherry-pick one after the other
        return collect_tests_locked(proj, wt, only, force)

def collect_tests_locked(proj, wt, only, force):
    want = {x.strip() for x in only.split(",")} if only else None
    everyone = fc_tasks.load_tasks(proj, required=True)
    by_id = {m["id"]: m for m in everyone if not want or m["id"] in want}
    ids = sorted(by_id)
    conflicts = []
    for tid in ids:
        branch = test_branch(proj, tid)
        if not test_open(proj, tid):
            continue
        writer = fc_herdr.agent_name(proj.name, f"tests-{tid}")
        last = (fc_crew.crew_reports(proj, writer) or [{}])[-1]
        if not test_ready(last):
            fc_core.say(not_ready_line(tid, last))
            continue
        if fc_testred.gate(proj, by_id[tid], writer):  # a test that cannot load goes back to its writer once
            continue
        # a commit already on the branch is skipped: the same patch, or picked by hand (a cherry-pick keeps the author, the author
        # date, and the subject through a conflict resolution and any later edit, where no patch or content check would match)
        fmt = ["--no-merges", "--date=raw", "--format=%H%x00%an <%ae> %ad %s"]
        picked = fc_core.sh(["git", "log", *fmt, f"{branch}..HEAD"], wt).stdout.splitlines()
        ahead = fc_core.sh(["git", "rev-list", "--count", "--no-merges", f"HEAD..{branch}"], wt).stdout.strip()
        commits = to_pick(picked, fc_core.sh(["git", "log", "--reverse", "--right-only", "--cherry-pick", *fmt, f"HEAD...{branch}"], wt).stdout.splitlines())
        touched = [f for c in commits for f in fc_core.sh(["git", "show", "--no-renames", "--name-only", "--format=", c], wt).stdout.split()]
        if foreign := foreign_writes(touched, tid, everyone):
            conflicts.append(foreign_line(tid, branch, foreign))
            continue
        for c in commits:
            p = fc_core.sh(["git", "cherry-pick", "--allow-empty", c], wt, check=False)
            if p.returncode:
                fc_core.sh(["git", "cherry-pick", "--abort"], wt, check=False)
                conflicts.append(conflict_line(tid, c, branch, p.stdout + p.stderr))
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
            fc_crew.close_collected(proj, writer)  # its tab would stay open and idle until a teardown
            fc_core.say(f"tests-{tid} collected  {len(commits)} commit(s)" + (f", {int(ahead) - len(commits)} already on the branch" if int(ahead) > len(commits) else ""))
    fc_core.sh(["git", "worktree", "prune"], wt, check=False)
    groups = {}
    for tid, how in waiting_writers(proj).items():
        groups.setdefault((how["contract"], how["budget"], bool(how.get("redo"))), []).append(tid)
    try:
        for (contract, budget, redo), tids in groups.items():
            cmd_testcrews(argparse.Namespace(repo=str(wt), project=proj.name, contract=contract, only=",".join(tids), budget=budget, print_=False, collect=False,
                                             redo=redo, force=force))
    finally:  # slots kept for writers that did not start (still blocked, failed, or past the cap) go back to the pool
        for role, held in {k: v for k, v in fc_worktree.slots(proj).items() if k.startswith("spare-")}.items():
            if fc_worktree.release_slot(wt, held):
                fc_worktree.slots(proj, {role: None})
    if conflicts:
        sys.exit("\n".join(conflicts))
