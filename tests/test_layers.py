import hermetic  # first: no test sees the real home
import argparse, json, os, subprocess, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from layers_kit import WRITTEN, fc, unit
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""


class Layers(unittest.TestCase):
    """The stack and verify commands decide from what is live on the project and where HEAD stands, before any layer is cut."""
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve(); self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "verify.json").write_text(json.dumps({"review_base": "main"}))
        for p in (mock.patch.object(fc.fc_core, "checkout", lambda path: fc.fc_core.Checkout((self.repo, self.repo / ".git", self.repo / ".git"))),
                  mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_core, "current_branch", return_value="compose/p")):
            p.start(); self.addCleanup(p.stop)

    def hold(self, kind, since="07:01"):
        live = fc.fc_update.live_dir(); live.mkdir(parents=True, exist_ok=True)
        held = live / f"{os.getppid()}.json"  # the pid of a process that stays alive; its record is removed when the test ends
        held.write_text(json.dumps({"pid": os.getppid(), "kind": kind, "project": "p", "repo": str(self.repo), "since": since}))
        self.addCleanup(held.unlink, missing_ok=True)

    def test_stack_top_commands_refuse_while_a_verify_holds_the_stack_top(self):
        self.hold("verify", "21:01")
        ns = lambda **kw: argparse.Namespace(repo=str(self.repo), project="p", trunk="compose/p", **kw)
        for name, cmd, args in (("stack", fc.fc_ship.cmd_stack, ns(submit=False, update=False)), ("stack --submit", fc.fc_ship.cmd_stack, ns(submit=True, update=False)),
                                ("top", fc.fc_status.cmd_top, ns(detach=False)), ("top --detach", fc.fc_status.cmd_top, ns(detach=True))):
            with self.subTest(name), self.assertRaises(SystemExit) as e:
                cmd(args)
            self.assertIn(f"verify is running on p (pid {os.getppid()}, since 21:01)", str(e.exception))

    def test_a_verify_stops_while_a_run_or_resume_is_live_and_a_run_stops_while_a_verify_is_live(self):
        """A run registers its stack in the same CLI worktree a verify stands on: a verify begun beside it judged the project branch, not the stack top."""
        a = argparse.Namespace(repo=str(self.repo), project="p", cmd="run", only=None, trunk="compose/p", force=False)
        for kind in ("run", "resume"):
            self.hold(kind)
            with self.subTest(f"verify beside a {kind}"), mock.patch.object(fc.fc_init, "start_checks", side_effect=AssertionError("it stops before any check")), self.assertRaises(SystemExit) as e:
                fc.fc_verify.cmd_verify(a)
            self.assertIn(f"{kind} is running on p (pid {os.getppid()}, since 07:01)", str(e.exception))
        self.hold("verify")
        with self.subTest("run beside a verify"), mock.patch.object(fc.fc_run.fc_credit, "check", return_value="credit"), mock.patch.object(fc.fc_proc, "keep_awake"), self.assertRaises(SystemExit) as e:
            fc.fc_run.run_tasks(self.repo, self.proj, [], a)
        self.assertIn(f"verify is running on p (pid {os.getppid()}, since 07:01)", str(e.exception))


