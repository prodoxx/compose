import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

SETTLED = "## Not yet specified\n\nNone\n\n### Codebase brief\n\nWhere things live.\n\n## Plan\n\nThe plan."
STORY = {"id": "ENG-2", "title": "Story", "description": "## Acceptance criteria\n\n* it works", "status": "Todo", "blockedBy": []}
# What `compose tasks` printed before file_clashes was pulled out of it, plus the route column.
TASKS_OUT = """project p   3 tasks   stack order (bottom first): 001 <- 002 <- 003
  id   route                             title                                    after      linear    files | protected | test
  001  STANDARD test-first               T                                        -          ENG-3     a.go | 001_test.go | t
  002  -                                 T                                        -          ENG-4     a.go | 002_test.go | t
  003  DEEP fix-then-regression-test     T                                        001        ENG-5     b.go | 003_test.go | t

  WARNING two tasks own the same file (they must be one task, or one must be `after:` the other):
    a.go: 001, 002

  run waves (each wave in parallel): 001, 002  then  003

  WARNING SERIAL CHAIN: 3 tasks run in 2 waves, mostly one after another. Keep an `after:` (blocked-by) only where a task needs another task's code or file; the stack puts a task above its `after:` tasks and orders the rest by id, so review order needs no `after:`.
"""


def task(ident, tid, blocked_by=(), crit="- works", **kv):
    kv = {"id": tid, "files": f"{tid}.go", "protected": f"{tid}_test.go", "test": "t", **kv}
    desc = f"## Acceptance criteria\n{crit}\n\n```compose\n" + "\n".join(f"{k}: {v}" for k, v in kv.items()) + "\n```\n"
    return {"id": ident, "title": "T", "description": desc, "status": "Todo", "blockedBy": list(blocked_by), "parentId": "ENG-2"}


def routed(issue, *bullets):
    """The issue with a Route section of these bullets right above its compose block."""
    section = "## Route\n\n" + "\n".join(bullets) + "\n\n"
    return {**issue, "description": issue["description"].replace("```compose", section + "```compose")}


def data(issues, desc=SETTLED):
    return {"feature": {"id": "ENG-1", "title": "F", "description": desc}, "issues": [STORY, *issues]}


def check(issues, desc=SETTLED):
    return {i: (ok, level, gaps) for ok, i, level, gaps in fc.fc_spec.spec_check(data(issues, desc))}


def has(gaps, *kws):
    return any(k in g for g in gaps for k in kws)


