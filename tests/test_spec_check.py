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


def show_at(rev, files):
    """A fake `sh` that answers only `git show <rev>:<path>`, the one call that reads a file at a revision."""
    def sh(argv, *a, **kw):
        path = argv[2].partition(":")[2] if argv[:2] == ["git", "show"] and argv[2].startswith(rev + ":") else None
        return subprocess.CompletedProcess(argv, 0 if path in files else 128, files.get(path, ""), "")
    return sh


def branches(have=(), merged=()):
    """`compose tasks` and `spec-check` with git's answers about the project's task branches faked: these are the branches that exist and the ones merged."""
    facts = {"prefix": "fast/p/", "have": {f"fast/p/{t}" for t in have}, "merged": {f"fast/p/{t}" for t in merged}}
    return mock.patch.multiple(fc.fc_status, project_head=lambda *a: "head", branch_facts=lambda *a: facts)


def has(gaps, *kws):
    return any(k in g for g in gaps for k in kws)


class SpecCheck(unittest.TestCase):
    def run_cli(self, stdin=None, **kw):
        """The printed lines and the SystemExit (SystemExit(0) when it returns normally); no stdin reads the synced cache."""
        with mock.patch.object(sys, "stdin", io.StringIO(stdin or "")), mock.patch("sys.stdout", io.StringIO()) as out, branches():
            try:
                fc.fc_spec.cmd_spec_check(SimpleNamespace(**{"repo": ".", "project": None, **kw}, stdin=stdin is not None))
            except SystemExit as e:
                return out.getvalue().splitlines(), e
        return out.getvalue().splitlines(), SystemExit(0)

    def test_settled_feature_prints_only_pass_and_skips_canceled_issues(self):
        canceled = {**STORY, "id": "ENG-9", "description": "", "status": "Canceled"}
        done = {**STORY, "id": "ENG-8", "description": "An audit, finished.", "status": "Done"}  # no bullets, no tasks: nothing left to specify
        lines, e = self.run_cli(json.dumps(data([task("ENG-3", "001"), canceled, done])))
        self.assertIn(e.code, (0, None))
        self.assertEqual(lines[0], "PASS ENG-1 feature")
        self.assertEqual(sorted(lines[1:]), ["PASS ENG-2 story", "PASS ENG-3 task"])

    def test_fail_line_exits_1_and_bad_stdin_names_the_shape(self):
        lines, e = self.run_cli(json.dumps(data([], "## Not yet specified\n\n- which color?")))
        self.assertEqual(e.code, 1)
        self.assertRegex(lines[0], r"^FAIL ENG-1 feature: [^;]+; [^;]+; [^;]+$")
        self.assertIn(fc.fc_tasks.SYNC_SHAPE, str(self.run_cli("not json")[1]))

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

    DESIGN = ("## Intent\n\nA shop.\n\n## Goals\n\n- sell\n\n## Non-goals\n\n- rent\n\n## Anti-goals\n\n- ads\n\n## Constraints\n\n- one box\n\n"
              "## Decided by the product owner\n\n- D1\n\n## My calls\n\n- SQLite\n\n## Left to the builder\n\n- names\n\n## Open questions\n\n"
              "## Decisions so far\n\n- D1: one store (product owner, 2026-10-25; said: \"one\")\n"
              "- D2: the design on ENG-1 is approved (product owner, 2026-10-25; said: \"go\")\n\n"
              "### Codebase brief\n\nWhere things live.\n\n## Plan\n\nThe plan.")

    def test_a_brainstormed_design_without_not_yet_specified_is_settled(self):
        ok, level, gaps = check([task("ENG-3", "001")], self.DESIGN)["ENG-1"]
        self.assertEqual((ok, gaps), (True, []))

    def test_an_open_question_left_on_the_design_fails_it(self):
        desc = self.DESIGN.replace("## Open questions\n\n", "## Open questions\n\nWhich payment provider?\n\n")
        ok, level, gaps = check([task("ENG-3", "001")], desc)["ENG-1"]
        self.assertEqual((ok, len(gaps), has(gaps, "Open questions"), has(gaps, "Which payment provider?")), (False, 1, True, True))

    def test_open_questions_under_not_yet_specified_still_fail_and_an_empty_section_is_settled(self):
        held = "## Not yet specified\n\n- Q1: Which auth? - Recommendation: Clerk - every screen signs in\n\n"
        ok, _, gaps = check([task("ENG-3", "001")], held + self.DESIGN)["ENG-1"]
        self.assertEqual((ok, has(gaps, "Not yet specified"), has(gaps, "Q1: Which auth?"), has(gaps, "answer its open questions with `compose decide`")), (False, True, True, True))
        self.assertFalse(has(gaps, "card"))
        card = "## Not yet specified\n\nQ1. Which auth?\nA. Clerk - affects: billing\nB. JWT - affects: work\nRecommendation: A\n\n"
        ok, _, gaps = check([task("ENG-3", "001")], card + self.DESIGN)["ENG-1"]
        self.assertEqual((ok, has(gaps, "Q1. Which auth?")), (False, True))
        ok, _, gaps = check([task("ENG-3", "001")], "## Not yet specified\n\n" + self.DESIGN)["ENG-1"]
        self.assertEqual((ok, gaps), (True, []))

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
        for body in ("see bin/fc_shrink_extra.py and old/bin/fc_shrink.py", "the shrink step", "fc_shrinker.run(x)", "tests in bin/fc_shrink.test.py"):  # a lookalike names nothing
            self.assertEqual(miss(shrink, dict(jev, body=body)), {}, body)
        self.assertEqual(miss(shrink, dict(jev, files=["bin/fc_shrink.py"])), {})  # the same file is the file clash's to report
        for owner, body in (("app/src/lib/api.ts", "returns api.Identity and api.NewRouter"), ("server/api/api.go", "from api import x"), ("app/package.json", "imports from package X")):
            self.assertEqual(miss(dict(shrink, files=[owner]), dict(jev, body=body)), {}, body)  # a bare stem names a module only in Python (stem.name, import stem) and JS/TS (require)
        for owner, body in (("app/src/lib/limit-message.ts", "Delete app/src/lib/limit-message.test.ts."), ("app/src/app/[threadId]/page.tsx", "app/src/app/[threadId]/page.test.tsx")):
            self.assertEqual(miss(dict(shrink, files=[owner]), dict(jev, body=body)), {}, body)  # a test file's path does not name its source
        for owner, body in (("app/src/lib/api.ts", "require('./api')"), ("app/package.json", "see app/package.json"), ("app/src/lib/limit-message.ts", "see app/src/lib/limit-message.")):
            self.assertEqual(miss(dict(shrink, files=[owner]), dict(jev, body=body)), {"004": {"001": [owner]}}, body)
        for owner, body in (("app/src/lib/auth.ts", "see app/src/lib/auth-client.ts"), ("app/.env.example", "set it in app/.env"),
                            ("app/.env", "copy app/.env.example"), ("app/src/lib/auth.ts", "app/src/lib/auth.ts-old")):
            self.assertEqual(miss(dict(shrink, files=[owner]), dict(jev, body=body)), {}, body)  # a hyphen goes on the name; a dotfile's suffix is part of it
        for owner, body in (("app/.env.example", "add it to app/.env.example."), ("app/src/lib/auth.ts", "see app/src/lib/auth - the server half")):
            self.assertEqual(miss(dict(shrink, files=[owner]), dict(jev, body=body)), {"004": {"001": [owner]}}, body)
        self.assertEqual(miss(dict(shrink, files=["app/package-lock.json"]), dict(jev, body="app/package-lock.json")), {})  # generated
        self.assertEqual(miss(dict(shrink, kind="design"), jev), {})  # a design task builds a page, not code
        self.assertEqual(fc.fc_spec.missing_after_text(miss(shrink, jev)), ["004 names bin/fc_shrink.py, which 001 owns: add `after: 001`"])

    def test_a_task_naming_only_code_already_on_the_base_needs_no_after(self):
        base = {"bin/fc_tasks.py": {"load_tasks", "code_tasks"}}
        miss = lambda body: fc.fc_spec.missing_after([owner, dict(user, body=body)], lambda path, name: name in base.get(path, ()))
        owner = {"id": "001", "files": ["bin/fc_tasks.py"], "body": "Add a new helper to fc_tasks.", "test": "t"}
        user = {"id": "002", "files": ["bin/fc_other.py"], "body": "", "test": "t"}
        for body in ("Call fc_tasks.load_tasks (exists on trunk) from the new command.", "from fc_tasks import load_tasks, code_tasks"):
            self.assertEqual(miss(body), {}, body)
        # a name the base lacks is the owner's to add; the path or a bare import says nothing of which part it needs
        for body in ("Call fc_tasks.new_helper.", "fc_tasks.load_tasks and fc_tasks.new_helper", "from fc_tasks import load_tasks, new_helper",
                     "Call fc_tasks.load_tasks in bin/fc_tasks.py", "import fc_tasks; fc_tasks.load_tasks"):
            self.assertEqual(miss(body), {"002": {"001": ["bin/fc_tasks.py"]}}, body)

    def test_package_scripts_reads_what_a_check_runs_and_where(self):
        ps = fc.fc_spec.package_scripts
        self.assertEqual(ps("bun run build-storybook"), [("package.json", "build-storybook")])
        self.assertEqual(ps("cd app && . /x/smoke.env && bun --bun run prisma generate && bun run test:storybook"),
                         [("app/package.json", "prisma"), ("app/package.json", "test:storybook")])
        self.assertEqual(ps("cd ./web; npm run lint || pnpm run e2e"), [("web/package.json", "lint"), ("web/package.json", "e2e")])
        for c in ("bun test", "yarn build", "make build", "cd /abs/app && bun run x", "cd .. && bun run x", "bun --cwd app run x", "FOO=1 bun run x"):
            self.assertEqual(ps(c), [], c)  # nothing it can tell is a script of a package.json it can name

    def test_base_scripts_are_scripts_dependencies_and_installed_bins_at_the_base(self):
        with tempfile.TemporaryDirectory() as d:
            at_base = {"app/package.json": json.dumps({"scripts": {"build": "vite build"}, "devDependencies": {"prisma": "6"}})}
            Path(d, "app").mkdir(); Path(d, "app", "package.json").write_text(json.dumps({"scripts": {"build-storybook": "storybook build"}}))  # the worktree is not the base
            Path(d, "node_modules", ".bin").mkdir(parents=True); Path(d, "node_modules", ".bin", "tsc").write_text("")
            with mock.patch.object(fc.fc_core, "sh", show_at("HEAD", at_base)):
                has = fc.fc_spec.base_scripts(d, "HEAD")
                self.assertEqual([n for n in ("build", "prisma", "tsc") if not has("app/package.json", n)], [])
                self.assertFalse(has("app/package.json", "build-storybook"))
                self.assertFalse(has("web/package.json", "build"))  # no package.json at the base defines nothing

    def test_a_check_running_a_script_the_base_lacks_needs_a_toolchain_task_that_adds_it(self):
        checks = ["bun run build", "bun run build-storybook"]
        scripts = lambda pkg, name: name == "build"
        gaps = lambda issues: fc.fc_spec.spec_check(data(issues), checks, scripts=scripts)[0][3]
        self.assertEqual(gaps([task("ENG-3", "001")]), ["verify check `bun run build-storybook` runs build-storybook, which package.json does not define "
                         "on the base, and no task adds it: put it in the `kind: toolchain` task that sets its tool up, or drop the check"])
        self.assertEqual(gaps([task("ENG-3", "001", files="package.json, bun.lock")]), ["verify check `bun run build-storybook` runs build-storybook, "
                         "which package.json does not define on the base: make the task of 001 that sets its tool up `kind: toolchain`, so its "
                         "layer goes below groundwork and every layer can run the check"])
        self.assertEqual(gaps([task("ENG-3", "001", files="package.json, bun.lock", kind="toolchain")]), [])
        self.assertEqual(fc.fc_spec.spec_check(data([task("ENG-3", "001")]), checks)[0][3], [])  # no base to read says nothing

    def test_the_base_is_read_from_git_at_the_given_revision(self):
        at_base = {"m.py": "import os\nX = 1\nA, (B, C) = 1, (2, 3)\nT: int = 0\n\ndef f():\n    inner = 1\n\nclass K: pass\nasync def g(): pass\n", "bad.py": "def broken(:\n"}
        with mock.patch.object(fc.fc_core, "sh", show_at("HEAD", at_base)):
            defined = fc.fc_spec.base_definitions("d", "HEAD")
            self.assertEqual([n for n in ("X", "A", "B", "C", "T", "f", "K", "g") if not defined("m.py", n)], [])
            for path, name in (("m.py", "inner"), ("m.py", "os"), ("m.py", "later"), ("bad.py", "broken"), ("gone.py", "f")):
                self.assertFalse(defined(path, name), (path, name))
            self.assertFalse(fc.fc_spec.base_definitions("d", "")("m.py", "X"))  # no base revision defines nothing

    def test_spec_check_and_tasks_say_which_after_a_task_is_missing(self):
        brief = lambda **kv: task("ENG-4", "002", files="b.go", crit="- calls `a.go` Run", **kv)
        owner = task("ENG-3", "001", files="a.go")
        self.assertEqual(check([owner, brief()])["ENG-4"], (False, "task", ["002 names a.go, which 001 owns: add `after: 001`"]))
        self.assertEqual(check([owner, brief(), task("ENG-5", "003", ["ENG-3"], files="c.go", crit="- calls `a.go` Run")])["ENG-5"], (True, "task", []))
        self.assertEqual(check([owner, task("ENG-4", "002", ["ENG-3"], files="b.go", crit="- calls `a.go` Run")])["ENG-4"], (True, "task", []))
        repo = Path(tempfile.mkdtemp())
        proj = repo / ".compose" / "p"
        (proj / "results").mkdir(parents=True)
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        def printed(*issues):
            fc.fc_tasks.sync_tasks(proj, data(list(issues)))
            out = io.StringIO()
            with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch("sys.stdout", out), branches():
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
        proj = repo / ".compose" / "p"
        (proj / "results").mkdir(parents=True)
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        fc.fc_tasks.sync_tasks(proj, data([task("ENG-3", "001"), task("ENG-4", "002", ["ENG-3"]), task("ENG-5", "003", ["ENG-4"])]))
        for tid in ("001", "002"):  # passed layers, their branches not merged yet
            (proj / "results" / f"{tid}.json").write_text('{"status": "passed"}')
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch("sys.stdout", out), branches(have=("001", "002")):
            fc.fc_spec.cmd_tasks(SimpleNamespace(repo=str(repo), project="p"))
        self.assertIn("run waves (each wave in parallel): 003\n", out.getvalue())
        self.assertNotIn("SERIAL CHAIN", out.getvalue())

    def test_tasks_prints_the_stack_order_the_chain_builds_with_a_task_above_a_higher_id_it_is_after(self):
        """A re-plan made 012 and 013 wait on 025: `tasks` stacks them above it, before and after their branches exist, as the chain
        does, and a merged task is no layer."""
        repo = Path(tempfile.mkdtemp())
        proj = repo / ".compose" / "p"
        (proj / "results").mkdir(parents=True)
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        (proj / "verify.json").write_text('{"task_prefix": "fast/p/"}')
        fc.fc_tasks.sync_tasks(proj, data([task("ENG-3", "011"), task("ENG-4", "012", ["ENG-3", "ENG-7"]), task("ENG-5", "013", ["ENG-3", "ENG-7"]),
                                           task("ENG-6", "024"), task("ENG-7", "025", ["ENG-6"]), task("ENG-8", "026", files="z.go")]))
        def header(have=(), merged=()):
            out = io.StringIO()
            with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch("sys.stdout", out), contextlib.suppress(SystemExit), branches(have, merged):  # until a layer passes, this plan is a serial chain
                fc.fc_spec.cmd_tasks(SimpleNamespace(repo=str(repo), project="p"))
            return out.getvalue().splitlines()[0].partition("stack order (bottom first): ")[2]
        (proj / "results" / "026.json").write_text('{"status": "passed"}')
        self.assertEqual(header(("026",), ("026",)), "011 <- 024 <- 025 <- 012 <- 013")  # no layer is built yet
        built = ("011", "012", "013", "024", "025")
        for tid in built:
            (proj / "results" / f"{tid}.json").write_text('{"status": "passed"}')
        with mock.patch.object(fc.fc_worktree, "task_branches", return_value={f"fast/p/{t}": None for t in built}):
            chain = [b.removeprefix("fast/p/") for b in fc.fc_worktree.stack_order(repo, proj)]
        self.assertEqual((header(built + ("026",), ("026",)), " <- ".join(chain)), ("011 <- 024 <- 025 <- 012 <- 013",) * 2)

    def clash_tasks(self, cache=None):
        """`compose tasks` of three synced tasks, two sharing a.go, 001 and 003 routed; `cache` edits tasks.json first."""
        clash = [routed(task("ENG-3", "001", files="a.go"), "* workflow: test-first", "* depth: STANDARD", "* decided by: jev"),
                 task("ENG-4", "002", files="a.go"),
                 routed(task("ENG-5", "003", ["ENG-3"], files="b.go"), "- depth: DEEP", "- decided by: fallback", "- workflow: fix-then-regression-test")]
        repo = Path(tempfile.mkdtemp())
        proj = repo / ".compose" / "p"
        proj.mkdir(parents=True)
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        fc.fc_tasks.sync_tasks(proj, data(clash, ""))
        if cache:
            path = proj / "results" / "tasks.json"
            path.write_text(json.dumps(cache(json.loads(path.read_text()))))
        out = io.StringIO()
        self.exit_code = 0
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch("sys.stdout", out), branches():
            try:
                fc.fc_spec.cmd_tasks(SimpleNamespace(repo=str(repo), project="p"))
            except SystemExit as e:
                self.exit_code = e.code
        return clash, proj, out.getvalue()

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

    def test_a_delete_only_task_passes_and_one_with_neither_still_fails(self):
        got = check([task("ENG-3", "001", files="", delete="old.go, old/"), task("ENG-4", "002", files=""),
                      task("ENG-5", "003", files="", delete="")])
        self.assertEqual(got["ENG-3"], (True, "task", []))
        for i in ("ENG-4", "ENG-5"):
            self.assertFalse(got[i][0])
            self.assertTrue(has(got[i][2], "empty `files:`"), got[i])

    def test_a_delete_only_task_still_reports_its_other_gaps(self):
        ok, _, gaps = check([task("ENG-3", "001", crit="\n".join(f"- c{n}" for n in range(6)), files="", protected="", delete="old.go")])["ENG-3"]
        self.assertFalse(ok)
        self.assertTrue(has(gaps, "6 bullets"), gaps)
        self.assertFalse(has(gaps, "empty `protected:`"), gaps)
        self.assertFalse(has(gaps, "empty `files:`"), gaps)

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
        proj = repo / ".compose" / "p"
        proj.mkdir(parents=True)
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        fc.fc_tasks.sync_tasks(proj, data(issues))
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch("sys.stdout", out), branches():
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

    def test_a_line_shaped_like_a_key_with_a_capital_or_a_digit_is_named_not_merged_silently(self):
        ok, _, gaps = self.gaps((1, FULL + "\nGiven: also g\nstep2: run it\nnot a key: x"), (2, FULL))
        self.assertEqual((ok, gaps), (False, [
            "scenario line `Given: also g` is read as more text of the key above it: a key is lowercase letters and _ (`given:`)",
            "scenario line `step2: run it` is read as more text of the key above it: a key is lowercase letters and _"]))

    def test_a_scenario_without_an_id_shows_a_question_mark_and_both_gaps_come_in_order(self):
        gaps = self.gaps((1, FULL), ("", "scenario: s"))[2]
        self.assertEqual(gaps[0], "scenario 2 lacks id, given, when, then, test_type, oracle")
        self.assertEqual(gaps[1], "scenario ids 1, ? do not match the 2 criteria (expected 1 to 2)")