class HeadProblem(unittest.TestCase):
    """The stack is built only from the project branch, or from a HEAD that holds none of its unmerged task branches: a HEAD on a task
    branch hides every task branch it contains, and the layers above it would be deleted."""
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve(); self.proj = self.repo / ".compose" / "p"
        self.here, self.held, self.layers = "compose/p", [], []
        def sh(cmd, *a, **k):  # the four questions head_problem asks git
            out = {"--show-current": self.here, "--list": "\n".join(self.layers), "--merged=HEAD": "\n".join(self.held)}
            return subprocess.CompletedProcess(cmd, 0, next((v for flag, v in out.items() if flag in cmd), ""), "")
        for p in (mock.patch.object(fc.fc_core, "sh", sh), mock.patch.object(fc.fc_core, "project_branch", return_value="compose/p"),
                  mock.patch.object(fc.fc_worktree, "own_branches", return_value=["compose/p-001", "compose/p-002", "compose/p-003"]),
                  mock.patch.object(fc.fc_layers, "layer_prefix", return_value="compose/p-"), mock.patch.object(fc.fc_worktree, "task_prefix", return_value="compose/p-")):
            p.start(); self.addCleanup(p.stop)

    def test_a_head_on_a_task_or_layer_branch_stops_the_stack(self):
        self.layers = ["compose/p-layer-01"]
        for at in ("compose/p-003", "compose/p-002", "compose/p-layer-01"):
            self.here = at
            self.assertIn(f"HEAD is on {at}, one of the project's own branches, not on compose/p", fc.fc_stack.head_problem(self.repo, self.proj))

    def test_a_head_holding_unmerged_task_branches_stops_the_stack_but_not_a_layer_branch_it_holds(self):
        self.here, self.held = "", ["compose/p-003", "compose/p-layer-01"]
        self.assertIn("HEAD holds the unmerged task branches compose/p-003, which would be left out of the stack", fc.fc_stack.head_problem(self.repo, self.proj))

    def test_the_project_branch_and_a_head_holding_no_task_is_fine(self):
        self.assertEqual(fc.fc_stack.head_problem(self.repo, self.proj), "")
        self.here = ""
        self.assertEqual(fc.fc_stack.head_problem(self.repo, self.proj), "")

    def test_a_stack_with_a_head_problem_builds_nothing_and_says_why(self):
        self.here, out = "compose/p-003", []
        with mock.patch.object(fc.fc_core, "say", out.append), mock.patch.object(fc.fc_worktree, "rename_legacy_branches", side_effect=AssertionError("it built on")):
            self.assertEqual(fc.fc_stack.register_stack(self.repo, self.proj, "compose/p"), [])
        self.assertEqual(len(out), 1); self.assertIn("stack  STOP  HEAD is on compose/p-003", out[0])
        self.assertIn("HEAD is on compose/p-003", fc.fc_layers.STACK_STOP)
        fc.fc_layers.STACK_STOP = ""


class BaseAhead(unittest.TestCase):
    """A base ahead of its upstream would put its unpushed commits into the PRs: a submit stops before anything is built or pushed."""
    def test_a_submit_with_the_base_ahead_of_its_upstream_stops_with_nothing_built(self):
        def sh(cmd, *a, **k):
            return subprocess.CompletedProcess(cmd, 0, "origin/main\n" if "--abbrev-ref" in cmd else "2\n", "")
        out = []
        with mock.patch.object(fc.fc_core, "sh", sh), mock.patch.object(fc.fc_core, "say", out.append), mock.patch.object(fc.fc_stack, "head_problem", return_value=""), \
             mock.patch.object(fc.fc_worktree, "rename_legacy_branches"), mock.patch.object(fc.fc_worktree, "stack_order", return_value=["fast/p/001"]), \
             mock.patch.object(fc.fc_core, "project_config", return_value={}), mock.patch.object(fc.fc_stack, "layer_base", return_value="main"), \
             mock.patch.object(fc.fc_stack, "fc_layers", mock.Mock(STACK_STOP="")) as layers:
            self.assertEqual(fc.fc_stack.register_stack(Path("."), Path("p"), "compose/p", submit=True), [])
        self.assertEqual(out, ["stack  STOP  main is 2 commits ahead of origin/main, so the PRs would carry them; nothing was built or pushed. "
                               "Push main, or wait until origin/main has them, then re-run"])
        self.assertEqual(layers.STACK_STOP, out[0][len("stack  STOP  "):])