class SpecCheck(unittest.TestCase):
    def run_cli(self, stdin=None, **kw):
        """The printed lines and the SystemExit (SystemExit(0) when it returns normally); no stdin reads the synced cache."""
        with mock.patch.object(sys, "stdin", io.StringIO(stdin or "")), mock.patch("sys.stdout", io.StringIO()) as out:
            try:
                fc.fc_spec.cmd_spec_check(SimpleNamespace(**{"repo": ".", "project": None, **kw}, stdin=stdin is not None))
            except SystemExit as e:
                return out.getvalue().splitlines(), e
        return out.getvalue().splitlines(), SystemExit(0)

    def test_settled_feature_prints_only_pass_and_skips_canceled_issues(self):
        canceled = {**STORY, "id": "ENG-9", "description": "", "status": "Canceled"}
        lines, e = self.run_cli(json.dumps(data([task("ENG-3", "001"), canceled])))
        self.assertIn(e.code, (0, None))
        self.assertEqual(lines[0], "PASS ENG-1 feature")
        self.assertEqual(sorted(lines[1:]), ["PASS ENG-2 story", "PASS ENG-3 task"])

    def test_fail_line_exits_1_and_bad_stdin_names_the_shape(self):
        lines, e = self.run_cli(json.dumps(data([], "## Not yet specified\n\n- which color?")))
        self.assertEqual(e.code, 1)
        self.assertRegex(lines[0], r"^FAIL ENG-1 feature: [^;]+; [^;]+; [^;]+$")
        self.assertIn(fc.fc_tasks.SYNC_SHAPE, str(self.run_cli("not json")[1]))

    def test_command_line_registers_spec_check(self):
        def cli(desc):
            return subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "compose.py"), "spec-check", "--stdin"],
                                  input=json.dumps(data([task("ENG-3", "001")], desc)), capture_output=True, text=True, env={**os.environ, "COMPOSE_NO_TREEHOUSE": "1"})
        ok, bad = cli(SETTLED), cli("## Not yet specified\n\n- which color?")
        self.assertEqual((ok.returncode, ok.stdout.partition("\n")[0]), (0, "PASS ENG-1 feature"), ok.stderr)
        self.assertEqual(bad.returncode, 1, bad.stderr)
        self.assertTrue(bad.stdout.startswith("FAIL ENG-1 feature"), bad.stdout)

    def test_each_feature_gap_alone(self):
        for desc, kw in ((SETTLED.replace("None", "- which color?"), "Not yet specified"),
                         (SETTLED.replace("### Codebase brief", ""), "Codebase brief"),
                         (SETTLED.replace("## Plan", ""), "Plan")):
            ok, level, gaps = check([task("ENG-3", "001")], desc)["ENG-1"]
            self.assertEqual((ok, level, len(gaps), has(gaps, kw)), (False, "feature", 1, True), desc)

    def test_none_in_not_yet_specified_ignores_case_period_and_whitespace(self):
        for none in ("None.", "none", "NONE.", "  None  ", "None ."):
            ok, level, gaps = check([task("ENG-3", "001")], SETTLED.replace("None", none, 1))["ENG-1"]
            self.assertEqual((ok, gaps), (True, []), none)

    def test_tasks_chained_by_blocked_by_may_share_a_file(self):
        issues = [task("ENG-3", "001", files="a.go"), task("ENG-4", "002", ["ENG-3"], files="a.go"), task("ENG-5", "003", ["ENG-4"], files="a.go")]
        self.assertFalse(has(check(issues)["ENG-1"][2], "a.go"))

    def test_story_and_task_gaps(self):
        r = check([task("ENG-3", "001"), task("ENG-4", "002", files="x.go, y.go, z.go"), task("ENG-5", "003", crit="- a\n" * 6),
                   task("ENG-6", "004", files=""), task("ENG-7", "005", protected=""),
                   task("ENG-9", "007", crit="- prints the line\n\nSignatures and exact lines come from the contract."),
                   {**STORY, "id": "ENG-10"}, {**STORY, "id": "ENG-11", "description": "## Acceptance criteria\n\nTBD"}])
        self.assertEqual((r["ENG-1"][0], r["ENG-2"][0], r["ENG-3"]), (True, True, (True, "task", [])))  # its test is not written yet
        for i, *kws in (("ENG-4", "owns 3 files"), ("ENG-5", "Acceptance criteria"), ("ENG-6", "files"), ("ENG-7", "protected"),
                        ("ENG-9", "implementers never see"), ("ENG-10", "task"), ("ENG-11", "Acceptance criteria")):
            self.assertTrue(not r[i][0] and has(r[i][2], *kws), (i, r[i]))
        self.assertTrue(check([{**STORY, "id": "ENG-10"}])["ENG-10"][0])  # no issue has parentId: a story needs no task
        bare, marked = {**STORY, "id": "ENG-12"}, {**STORY, "id": "ENG-13", "description": STORY["description"] + "\n\nGroundwork: dependency bump"}
        r = check([task("ENG-3", "001"), bare, marked])
        self.assertTrue(not r["ENG-12"][0] and has(r["ENG-12"][2], "task"), r["ENG-12"])
        self.assertTrue(not r["ENG-13"][0] and has(r["ENG-13"][2], "task"), r["ENG-13"])  # a Groundwork line never excuses a story

    def test_criteria_fold_nested_bullets_and_continuations(self):
        parse = lambda body: fc.fc_tasks.parse_criteria("## Acceptance criteria\n" + body)
        self.assertEqual(parse("- a\n* b\n"), ["a", "b"])
        for pad in ("  ", "    "):
            self.assertEqual(parse(f"* Sheet shows one example per role:\n{pad}* primary\n{pad}* ghost\n* other"),
                             ["Sheet shows one example per role: primary; ghost", "other"])
        self.assertEqual(parse("* wraps onto\n  a second line\n* next"), ["wraps onto a second line", "next"])
        self.assertEqual(parse("  - indented base\n  - same level\n    - child"), ["indented base", "same level; child"])

    def test_size_lint_counts_top_level_bullets_only(self):
        sub = "".join(f"  * s{n}\n" for n in range(6))
        self.assertFalse(has(check([task("ENG-3", "001", crit=f"* parent:\n{sub}* two")])["ENG-3"][2], "Acceptance criteria"))

    def test_lockfiles_never_count_toward_the_file_cap(self):
        got = check([task("ENG-3", "001", files="server/go.mod, server/go.sum, server/db/migrate.go"),
                     task("ENG-4", "002", files="app/package.json, app/package-lock.json, app/src/session.ts"),
                     task("ENG-5", "003", files="Cargo.toml, Cargo.lock, src/a.rs, src/b.rs")])
        self.assertEqual((got["ENG-3"], got["ENG-4"]), ((True, "task", []), (True, "task", [])))
        self.assertTrue(has(got["ENG-5"][2], "owns 3 files"), got["ENG-5"])

    def test_serial_chain_and_file_clashes(self):
        chain = [task("ENG-3", "001"), task("ENG-4", "002", ["ENG-3"]), task("ENG-5", "003", [{"id": "ENG-4", "title": "T"}])]
        self.assertTrue(has(check(chain)["ENG-1"][2], "wave"))
        self.assertEqual(fc.fc_spec.file_clashes([]), {})
        tasks = [{"id": "001", "files": ["a.go"]}, {"id": "002", "files": ["a.go", "b.go"]}, {"id": "003", "files": ["c.go"]}]
        self.assertEqual(fc.fc_spec.file_clashes(tasks), {"a.go": ["001", "002"]})
        two = [task("ENG-3", "001", files="a.go"), task("ENG-4", "002", files="a.go"),
               task("ENG-5", "003", files="b.go"), task("ENG-6", "004", files="b.go")]
        g, = [g for g in check(two)["ENG-1"][2] if "a.go" in g]
        self.assertTrue("a.go: 001, 002" in g and "b.go: 003, 004" in g, g)
        self.assertEqual(g.count(g[:min(g.index("a.go"), g.index("b.go"))]), 1, g)  # the explanation leads, once

    def test_a_task_naming_code_another_task_owns_needs_an_after_on_it(self):
        miss = lambda *t: fc.fc_spec.missing_after(list(t))
        shrink = {"id": "001", "files": ["bin/fc_shrink.py"], "body": "", "test": "t"}
        cmd = {"id": "002", "files": ["bin/fc_shrink.py"], "body": "", "test": "t", "after": "001"}
        jev = {"id": "004", "files": ["bin/fc_jev.py"], "body": "Call `fc_shrink.shrink(p, tmp)` on every jpg.", "test": "t"}
        self.assertEqual(miss(shrink, cmd, jev), {"004": {"001": ["bin/fc_shrink.py"]}})  # the first owner, not the one that adds on top of it
        for after in ("001", "002"):  # a path to any owner, direct or through another task, orders it
            self.assertEqual(miss(shrink, cmd, dict(jev, after=after)), {})
        self.assertEqual(miss(dict(shrink, after="004"), jev), {})  # the owner waits for it instead
        for body in ("import fc_shrink", "see bin/fc_shrink.py", "see bin/fc_shrink and fc_shrink.shrink"):
            self.assertEqual(miss(shrink, dict(jev, body=body)), {"004": {"001": ["bin/fc_shrink.py"]}}, body)
        self.assertEqual(miss(shrink, dict(jev, body="", test="python3 -m unittest tests.test_x; python3 bin/fc_shrink.py")), {"004": {"001": ["bin/fc_shrink.py"]}})
        for body in ("see bin/fc_shrink_extra.py and old/bin/fc_shrink.py", "the shrink step", "fc_shrinker.run(x)"):  # a lookalike names nothing
            self.assertEqual(miss(shrink, dict(jev, body=body)), {}, body)
        self.assertEqual(miss(shrink, dict(jev, files=["bin/fc_shrink.py"])), {})  # the same file is the file clash's to report
        self.assertEqual(miss(dict(shrink, files=["app/package-lock.json"]), dict(jev, body="app/package-lock.json")), {})  # generated
        self.assertEqual(miss(dict(shrink, kind="design"), jev), {})  # a design task builds a page, not code
        self.assertEqual(fc.fc_spec.missing_after_text(miss(shrink, jev)), ["004 names bin/fc_shrink.py, which 001 owns: add `after: 001`"])

    def test_spec_check_and_tasks_say_which_after_a_task_is_missing(self):
        brief = lambda **kv: task("ENG-4", "002", files="b.go", crit="- calls `a.go` Run", **kv)
        owner = task("ENG-3", "001", files="a.go")
        self.assertEqual(check([owner, brief()])["ENG-4"], (False, "task", ["002 names a.go, which 001 owns: add `after: 001`"]))
        self.assertEqual(check([owner, brief(), task("ENG-5", "003", ["ENG-3"], files="c.go", crit="- calls `a.go` Run")])["ENG-5"], (True, "task", []))
        self.assertEqual(check([owner, task("ENG-4", "002", ["ENG-3"], files="b.go", crit="- calls `a.go` Run")])["ENG-4"], (True, "task", []))
        repo = Path(tempfile.mkdtemp())
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True, text=True)
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "init")
        proj = repo / ".compose" / "p"
        (proj / "results").mkdir(parents=True)
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        def printed(*issues):
            fc.fc_tasks.sync_tasks(proj, data(list(issues)))
            out = io.StringIO()
            with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch("sys.stdout", out):
                fc.fc_spec.cmd_tasks(SimpleNamespace(repo=str(repo), project="p"))
            return out.getvalue()
        self.assertIn("WARNING a task names code another task owns, with no `after:` on it", printed(owner, brief()))
        self.assertIn("\n    002 names a.go, which 001 owns: add `after: 001`\n", printed(owner, brief()))
        self.assertNotIn("names code another task owns", printed(owner, task("ENG-4", "002", ["ENG-3"], files="b.go", crit="- calls `a.go` Run")))

    def test_serial_chain_counts_only_tasks_left_to_run(self):
        chain = [{**task("ENG-3", "001"), "status": "In Review"}, {**task("ENG-4", "002", ["ENG-3"]), "status": "Done"},
                 task("ENG-5", "003", ["ENG-4"])]
        self.assertTrue(check(chain)["ENG-1"][0], check(chain)["ENG-1"])
        repo = Path(tempfile.mkdtemp())
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "init")
        proj = repo / ".compose" / "p"
        (proj / "results").mkdir(parents=True)
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        fc.fc_tasks.sync_tasks(proj, data([task("ENG-3", "001"), task("ENG-4", "002", ["ENG-3"]), task("ENG-5", "003", ["ENG-4"])]))
        for tid in ("001", "002"):  # passed layers, their branches not merged yet
            git("branch", f"fast/p/{tid}", git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", tid))
            (proj / "results" / f"{tid}.json").write_text('{"status": "passed"}')
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch("sys.stdout", out):
            fc.fc_spec.cmd_tasks(SimpleNamespace(repo=str(repo), project="p"))
        self.assertIn("run waves (each wave in parallel): 003\n", out.getvalue())
        self.assertNotIn("SERIAL CHAIN", out.getvalue())

    def test_tasks_prints_the_stack_order_the_chain_builds_with_a_task_above_a_higher_id_it_is_after(self):
        """A re-plan made 012 and 013 wait on 025: `tasks` stacks them above it, before and after their branches exist, as the chain
        does, and a merged task is no layer."""
        repo = Path(tempfile.mkdtemp())
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "init")
        proj = repo / ".compose" / "p"
        (proj / "results").mkdir(parents=True)
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        fc.fc_tasks.sync_tasks(proj, data([task("ENG-3", "011"), task("ENG-4", "012", ["ENG-3", "ENG-7"]), task("ENG-5", "013", ["ENG-3", "ENG-7"]),
                                           task("ENG-6", "024"), task("ENG-7", "025", ["ENG-6"]), task("ENG-8", "026", files="z.go")]))
        def header():
            out = io.StringIO()
            with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch("sys.stdout", out), contextlib.suppress(SystemExit):  # until a layer passes, this plan is a serial chain
                fc.fc_spec.cmd_tasks(SimpleNamespace(repo=str(repo), project="p"))
            return out.getvalue().splitlines()[0].partition("stack order (bottom first): ")[2]
        git("branch", "fast/p/026", git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "026"))
        git("merge", "-q", "fast/p/026")
        (proj / "results" / "026.json").write_text('{"status": "passed"}')
        self.assertEqual(header(), "011 <- 024 <- 025 <- 012 <- 013")  # no layer is built yet
        for tid in ("011", "012", "013", "024", "025"):
            git("branch", f"fast/p/{tid}", git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", tid))
            (proj / "results" / f"{tid}.json").write_text('{"status": "passed"}')
        chain = [b.removeprefix("fast/p/") for b in fc.fc_worktree.stack_order(repo, proj)]
        self.assertEqual((header(), " <- ".join(chain)), ("011 <- 024 <- 025 <- 012 <- 013",) * 2)

    def clash_tasks(self, cache=None):
        """`compose tasks` of three synced tasks, two sharing a.go, 001 and 003 routed; `cache` edits tasks.json first."""
        clash = [routed(task("ENG-3", "001", files="a.go"), "* workflow: test-first", "* depth: STANDARD", "* decided by: jev"),
                 task("ENG-4", "002", files="a.go"),
                 routed(task("ENG-5", "003", ["ENG-3"], files="b.go"), "- depth: DEEP", "- decided by: fallback", "- workflow: fix-then-regression-test")]
        repo = Path(tempfile.mkdtemp())
        for a in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *a], cwd=repo, check=True)
        proj = repo / ".compose" / "p"
        proj.mkdir(parents=True)
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        fc.fc_tasks.sync_tasks(proj, data(clash, ""))
        if cache:
            path = proj / "results" / "tasks.json"
            path.write_text(json.dumps(cache(json.loads(path.read_text()))))
        out = io.StringIO()
        self.exit_code = 0
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch("sys.stdout", out):
            try:
                fc.fc_spec.cmd_tasks(SimpleNamespace(repo=str(repo), project="p"))
            except SystemExit as e:
                self.exit_code = e.code
        return clash, proj, out.getvalue()

    def test_shared_file_in_spec_check_and_tasks_and_sync_keeps_parent(self):
        clash, proj, out = self.clash_tasks()
        self.assertEqual(out, TASKS_OUT)
        self.assertEqual(fc.fc_tasks.linear_cache(proj)["issues"]["ENG-3"]["parentId"], "ENG-2")
        self.assertTrue(has(check(clash)["ENG-1"][2], "a.go"))

    def test_tasks_exits_1_on_a_serial_chain_after_printing_it(self):
        _, _, out = self.clash_tasks()
        self.assertIn("WARNING SERIAL CHAIN", out)
        self.assertEqual(self.exit_code, 1)

    def test_route_is_a_dash_for_an_issue_missing_from_the_cache_or_a_cache_without_issues(self):
        wide = lambda r: f"{r:33}"
        _, _, out = self.clash_tasks(lambda c: {**c, "issues": {k: v for k, v in c["issues"].items() if k != "ENG-5"}})
        self.assertEqual(out, TASKS_OUT.replace(wide("DEEP fix-then-regression-test"), wide("-")))
        _, _, out = self.clash_tasks(lambda c: {k: v for k, v in c.items() if k != "issues"})
        self.assertEqual(out, TASKS_OUT.replace(wide("DEEP fix-then-regression-test"), wide("-")).replace(wide("STANDARD test-first"), wide("-")))

    def test_unsynced_project_stops_with_the_sync_message(self):
        repo = Path(tempfile.mkdtemp())
        proj = repo / ".compose" / "p"
        proj.mkdir(parents=True)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_herdr, "project_worktree", return_value=repo):
            lines, e = self.run_cli(repo=str(repo), project="p")
        self.assertIn("no tasks synced for p", str(e))
        self.assertIn("compose sync", str(e))

    def test_synced_cache_prints_what_stdin_prints(self):
        d = data([task("ENG-3", "001"), {**STORY, "id": "ENG-11", "description": "TBD"}])
        repo = Path(tempfile.mkdtemp())
        proj = repo / ".compose" / "p"
        proj.mkdir(parents=True)
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        fc.fc_tasks.sync_tasks(proj, d)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj):
            lines, e = self.run_cli(repo=str(repo), project="p")
        want, we = self.run_cli(json.dumps(d))
        self.assertTrue(any(l.startswith("FAIL ENG-11") for l in want), want)
        self.assertEqual((lines, e.code), (want, we.code))

    def test_a_task_whose_test_is_a_verify_check_runs_the_whole_suite_and_fails(self):
        d = data([task("ENG-3", "001", test="go  test ./..."), task("ENG-4", "002", test="go test ./b -run '^TestB'")])
        repo = Path(tempfile.mkdtemp())
        proj = repo / ".compose" / "p"
        proj.mkdir(parents=True)
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        (proj / "verify.json").write_text(json.dumps({"checks": ["go vet ./...", "go test ./..."]}))
        fc.fc_tasks.sync_tasks(proj, d)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj):
            lines, e = self.run_cli(repo=str(repo), project="p")
        self.assertEqual(e.code, 1)
        self.assertIn("PASS ENG-4 task", lines)
        bad, = [l for l in lines if l.startswith("FAIL ENG-3 task")]
        self.assertIn("`test:` is the verify check `go  test ./...`, the whole suite: run only this task's test", bad)
        self.assertIn("PASS ENG-3 task", self.run_cli(json.dumps(d))[0])  # stdin has no verify.json to compare with


