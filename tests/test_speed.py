import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, shutil, subprocess, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"  # these cover the plain-worktree path, not the treehouse pool
os.environ["HERDR_ENV"] = ""
os.environ["COMPOSE_E2E_HEADLESS"] = "1"  # testing the tool itself never opens a browser window; real projects keep theirs

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


def git_show(base):
    """fc_core.sh answering only `git show HEAD:<file>` from `base` ({file: text on HEAD}); a file it lacks is not on HEAD. Anything else really runs."""
    real = fc.fc_core.sh
    def sh(cmd, cwd=None, **k):
        if cmd[:2] != ["git", "show"]:
            return real(cmd, cwd, **k)
        name = cmd[2].removeprefix("HEAD:")
        return SimpleNamespace(returncode=0, stdout=base[name]) if name in base else SimpleNamespace(returncode=128, stdout="")
    return mock.patch.object(fc.fc_core, "sh", sh)


class FormatOnPass(unittest.TestCase):
    def test_final_newline_added_without_a_formatter_but_base_and_binary_kept(self):
        wt = Path(tempfile.mkdtemp())
        (wt / "new.mjs").write_bytes(b"export {}")
        (wt / "bare.txt").write_bytes(b"still none")
        (wt / "bin.dat").write_bytes(b"\0\2")
        (wt / "old.mjs").write_bytes(b"a\n\n\n")
        with git_show({"bare.txt": "no newline", "bin.dat": "\0\1"}):
            fc.fc_format.format_files(wt, ["new.mjs", "bare.txt", "bin.dat", "old.mjs"], "HEAD")
        self.assertEqual((wt / "new.mjs").read_bytes(), b"export {}\n")
        self.assertEqual((wt / "old.mjs").read_bytes(), b"a\n")
        self.assertEqual((wt / "bare.txt").read_bytes(), b"still none")
        self.assertEqual((wt / "bin.dat").read_bytes(), b"\0\2")


class TaskBranches(unittest.TestCase):
    """task_base with git answered: `self.branches` are the task branches that exist, none of them in HEAD yet."""
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.repo, self.proj = root / "repo", root / "proj"
        self.repo.mkdir(); self.proj.mkdir()
        self.branches = {"compose/proj-001"}
        def sh(cmd, cwd=None, **k):
            code = 1 if "--is-ancestor" in cmd else (cmd[-1] not in self.branches) if "--verify" in cmd else 0  # no branch is in HEAD yet
            return SimpleNamespace(returncode=int(code), stdout="head" if cmd[-1] == "HEAD" else "sha-" + cmd[-1], stderr="")
        for mod, name, fake in ((fc.fc_core, "sh", sh), (fc.fc_core, "current_branch", lambda repo: "main"), (fc.fc_worktree, "stack_worktree", lambda repo, proj, trunk: repo),
                                (fc.fc_layers, "linearize", lambda repo, proj, trunk, wt, through=(): list(through))):
            patch = mock.patch.object(mod, name, fake); patch.start(); self.addCleanup(patch.stop)
        (self.proj / "verify.json").write_text('{"task_prefix": "compose/proj-"}')

    def test_an_after_task_builds_on_its_unmerged_dependency(self):
        base, sha = fc.fc_implement.task_base(self.repo, self.proj, {"id": "002", "after": "001"})
        self.assertEqual((base, sha), ("compose/proj-001", "sha-compose/proj-001"))

    def test_an_after_on_an_already_green_task_builds_on_that_task_s_own_dependency(self):
        # 002's test passes on 001's code, so 002 ends already_green with no branch; 003 needs that code all the same
        (self.proj / "results").mkdir()
        (self.proj / "results" / "tasks.json").write_text('{"feature": {}, "tasks": [{"id": "001", "after": ""}, {"id": "002", "after": "001"}, {"id": "003", "after": "002"}]}')
        (self.proj / "results" / "002.json").write_text('{"status": "already_green"}')
        base, _ = fc.fc_implement.task_base(self.repo, self.proj, {"id": "003", "after": "002"})
        self.assertEqual(base, "compose/proj-001")

    def test_a_bench_tag_follows_the_prefix_and_the_id_in_the_branch_name(self):
        name = fc.fc_implement.fc_attempt.branch_name
        self.assertEqual(name("compose/proj-", "001", ""), "compose/proj-001")
        self.assertEqual(name("compose/proj-", "001", "-bench-x"), "compose/proj-001-bench-x")
        self.assertEqual(name("team/x-", "001", ""), "team/x-001")  # a recorded project branch