class FakedLayers(unittest.TestCase):
    """The layer helpers on plain data: a temp project dir, and no process can start."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / "p"
        (self.proj / "results" / "crew").mkdir(parents=True)
        for target in ("subprocess.run", "subprocess.Popen"):
            patch = mock.patch(target, side_effect=AssertionError("a faked test starts no process"))
            patch.start(); self.addCleanup(patch.stop)

    def test_a_redone_test_writer_owns_only_the_commits_of_its_own_runs_not_the_gap_between_them(self):
        """Tests-003 ran at 10:10 and again at 12:00. Tests-001, redone at 11:00 in between: 003's spans never cover it."""
        for t, runs in (("001", (("10:00:00", "started"), ("10:05:00", "test ready"), ("10:06:00", "done"), ("11:00:00", "started"), ("11:05:00", "done"))),
                        ("003", (("10:10:00", "started"), ("10:15:00", "done"), ("12:00:00", "started"), ("12:05:00", "done")))):
            (self.proj / "results" / "crew" / f"{fc.fc_herdr.agent_name('p', f'tests-{t}')}.status").write_text("".join(
                json.dumps({"at": f"2026-01-01 {at}", "state": "done" if m == "done" else "working", "message": m}) + "\n" for at, m in runs))
        at = lambda hms: time.mktime(time.strptime(f"2026-01-01 {hms}", "%Y-%m-%d %H:%M:%S"))
        self.assertEqual(fc.fc_layers.test_writers(self.proj, ["001", "002", "003"]),
                         [("003", at("12:00:00"), at("12:05:00")), ("001", at("11:00:00"), at("11:05:00")),
                          ("003", at("10:10:00"), at("10:15:00")), ("001", at("10:00:00"), at("10:06:00"))])

    def test_a_task_over_the_budget_is_never_split(self):
        self.assertEqual(fc.fc_layers.layer_note({"tasks": ["003"], "lines": 155, "generated": {}}, 130),
                         "task 003: 155 lines; over the 130-line budget, a task is never split")

    def test_a_recorded_layer_keeps_its_tasks_whatever_their_sizes_and_the_rest_are_grouped_into_new_layers_above(self):
        (self.proj / "layers.json").write_text(json.dumps({"p-layer-01": ["groundwork"], "p-layer-02": ["001", "002"], "p-layer-03": ["003"]}))
        us = [unit("groundwork", 40), unit("001", 105), unit("002", 125), unit("003", 5), unit("004", 100), unit("005", 100)]  # 003 now fits with 001 and 002
        self.assertEqual([(b, [u["name"] for u in g]) for b, g, _ in fc.fc_layers.group_by_record(self.proj, us, 300)],
                         [("p-layer-01", ["groundwork"]), ("p-layer-02", ["001", "002"]), ("p-layer-03", ["003"]), (None, ["004", "005"])])


class PrText(unittest.TestCase):
    def test_pr_text_is_a_title_why_and_bullets_and_anything_else_is_refused_with_the_reason(self):
        f = Path(tempfile.mkdtemp()) / "001.md"
        self.assertIsNone(fc.fc_stack.pr_text(f, 72))
        f.write_text(WRITTEN.replace("\n\n## Why", "\n\n\n## Why\n").replace("on load\n", "on load\n\n\n"))
        self.assertEqual(fc.fc_stack.pr_text(f, 72), ("Keep the toggle's state across reloads",
                         ["The toggle forgot its state on every reload.", "This stores it."], ["- Store the toggle's state", "- Read it back on load"]))
        for text, why in [("Keep it\n## Why\nx\n## What changed\n- y", "line 1 must be `# <title>`"),
                          ("# JS-7: Keep it\n## Why\nx\n## What changed\n- y", "ticket key or (i/n)"),
                          ("# Keep it (2/3)\n## Why\nx\n## What changed\n- y", "ticket key or (i/n)"),
                          ("# " + "x" * 73 + "\n## Why\nx\n## What changed\n- y", "the title is 73 characters, at most 72"),
                          ("# Keep it\nintro\n## Why\nx\n## What changed\n- y", "`## Why` then `## What changed`, nothing else"),
                          ("# Keep it\n## What changed\n- y\n## Why\nx", "`## Why` then `## What changed`, nothing else"),
                          ("# Keep it\n## Why\nx\n## What changed\n- y\n### More\n", "`## Why` then `## What changed`, nothing else"),
                          ("# Keep it\n## Why\n\n## What changed\n- y", "must not be empty"),
                          ("# Keep it\n## Why\nx\nFixes o/r#9\n## What changed\n- y", "compose adds the Fixes and Part of lines"),
                          ("# Keep it\n## Why\nx\n## What changed\n- y\nz", "must be a `- ` bullet")]:
            f.write_text(text)
            with self.assertRaises(ValueError) as e:
                fc.fc_stack.pr_text(f, 72)
            self.assertIn(why, str(e.exception), text)