class DeleteLine(unittest.TestCase):
    """Task 013 (NEB-934): delete: parsed from the compose block; spec-check and the 2-file cap with delete: paths."""

    def test_delete_parses_like_files_and_defaults_to_empty(self):
        parse = lambda **kv: fc.fc_tasks.task_from_issue(task("ENG-3", "001", **kv))["delete"]
        self.assertEqual(parse(delete="a.py, old/"), ["a.py", "old/"])
        self.assertEqual(parse(delete=" a.py ,, old/ , "), ["a.py", "old/"])
        self.assertEqual(parse(), [])
        self.assertEqual(parse(delete=""), [])

    def test_a_delete_only_task_passes_and_one_with_neither_still_fails(self):
        got = check([task("ENG-3", "001", files="", delete="old.go, old/"), task("ENG-4", "002", files=""),
                      task("ENG-5", "003", files="", delete="")])
        self.assertEqual(got["ENG-3"], (True, "task", []))
        for i in ("ENG-4", "ENG-5"):
            self.assertFalse(got[i][0])
            self.assertTrue(has(got[i][2], "empty `files:`"), got[i])

    def test_a_delete_only_task_still_reports_its_other_gaps(self):
        ok, _, gaps = check([task("ENG-3", "001", files="", protected="", delete="old.go")])["ENG-3"]
        self.assertFalse(ok)
        self.assertTrue(has(gaps, "empty `protected:`"), gaps)
        self.assertFalse(has(gaps, "empty `files:`"), gaps)

    def test_delete_paths_never_count_toward_the_file_cap(self):
        many = ", ".join(f"old/{n}.go" for n in range(6))
        got = check([task("ENG-3", "001", files="a.go, b.go", delete=many), task("ENG-4", "002", files="c.go, d.go, e.go", delete=many)])
        self.assertEqual(got["ENG-3"], (True, "task", []))
        self.assertTrue(has(got["ENG-4"][2], "owns 3 files"), got["ENG-4"])
        meta = fc.fc_tasks.task_from_issue(task("ENG-3", "001", files="", delete=many))
        self.assertEqual((len(meta["delete"]), fc.fc_spec.too_big(meta)), (6, None))