class E2EBesideReview(unittest.TestCase):
    def test_e2e_runs_while_review_waits_on_models(self):
        spans = {}
        def slow(name, result):
            def run(*a, **k):
                start = time.time(); time.sleep(0.5); spans[name] = (start, time.time()); return result
            return run
        # a configured browser, so the stage does not launch its own: this times the overlap, not a Chromium start
        cfg = {"e2e": [{"name": "flow"}], "e2e_env": {"BU_CDP_URL": "http://127.0.0.1:1"}, "review": True, "jev_acceptance": True}
        args = type("A", (), {"skip_e2e": False, "skip_review": False})()
        with mock.patch.object(fc.fc_herdr, "herdr_verify_panes", return_value={}), mock.patch.object(fc.fc_herdr, "herdr_space", return_value=None), \
                mock.patch.object(fc.fc_verify, "review_inputs", return_value=("", "", "", [])), \
                mock.patch.object(fc.fc_jev, "jev_e2e", slow("e2e", {"exit": 0, "stdout": "", "stderr": ""})), \
                mock.patch.object(fc.fc_verify.fc_review, "llm_review", slow("review", {"pass": True, "issues": [], "confirmed": []})), \
                mock.patch.object(fc.fc_accept, "jev_acceptance", lambda *a, **k: {"pass": True, "criteria": [], "failed": []}):
            ok, report, _ = fc.fc_verify.run_stages(Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()), cfg, args)
        self.assertTrue(ok)
        self.assertEqual(len(report["e2e"]), 1)
        (a0, a1), (b0, b1) = spans["e2e"], spans["review"]
        self.assertLess(max(a0, b0), min(a1, b1))  # the two waits overlap; wall time would depend on machine load


def task_issue(ident, tid, crits, **kv):
    body = "## Goal\nDo it.\n\n## Acceptance criteria\n" + "".join(f"- {c}\n" for c in crits)
    block = "\n```compose\n" + "\n".join(f"{k}: {v}" for k, v in {"id": tid, **kv, "test": "t"}.items()) + "\n```\n"
    return {"id": ident, "title": tid, "description": body + block, "status": "Todo", "blockedBy": []}


def hunk(path, body):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n+{body}\n"