class FakedRegister(unittest.TestCase):
    """register_stack's decisions before anything is built, with git, gh and the chain faked: `upstream` and `ahead` say how the base stands."""
    ORDER = ["compose/p-001", "compose/p-002", "compose/p-003"]

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "verify.json").write_text("{}")
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": [{"id": t, "protected": []} for t in ("001", "002", "003")]}))
        self.upstream, self.ahead, self.behind, self.said, self.chain, self.through, self.built = "", 0, 0, [], [], [], []
        self.addCleanup(setattr, fc.fc_layers, "STACK_STOP", "")
        fc.fc_layers.STACK_STOP = ""
        for target, name, fake in ((fc.fc_stack, "head_problem", lambda *a: ""), (fc.fc_worktree, "rename_legacy_branches", lambda *a: {}), (fc.fc_worktree, "stack_order", lambda *a: self.ORDER),
                                   (fc.fc_worktree, "task_prefix", lambda *a: "compose/p-"), (fc.fc_stack, "layer_base", lambda *a: "main"), (fc.fc_core, "sh", self.sh),
                                   (fc.fc_core, "say", self.said.append), (fc.fc_worktree, "stack_worktree", lambda *a: self.proj), (fc.fc_layers, "linearize", self.linearize),
                                   (fc.fc_layers, "build_layers", lambda *a, **kw: self.built.append(a) or self.layers), (fc.fc_config, "read_config", lambda: {})):
            patch = mock.patch.object(target, name, fake)
            patch.start(); self.addCleanup(patch.stop)
        self.layers = None
        patch = mock.patch("subprocess.run", side_effect=AssertionError("gh stack was touched")); patch.start(); self.addCleanup(patch.stop)

    def sh(self, cmd, cwd=None, **kw):
        out = {"rev-list": str(self.behind if cmd[-1].startswith("main..") else self.ahead)}.get(cmd[1], self.upstream if "--abbrev-ref" in cmd else "")
        return SimpleNamespace(stdout=out + "\n", stderr="", returncode=0 if out or cmd[1] != "rev-parse" else 1)

    def linearize(self, repo, proj, trunk, wt, through=()):
        self.through.append(list(through))
        fc.fc_layers.STACK_STOP = self.stop
        return self.chain

    stop = ""

    def register(self, **kw):
        return fc.fc_stack.register_stack(self.proj.parent.parent, self.proj, "compose/p", **kw)

    def result(self, tid, status):
        (self.proj / "results" / f"{tid}.json").write_text(json.dumps({"status": status}))

    def test_a_base_ahead_of_its_upstream_warns_and_a_submit_stops_before_anything_is_built(self):
        self.upstream, self.ahead = "origin/main", 2
        self.assertEqual(self.register(), [])
        self.assertIn("stack  WARN  main is 2 commits ahead of origin/main, so the PRs would carry them; --submit stops until main is pushed", self.said)
        self.said.clear(); self.through.clear()
        self.assertEqual(self.register(submit=True), [])
        self.assertEqual(self.said, ["stack  STOP  main is 2 commits ahead of origin/main, so the PRs would carry them; nothing was built or pushed. Push main, or wait until origin/main has them, then re-run"])
        self.assertEqual((self.through, fc.fc_layers.STACK_STOP), ([], self.said[0].removeprefix("stack  STOP  ")))

    def test_a_base_behind_its_upstream_or_with_none_submits_without_a_word(self):
        for t in ("001", "002", "003"):
            self.result(t, "passed")
        for upstream in ("origin/main", ""):
            self.said.clear(); self.through.clear()
            self.upstream, self.ahead = upstream, 0  # behind: `upstream..base` holds nothing
            self.register(submit=True)
            self.assertEqual(self.through, [[]])  # it went on to the chain
            self.assertFalse([l for l in self.said if "WARN" in l or "STOP" in l], self.said)

    def test_a_base_behind_its_remote_has_the_layers_cut_on_the_remote_and_says_so(self):
        for t in ("001", "002", "003"):
            self.result(t, "passed")
        self.upstream, self.behind, self.chain = "origin/main", 20, self.ORDER
        self.register(submit=True)
        self.assertEqual(self.built[0][2], "origin/main")  # build_layers' base
        self.assertIn("stack  base  main is 20 commits behind origin/main; building on origin/main", self.said)

    def test_a_chain_rebase_that_stops_leaves_the_layers_and_gh_stack_untouched(self):
        self.stop, self.chain = "compose/p-003 conflicts when rebased onto compose/p-002 in 002.src", self.ORDER[:2]
        self.assertEqual(self.register(), [])
        self.assertEqual(self.built, [])  # no layer was cut; a gh call would have raised
        self.assertEqual(fc.fc_layers.STACK_STOP, self.stop)
        self.chain = []  # nothing chained at all
        self.stop = ""
        self.assertEqual(self.register(), [])
        self.assertEqual(self.built, [])

    def test_layers_that_do_not_add_up_to_the_chain_leave_gh_stack_untouched(self):
        for t in ("001", "002", "003"):
            self.result(t, "passed")
        self.chain, self.layers = self.ORDER, None  # build_layers says no: its STACK_STOP is the reason
        self.assertEqual(self.register(submit=True, gate=lambda *a: self.fail("the gate ran")), [])
        self.assertEqual(len(self.built), 1)

    def test_the_layers_differ_from_the_chain_in_every_file_but_the_ones_a_held_task_protects(self):
        tasks = [{"id": "001", "protected": ["t001.test"]}, {"id": "002", "protected": ["t002.test", "shared.test"]}, {"id": "003", "protected": ["t003.test"]}]
        self.assertEqual(fc.fc_layers.differing_files(["003.src", "t003.test", "t002.test"], tasks, []), ["003.src", "t003.test", "t002.test"])
        self.assertEqual(fc.fc_layers.differing_files(["003.src", "t003.test", "t002.test"], tasks, ["002", "003"]), ["003.src"])
        self.assertEqual(fc.fc_layers.differing_files([], tasks, ["001"]), [])
        why = fc.fc_layers.chain_conflict("compose/p-003", "compose/p-002", "002.src", "abc", "the old base from the reflog")
        self.assertEqual(why, "compose/p-003 conflicts when rebased onto compose/p-002 in 002.src; the layers below it are kept. Resolve by hand: "
                              "git checkout compose/p-003 && git rebase --onto compose/p-002 abc (the old base from the reflog), then re-run")

    def test_a_failed_task_holds_it_and_the_ones_after_it_back_and_the_layers_below_go_on(self):
        self.result("001", "passed"); self.result("002", "failed"); self.result("003", "passed")
        self.register(submit=True)
        self.assertIn("stack  hold  002 is failed; 002, 003 not submitted", self.said)
        self.assertEqual(self.through, [["compose/p-001"]])

    def test_a_pending_first_task_stops_with_nothing_pushed(self):
        self.assertEqual(self.register(submit=True), [])
        self.assertIn("stack  STOP  nothing to submit: 001 is pending", self.said)
        self.assertEqual(self.through, [])
        self.assertEqual(fc.fc_layers.STACK_STOP, "nothing to submit: 001 is pending")

    def test_held_tasks_start_at_the_first_task_not_passed_or_done_on_linear_whatever_its_id(self):
        held = lambda order, statuses, finished=(): fc.fc_stack.held_tasks(order, {**{t: "passed" for t in order}, **statuses}, set(finished))
        self.assertEqual(held(["001", "002", "003"], {"002": "failed"}), ["002", "003"])
        self.assertEqual(held(["001", "002", "003"], {"002": "failed"}, ["002"]), [])  # done on Linear, whatever its result
        self.assertEqual(held(["001", "002", "003"], {"001": "pending"}), ["001", "002", "003"])
        self.assertEqual(held(["001", "002"], {"001": "already_green", "002": "hand_done"}), [])
        order = fc.fc_worktree.layer_order([{"id": "001", "after": "003"}, {"id": "002"}, {"id": "003"}])  # 001 sits right above 003, below 002
        self.assertEqual((order, held(order, {"002": "failed"})), (["003", "001", "002"], ["002"]))


if __name__ == "__main__":
    unittest.main()