def story(ident, n, status="Todo", sub=""):
    crit = "\n".join(f"* c{i}{sub}" for i in range(n))
    return {"id": ident, "title": "All the screens, every flow", "description": f"## Acceptance criteria\n\n{crit}", "status": status, "blockedBy": []}


class StoryTooBig(unittest.TestCase):
    """Task 001 (NEB-1266): a story with more than 5 criteria fails spec-check and is warned about in `compose tasks`."""

    def issues(self, *stories):  # each story gets a child task, so "no task" is never one of its gaps
        return [x for n, s in enumerate(stories) for x in (s, {**task(f"T-{n}", f"{n:03}"), "parentId": s["id"]})]

    def test_spec_check_fails_a_story_over_five_criteria_naming_its_count(self):
        gap = lambda n: f"too big: {n} criteria under `## Acceptance criteria` (max 5)"
        issues = self.issues(story("ENG-20", 7), story("ENG-24", 6), story("ENG-22", 5), story("ENG-23", 5, sub="\n  * part\n  * more"),
                             story("ENG-26", 9, "Canceled"))
        issues.append(task("ENG-27", "009", crit="\n".join(f"- c{i}" for i in range(7))))
        got = check(issues)
        self.assertEqual(got["ENG-20"], (False, "story", [gap(7)]))
        self.assertEqual(got["ENG-24"], (False, "story", [gap(6)]))
        for i in ("ENG-22", "ENG-23"):
            self.assertEqual(got[i], (True, "story", []))
        self.assertNotIn("ENG-26", got)
        self.assertFalse(has(got["ENG-27"][2], "criteria under", "too big"), got["ENG-27"])
        lines, e = SpecCheck.run_cli(self, json.dumps(data(issues)))
        self.assertEqual(e.code, 1)
        self.assertIn(f"FAIL ENG-20 story: {gap(7)}", lines)

    def tasks_out(self, issues):
        repo = Path(tempfile.mkdtemp())
        for a in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *a], cwd=repo, check=True)
        proj = repo / ".compose" / "p"
        proj.mkdir(parents=True)
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        fc.fc_tasks.sync_tasks(proj, data(issues))
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch("sys.stdout", out):
            fc.fc_spec.cmd_tasks(SimpleNamespace(repo=str(repo), project="p"))
        return out.getvalue()

    def test_tasks_warns_each_story_over_five_in_cache_order_and_skips_canceled_and_tasks(self):
        many = "\n".join(f"- c{i}" for i in range(7))
        out = self.tasks_out(self.issues(story("ENG-30", 8), story("ENG-22", 5), story("ENG-26", 9, "Canceled"), story("ENG-20", 6))
                            + [task("ENG-27", "009", crit=many)])
        self.assertIn("\n\n  WARNING STORY TOO BIG: split it into thinner stories before cutting its tasks:\n"
                      "    ENG-30: 8 criteria (max 5)\n    ENG-20: 6 criteria (max 5)\n", out)
        self.assertNotIn("ENG-26:", out)
        self.assertNotIn("ENG-27:", out)
        self.assertNotIn("STORY TOO BIG", self.tasks_out(self.issues(story("ENG-22", 5), story("ENG-26", 9, "Canceled"))))