class AcceptanceJudge(unittest.TestCase):
    """The judge reads each task's own diff hunks, and confirm calls share one 60,000-character budget per task."""
    def test_state_is_the_tasks_hunks_and_confirm_shares_one_budget_per_task(self):
        repo, proj = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "p"
        (repo / "tests").mkdir()
        for f, n in (("a.py", 21000), ("tests/test_a.py", 21000), ("b.py", 31000)):
            (repo / f).write_text("R" * n)  # whole files, which only confirm reads
        proj.mkdir()
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        fc.fc_tasks.sync_tasks(proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [
            task_issue("ENG-2", "002", ["a1", "a2", "a3"], files="a.py", protected="tests/test_a.py"),
            task_issue("ENG-3", "003", ["b1", "b2", "b3"], files="b.py"),
            task_issue("ENG-4", "004", ["c1"], files="c.py")]})
        diff = (hunk("a.py", "A_OWN") + hunk("lib/a.py", "NOT_MINE") + hunk("tests/test_a.py", "T_OWN") + hunk("b.py", "B" * 40000))
        reply = {"model": "m", "answers": {f"c{i}": {"choice": "NOT_MET", "probabilities": {"NOT_MET": 0.9}} for i in range(3)}}
        sent = []
        def confirm_call(env, system, user, max_tokens=500):
            crit, code = user.split("\n\nCODE:\n", 1)
            sent.append((crit[len("CRITERION: "):], code))
            return True, "x"
        with mock.patch.object(fc.fc_config, "ENV_FILE", proj / "none.env"), mock.patch.object(fc.fc_models, "jev_ask", return_value=reply) as ask, \
                mock.patch.object(fc.fc_accept.fc_ledger, "counted", lambda repo, proj, meta, rows: rows), mock.patch.object(fc.fc_models, "confirm_call", side_effect=confirm_call), \
                mock.patch.object(fc.fc_core, "sh", return_value=subprocess.CompletedProcess([], 0, "", "")):  # keep_verdict asks git for the tip
            res = fc.fc_accept.jev_acceptance({}, proj, diff, repo=repo)
        states = {c.args[2]["task"]: c.args[2] for c in ask.call_args_list if c.args[4] == "Jev acceptance"}
        a, b, c = states["002"], states["003"], states["004"]
        self.assertEqual(a["files"], ["a.py", "tests/test_a.py"])
        self.assertLess(a["code"].index("A_OWN"), a["code"].index("T_OWN"))
        self.assertNotIn("NOT_MINE", a["code"])  # lib/a.py is another path, not a.py
        self.assertNotIn("B" * 10, a["code"])
        self.assertNotIn("R" * 10, a["code"] + b["code"])  # the state never reads the repo
        self.assertTrue(b["code"].startswith("diff --git a/b.py b/b.py"))
        self.assertEqual(len(b["code"]), fc.fc_accept.JEV_CODE_CAP)
        self.assertEqual(c["code"], diff[:fc.fc_accept.JEV_CODE_CAP])  # no hunk of its own: the whole diff, capped
        for prefix in ("a", "b"):
            calls = [code for crit, code in sent if crit.startswith(prefix)]
            self.assertEqual(len(calls), 2, prefix)  # the second call gets the rest of the budget, the third none
            self.assertLessEqual(sum(map(len, calls)), 60000, prefix)
            spent = [r for r in res["suspects"] if r["criterion"].startswith(prefix) and r.get("evidence") == "confirm budget spent"]
            self.assertEqual([r["confirmed"] for r in spent], [False], prefix)
            self.assertNotIn(spent[0]["criterion"], [crit for crit, _ in sent])

    def test_confirm_reads_only_regular_files_and_budgets_what_it_sends(self):
        repo, proj = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "p"
        for d in ("sections", "docs", "tests/fixtures"):
            (repo / d).mkdir(parents=True)
            (repo / d / "inner.md").write_text("INNER")
        (repo / "a.py").write_text("A" * 50000)
        proj.mkdir()
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        fc.fc_tasks.sync_tasks(proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [
            task_issue("ENG-2", "002", ["a1", "a2", "a3"], files="sections, a.py", protected="tests/fixtures, gone.py"),
            task_issue("ENG-3", "003", ["b1"], files="docs")]})
        reply = {"model": "m", "answers": {f"c{i}": {"choice": "NOT_MET", "probabilities": {"NOT_MET": 0.9}} for i in range(3)}}
        sent = []
        def confirm_call(env, system, user, max_tokens=500):
            crit, code = user.split("\n\nCODE:\n", 1)
            sent.append((crit[len("CRITERION: "):], code))
            return True, "x"
        with mock.patch.object(fc.fc_config, "ENV_FILE", proj / "none.env"), mock.patch.object(fc.fc_models, "jev_ask", return_value=reply), \
                mock.patch.object(fc.fc_accept.fc_ledger, "counted", lambda repo, proj, meta, rows: rows), mock.patch.object(fc.fc_models, "confirm_call", side_effect=confirm_call), \
                mock.patch.object(fc.fc_core, "sh", return_value=subprocess.CompletedProcess([], 0, "", "")):  # keep_verdict asks git for the tip
            res = fc.fc_accept.jev_acceptance({}, proj, hunk("a.py", "x"), repo=repo)  # raises IsADirectoryError if a directory is read
        whole = "==== a.py ====\n" + "A" * 50000
        a = sorted((code for crit, code in sent if crit.startswith("a")), key=len)
        self.assertEqual(a, [whole[:60000 - len(whole)], whole])  # the directories cost nothing; the second call gets the rest
        self.assertNotIn("INNER", "".join(code for _, code in sent))
        self.assertEqual([code for crit, code in sent if crit == "b1"], [""])  # only directories: still sent, with budget left
        self.assertEqual([r["evidence"] for r in res["suspects"] if not r["confirmed"]], ["confirm budget spent"])
        self.assertEqual(len(res["failed"]), 3)  # suspects are judged as before


class PrettierOptIn(unittest.TestCase):
    """Task 016 (NEB-953): prettier_opted_in and format_files running prettier only on an opt-in."""

    def ran(self, files, calls, text):
        """Write `files` (relative to the clone's parent) and an unformatted web/a.js, with prettier hoisted in the clone's
        node_modules; format the file, assert prettier's recorded argv and the file's text, then return prettier_opted_in."""
        top = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, top, True)
        wt, log, prettier = top / "clone", top / "calls", top / "clone" / "node_modules" / ".bin" / "prettier"
        for f, t in {**files, "clone/web/a.js": "x  =  1"}.items():
            (top / f).parent.mkdir(parents=True, exist_ok=True); (top / f).write_text(t)
        prettier.parent.mkdir(parents=True)
        prettier.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nprintf "pretty\\n\\n" > "$2"\n'); prettier.chmod(0o755)
        with git_show({}):
            fc.fc_format.format_files(wt, ["web/a.js"], "HEAD")
        self.assertEqual((log.read_text().replace(str(wt), "<wt>") if log.exists() else "", (wt / "web" / "a.js").read_text()), (calls, text))
        return fc.fc_format.prettier_opted_in(wt, wt / "web" / "a.js")

    def test_hoisted_prettier_alone_never_runs_and_the_file_keeps_its_text(self):
        for files in [{}, {"clone/.editorconfig": "root = true\n", "clone/web/.prettierignore": "dist\n", "clone/other/.prettierrc": ""},
                      {".prettierrc": "", "package.json": '{"prettier": {}, "devDependencies": {"prettier": "3"}}'},
                      {"clone/web/package.json": '{"scripts": {"prettier": "prettier ."}, "peerDependencies": {"prettier": "3"}}'},
                      {"clone/package.json": '{"devDependencies": {"prettier": "3"}}', "clone/web/package.json": '{"dependencies": {"x": "1"}}'}]:
            with self.subTest(files=files):
                self.assertIs(self.ran(files, "", "x  =  1\n"), False)

    def test_each_opt_in_at_the_files_directory_or_the_root_opts_in(self):
        names = [".prettierrc"] + [".prettierrc." + e for e in "json yaml yml json5 js cjs mjs ts cts mts toml".split()] \
            + ["prettier.config." + e for e in "js cjs mjs ts cts mts".split()]
        cases = [{d + n: ""} for n in names for d in ("clone/web/", "clone/")]
        cases += [{"clone/package.json": '{"prettier": "@acme/style"}', "clone/web/package.json": "{}"},
                  {"clone/web/package.json": '{"prettier": {}}'},
                  {"clone/web/package.json": '{"dependencies": {"prettier": "3"}}'},
                  {"clone/package.json": '{"devDependencies": {"prettier": "3"}}'}]
        for files in cases:
            with self.subTest(files=files):
                top = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, top, True)
                for f, t in {**files, "clone/web/a.js": ""}.items():
                    (top / f).parent.mkdir(parents=True, exist_ok=True); (top / f).write_text(t)
                self.assertIs(fc.fc_format.prettier_opted_in(top / "clone", top / "clone" / "web" / "a.js"), True)

    def test_an_opt_in_runs_prettier_on_the_file(self):
        self.assertIs(self.ran({"clone/.prettierrc": ""}, "--write <wt>/web/a.js\n", "pretty\n"), True)

if __name__ == "__main__":
    unittest.main()