class DesignAndHandTasks(unittest.TestCase):
    """A design task names `nodes:`, never `files:` or `protected:`; a Task-labelled issue without a block is a hand task, never a story."""

    def test_design_task_is_checked_for_nodes_not_files(self):
        design = lambda ident, tid, nodes: task(ident, tid, files="", protected="", kind="design", nodes=nodes,
                                                test=f"compose figma judge --project p --only {tid}")
        got = check([design("ENG-3", "001", "Primitives/Color"), design("ENG-4", "002", "")])
        self.assertEqual(got["ENG-3"], (True, "task", []))
        self.assertEqual(got["ENG-4"], (False, "task", ["empty `nodes:`"]))

    def test_task_labelled_issue_without_a_block_is_a_hand_task_of_its_story(self):
        lowfi = {**story("ENG-5", 6, "Done"), "labels": ["Task"], "parentId": "ENG-4"}
        got = check([task("ENG-3", "001"), {**story("ENG-4", 1), "parentId": "ENG-1"}, lowfi])
        self.assertEqual((got["ENG-4"], got["ENG-5"]), ((True, "story", []), (True, "hand task", [])))
        out = StoryTooBig.tasks_out(self, [task("ENG-3", "001"), lowfi])
        self.assertNotIn("STORY TOO BIG", out)
        lines, e = SpecCheck.run_cli(self, json.dumps(data([task("ENG-3", "001"), lowfi])))
        self.assertIn("PASS ENG-5 hand task", lines)



FULL = "scenario: s\ngiven: g\nwhen: w\nthen: t\ntest_type: unit\noracle: o"


def with_block(issue, *scenarios):
    block = "```scenarios\n" + "\n".join(f"id: {n}\n{s}" for n, s in scenarios) + "\n```"
    return {**issue, "description": issue["description"].replace("```compose", "## Test plan\n\n" + block + "\n\n```compose")}


class ScenarioGaps(unittest.TestCase):
    def gaps(self, *scenarios, crit="- a\n- b"):
        return check([with_block(task("ENG-3", "001", crit=crit), *scenarios)])["ENG-3"]

    def test_a_complete_block_passes_and_negative_cases_are_optional(self):
        self.assertEqual(self.gaps((1, FULL), (2, FULL + "\nnegative_cases: n")), (True, "task", []))

    def test_a_task_without_a_block_gets_no_new_gap(self):
        self.assertEqual(check([task("ENG-3", "001", crit="- a\n- b")])["ENG-3"][2], [])

    def test_missing_and_empty_keys_are_named_in_order_per_scenario(self):
        no_given_oracle = FULL.replace("given: g\n", "").replace("oracle: o", "oracle:")
        ok, _, gaps = self.gaps((1, FULL), (2, no_given_oracle))
        self.assertEqual((ok, gaps), (False, ["scenario 2 lacks given, oracle"]))
        self.assertEqual(self.gaps((1, FULL.replace("scenario: s\n", "")))[2][0], "scenario 1 lacks scenario")

    def test_ids_not_one_to_k_fail_with_the_criteria_count(self):
        ok, _, gaps = self.gaps((1, FULL), (3, FULL))
        self.assertEqual((ok, gaps), (False, ["scenario ids 1, 3 do not match the 2 criteria (expected 1 to 2)"]))
        self.assertEqual(self.gaps((2, FULL), (1, FULL))[2], ["scenario ids 2, 1 do not match the 2 criteria (expected 1 to 2)"])
        self.assertEqual(self.gaps((1, FULL), crit="- a\n- b\n- c")[2], ["scenario ids 1 do not match the 3 criteria (expected 1 to 3)"])

    def test_a_scenario_without_an_id_shows_a_question_mark_and_both_gaps_come_in_order(self):
        gaps = self.gaps((1, FULL), ("", "scenario: s"))[2]
        self.assertEqual(gaps[0], "scenario 2 lacks id, given, when, then, test_type, oracle")
        self.assertEqual(gaps[1], "scenario ids 1, ? do not match the 2 criteria (expected 1 to 2)")
