import hermetic  # first: no test sees the real home
import argparse, json, os, tempfile, time, unittest
from pathlib import Path
from unittest import mock
from layers_kit import LayersBase, fc, unit
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""


class Layers(LayersBase):
    """A project branch with groundwork and every task's test on it, and task branches above it: the layers regroup all of it."""
    def layers(self):
        return self.git("branch", "--list", "compose/p-layer-*", "--format=%(refname:short)").split()

    def test_groundwork_is_its_own_layer_and_each_test_lands_with_its_task(self):
        out = self.stack(review_base="master")  # default budget 300: tasks with their tests are 105, 125 and 155 lines; a base that names no branch (a project made before init recorded one) stacks on the default branch, main here
        self.assertEqual(self.layers(), ["compose/p-layer-01", "compose/p-layer-02", "compose/p-layer-03"])
        stack = json.loads((self.repo / ".git" / "fake-gh-stack.json").read_text())
        self.assertEqual((stack["trunk"], [b["name"] for b in stack["branches"]]), ("main", self.layers()))
        self.assertEqual(self.changed("compose/p-layer-01", "main"), {"lock.txt", "bun.lock", "setup.txt", "stub.txt"})
        self.assertEqual(self.changed("compose/p-layer-02", "compose/p-layer-01"), {"t001.test", "001.src", "t002.test", "002.src"})
        self.assertEqual(self.changed("compose/p-layer-03", "compose/p-layer-02"), {"t003.test", "003.src"})
        self.assertEqual(self.git("diff", "compose/p-layer-03", "compose/p-003"), "")  # the layers add up to the project's tree
        notes = [l for l in out if l.startswith("layer ")]
        self.assertIn("groundwork x2: 40 lines; generated, not counted: bun.lock (4000 lines)", notes[0])
        self.assertIn("task 001, 002: 230 lines", notes[1])

    def test_test_commits_on_one_file_keep_the_trunks_order_whoever_owns_them(self):
        """Two tasks protect one test file. A commit on it alone would go to the first of them, but it was written on top of a
        commit the second task owns: it follows that commit into the second task's unit, so the layers replay the trunk's order
        on every file and add up instead of conflicting."""
        tasks = [{"id": "001", "protected": ["t001.test", "shared.test"], "files": ["001.src"], "after": ""},
                 {"id": "002", "protected": ["t002.test", "shared.test"], "files": ["002.src"], "after": ""},
                 {"id": "003", "protected": ["t003.test"], "files": ["003.src"], "after": ""}]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        self.write("shared.test", "a\n" * 5); self.write("t002.test", "t\n" * 6); self.commit("test 002, with the shared file")  # 002's: two of its files
        self.write("shared.test", "a\n" * 5 + "b\n" * 5); self.commit("more shared tests")  # on 001's file too, written on top of 002's commit
        out = self.stack()
        self.assertEqual([l for l in out if "STOP" in l], [])
        self.assertEqual(self.layers(), ["compose/p-layer-01", "compose/p-layer-02", "compose/p-layer-03"])
        self.assertEqual(self.changed("compose/p-layer-02", "compose/p-layer-01"), {"t001.test", "001.src", "t002.test", "002.src", "shared.test"})
        self.assertEqual(self.git("show", "compose/p-layer-02:shared.test"), "a\n" * 5 + "b\n" * 4 + "b")
        self.assertEqual(self.git("diff", "compose/p-layer-03", "compose/p-003"), "")

    def toolchain(self, tid):
        tasks = json.loads((self.proj / "results" / "tasks.json").read_text())
        for t in tasks["tasks"]:
            t["kind"] = "toolchain" if t["id"] == tid else "code"
        (self.proj / "results" / "tasks.json").write_text(json.dumps(tasks))

    def test_a_toolchain_task_is_the_bottom_layer_below_groundwork_so_every_layer_has_the_tools_verify_runs(self):
        """Task 003 sets up a tool (packages, scripts, config) that verify's checks run. Groundwork's layers come below every task's,
        so a check of that tool would fail on them: the toolchain task leads the chain and its layer goes below groundwork."""
        self.toolchain("003")
        out = self.stack()
        self.assertEqual([l for l in out if "STOP" in l], [])
        self.assertEqual(self.layers(), ["compose/p-layer-01", "compose/p-layer-02", "compose/p-layer-03"])
        self.assertEqual(self.changed("compose/p-layer-01", "main"), {"t003.test", "003.src"})
        self.assertEqual(self.changed("compose/p-layer-02", "compose/p-layer-01"), {"lock.txt", "bun.lock", "setup.txt", "stub.txt"})
        self.assertEqual(self.changed("compose/p-layer-03", "compose/p-layer-02"), {"t001.test", "001.src", "t002.test", "002.src"})
        self.assertEqual(self.git("diff", "compose/p-layer-03", "compose/p-002"), "")  # the chain is 003, 001, 002, and the layers add up to it

    def test_a_toolchain_task_that_changes_a_groundwork_file_stays_above_groundwork_and_says_so(self):
        self.toolchain("003")
        self.git("checkout", "-q", "compose/p-003"); self.write("stub.txt", "s\n" * 21); self.commit("Stub the story"); self.git("checkout", "-q", "compose/p")
        out = self.stack()
        self.assertEqual([l for l in out if "STOP" in l], [])
        self.assertEqual(self.changed("compose/p-layer-01", "main"), {"lock.txt", "bun.lock", "setup.txt", "stub.txt"})
        self.assertIn("stack  note  toolchain task 003 stays above groundwork: it changes stub.txt too, so a check its tools bring fails below its layer", out)
        self.assertEqual(self.git("diff", self.layers()[-1], "compose/p-002"), "")

    def test_groundwork_written_after_a_tasks_test_gets_its_own_layer_above_that_task(self):
        """A commit no task owns, on a file a task's test commit touched before it, cannot replay in the bottom groundwork: it is a
        groundwork layer of its own right above that task, never inside the task's layer. A later commit on a file it touched goes
        to the first task above it that protects the file, not into the task below."""
        tasks = [{"id": "001", "protected": ["t001.test", "shared.test"], "files": ["001.src"], "after": ""},
                 {"id": "002", "protected": ["t002.test"], "files": ["002.src"], "after": ""},
                 {"id": "003", "protected": ["t003.test", "shared.test"], "files": ["003.src"], "after": ""}]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        self.write("t002.test", "g\n" + "t\n" * 5); self.write("shared.test", "g\n"); self.commit("guard every test")  # no task owns both
        self.write("shared.test", "g\n" + "a\n" * 5); self.commit("tests for 003")  # protected by 001 and 003, written after the guard
        out = self.stack()
        self.assertEqual([l for l in out if "STOP" in l], [])
        self.assertEqual(self.layers(), ["compose/p-layer-01", "compose/p-layer-02", "compose/p-layer-03", "compose/p-layer-04"])
        self.assertEqual(self.changed("compose/p-layer-02", "compose/p-layer-01"), {"t001.test", "001.src", "t002.test", "002.src"})
        self.assertEqual(self.changed("compose/p-layer-03", "compose/p-layer-02"), {"t002.test", "shared.test"})
        self.assertEqual(self.changed("compose/p-layer-04", "compose/p-layer-03"), {"t003.test", "003.src", "shared.test"})
        self.assertIn("layer-03  groundwork x1: 2 lines", [l for l in out if l.startswith("layer ")][2])
        self.assertEqual(self.git("diff", "compose/p-layer-04", "compose/p-003"), "")

    def test_a_test_commit_goes_to_the_task_its_test_writer_wrote_it_for(self):
        """Tasks 001 and 003 both protect one test file. 003's test writer, teammate tests-003, commits its test on top of 001's
        within the span of its reports: it lands in 003's layer, not in the first layer whose task protects the file."""
        tasks = [{"id": "001", "protected": ["t001.test", "shared.test"], "files": ["001.src"], "after": ""},
                 {"id": "002", "protected": ["t002.test"], "files": ["002.src"], "after": ""},
                 {"id": "003", "protected": ["t003.test", "shared.test"], "files": ["003.src"], "after": ""}]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        self.write("shared.test", "a\n" * 5); self.commit("tests for 001")  # no writer's span holds it: the first task's
        crew = self.proj / "results" / "crew"; crew.mkdir()
        (crew / f"{fc.fc_herdr.agent_name('p', 'tests-003')}.status").write_text("".join(json.dumps({"at": at, "state": "working", "message": m}) + "\n"
            for at, m in (("2026-01-01 10:00:00", "started"), ("2026-01-01 10:05:00", "test ready: 1234567"))))
        self.write("shared.test", "a\n" * 5 + "b\n" * 5); self.git("add", "-A"); self.git("commit", "-qm", "tests for 003", "--date", "2026-01-01T10:02:00")
        out = self.stack()
        self.assertEqual([l for l in out if "STOP" in l], [])
        self.assertEqual(self.layers(), ["compose/p-layer-01", "compose/p-layer-02", "compose/p-layer-03"])
        self.assertEqual(self.git("show", "compose/p-layer-02:shared.test"), "a\n" * 4 + "a")
        self.assertEqual(self.changed("compose/p-layer-03", "compose/p-layer-02"), {"t003.test", "003.src", "shared.test"})
        self.assertEqual(self.git("diff", "compose/p-layer-03", "compose/p-003"), "")

    def test_a_task_is_tested_on_the_lowest_layer_that_holds_its_code_and_its_test_commits(self):
        """003's test is on the shared file first and 001's writer, tests-001, appends right after it, so 001's test rides in 003's
        layer: 001 is tested there, while 002 still is on its own layer."""
        tasks = [{"id": "001", "protected": ["t001.test", "shared.test"], "files": ["001.src"], "after": ""},
                 {"id": "002", "protected": ["t002.test"], "files": ["002.src"], "after": ""},
                 {"id": "003", "protected": ["t003.test", "shared.test"], "files": ["003.src"], "after": ""}]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        self.write("t003.test", "u\n" * 5); self.write("shared.test", "c3\n"); self.commit("tests for 003")
        crew = self.proj / "results" / "crew"; crew.mkdir()
        (crew / f"{fc.fc_herdr.agent_name('p', 'tests-001')}.status").write_text("".join(json.dumps({"at": at, "state": "working", "message": m}) + "\n"
            for at, m in (("2026-01-01 10:00:00", "started"), ("2026-01-01 10:05:00", "test ready: 1234567"))))
        self.write("shared.test", "c3\nc1\n"); self.git("add", "-A"); self.git("commit", "-qm", "tests for 001", "--date", "2026-01-01T10:02:00")
        built, real = [], fc.fc_layers.build_layers
        with mock.patch.object(fc.fc_layers, "build_layers", lambda *a, **k: built.append(real(*a, **k)) or built[-1]):
            self.stack()
        self.assertEqual([l["tested"] for l in built[0]], [[], ["002"], ["001", "003"]])

    def test_a_test_commit_skips_an_earlier_one_on_its_file_it_does_not_build_on(self):
        """Task 004 has not passed: the test its writer made rides in the top layer. 001's test comes after it on the same file, in
        another part of it: it still lands in 001's layer. One that edits 004's test follows that test into the top layer."""
        tasks = [{"id": "001", "protected": ["t001.test", "shared.test"], "files": ["001.src"], "after": ""},
                 {"id": "002", "protected": ["t002.test"], "files": ["002.src"], "after": ""},
                 {"id": "003", "protected": ["t003.test"], "files": ["003.src"], "after": ""},
                 {"id": "004", "protected": ["t004.test", "shared.test"], "files": ["004.src"], "after": ""}]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        part = lambda a, b: f"class A\n{a}" + "-\n" * 8 + f"class B\n{b}"
        self.write("shared.test", part("", "")); self.write("stub.txt", "s\n" * 21); self.commit("a part of the test file for each task")
        crew = self.proj / "results" / "crew"; crew.mkdir()
        (crew / f"{fc.fc_herdr.agent_name('p', 'tests-004')}.status").write_text("".join(json.dumps({"at": at, "state": "working", "message": m}) + "\n"
            for at, m in (("2026-01-01 10:00:00", "started"), ("2026-01-01 10:05:00", "test ready: 1234567"))))
        self.write("shared.test", part("", "b\n" * 5)); self.git("add", "-A"); self.git("commit", "-qm", "tests for 004", "--date", "2026-01-01T10:02:00")
        self.write("shared.test", part("a\n" * 5, "b\n" * 5)); self.commit("tests for 001")
        self.write("shared.test", part("a\n" * 5, "b\n" * 4 + "c\n")); self.commit("fix a test of 004")  # 001 owns it too
        out = self.stack()
        self.assertEqual([l for l in out if "STOP" in l], [])
        self.assertEqual(self.layers(), ["compose/p-layer-01", "compose/p-layer-02", "compose/p-layer-03"])
        self.assertEqual(self.git("show", "compose/p-layer-02:shared.test") + "\n", part("a\n" * 5, ""))
        self.assertEqual(self.changed("compose/p-layer-03", "compose/p-layer-02"), {"t003.test", "003.src", "shared.test"})
        self.assertEqual(self.git("diff", "compose/p-layer-03", "compose/p-003"), "")

    def test_a_held_task_s_test_rides_no_higher_than_a_task_whose_own_commit_edits_it(self):
        """Task 004 has not passed, so its test rides in the top layer; but 002 was built on the trunk with that test and its own
        commit cuts lines from it: the test goes into 002's layer, where 002's commit applies on it."""
        tasks = json.loads((self.proj / "results" / "tasks.json").read_text())["tasks"]
        tasks += [{"id": "004", "protected": ["t004.test"], "files": ["004.src"], "after": ""}]
        tasks[1]["files"] += ["t004.test"]
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        self.write("t004.test", "a\n" * 5 + "b\n" * 5); self.commit("test 004")
        self.git("checkout", "-q", "compose/p-002"); self.git("rebase", "-q", "compose/p")
        self.write("t004.test", "a\n" * 5); self.commit("Cut the failing tests")
        self.git("checkout", "-q", "compose/p")
        out = self.stack()
        self.assertEqual([l for l in out if "STOP" in l], [])
        self.assertEqual(self.layers(), ["compose/p-layer-01", "compose/p-layer-02", "compose/p-layer-03"])
        self.assertEqual(self.git("show", "compose/p-layer-02:t004.test") + "\n", "a\n" * 5)
        self.assertEqual(self.git("diff", "compose/p-layer-03", "compose/p-003"), "")

    def stop(self, breaks):
        """Run `compose stack` after a first good build and `breaks`; return its STOP lines, having checked that it exits non-zero and
        changes no layer branch, recorded layer or gh stack."""
        self.stack()
        breaks()
        fc.fc_layers.record_layers(self.proj, [{"branch": b, "tasks": [b]} for b in self.layers()])
        refs = lambda: self.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/compose/p-layer-*")
        before = (refs(), (self.proj / "layers.json").read_text(), (self.repo / ".git" / "fake-gh-stack.json").read_text())
        calls = lambda: [c for c in (self.repo / ".git" / "fake-gh-calls").read_text().splitlines() if not c.startswith("pr view")]  # reading a PR's state changes nothing
        out, seen = [], calls()
        with mock.patch.object(fc.fc_core, "say", out.append), self.assertRaises(SystemExit) as e:
            fc.fc_ship.cmd_stack(argparse.Namespace(repo=str(self.repo), project="p", trunk="compose/p", submit=False, update=False))
        self.assertTrue(e.exception.code)
        self.assertEqual((refs(), (self.proj / "layers.json").read_text(), (self.repo / ".git" / "fake-gh-stack.json").read_text()), before)
        self.assertEqual(calls(), seen)
        return [l for l in out if "STOP" in l]

    def test_a_project_branch_behind_its_base_stops_the_stack_with_layers_and_gh_stack_untouched(self):
        def main_moves_on():
            self.git("checkout", "-q", "main"); self.write("setup.txt", "main\n"); self.commit("main moves on"); self.git("checkout", "-q", "compose/p")
        stops = self.stop(main_moves_on)
        main = self.git("rev-parse", "--short", "main")
        self.assertEqual(len(stops), 1)
        self.assertIn(f"compose/p conflicts with main {main} in setup.txt; the layers and the stack are left as they were. "
                      "Rebase compose/p onto main, then re-run", stops[0])

    def test_a_head_on_a_task_branch_or_holding_task_branches_stops_the_stack(self):
        for at in ("compose/p-003", "compose/p-002"):
            self.git("checkout", "-q", at)
            self.assertIn(f"HEAD is on {at}, one of the project's own branches, not on compose/p", fc.fc_stack.head_problem(self.repo, self.proj))
        with mock.patch.object(fc.fc_core, "say"):
            self.assertEqual(fc.fc_stack.register_stack(self.repo, self.proj, "compose/p"), [])
        self.assertEqual(self.layers(), [])
        self.git("checkout", "-q", "--detach", "compose/p-003")
        self.assertIn("HEAD holds the unmerged task branches compose/p-003, which would be left out of the stack", fc.fc_stack.head_problem(self.repo, self.proj))
        self.git("checkout", "-q", "compose/p")
        self.assertEqual(fc.fc_stack.head_problem(self.repo, self.proj), "")

    def test_a_chain_rebase_that_stops_stops_the_stack_verify_and_top_before_they_act_on_the_layers_below_it(self):
        def task_003_conflicts_with_002():
            self.git("checkout", "-q", "-B", "compose/p-003", "compose/p")
            self.write("002.src", "other\n"); self.write("003.src", "c\n"); self.commit("task 003 redone"); self.git("checkout", "-q", "compose/p")
        stops = self.stop(task_003_conflicts_with_002)
        why = "compose/p-003 conflicts when rebased onto compose/p-002 in 002.src; the layers below it are kept. Resolve by hand: git checkout compose/p-003 && git rebase --onto compose/p-002 "
        self.assertEqual(len(stops), 1); self.assertIn(why, stops[0])
        a = argparse.Namespace(repo=str(self.repo), project="p", fix=0, skip_review=True, skip_e2e=True, detach=False)
        said = {}
        for cmd in (fc.fc_verify.cmd_verify, fc.fc_status.cmd_top):
            out = said[cmd.__name__] = []
            with self.subTest(cmd.__name__):
                with mock.patch.object(fc.fc_core, "say", out.append), \
                        mock.patch.object(fc.fc_verify, "run_stages", side_effect=AssertionError("a stage ran")), self.assertRaises(SystemExit) as e:
                    cmd(a)
                self.assertEqual(e.exception.code, 1)
        self.assertTrue(said["cmd_verify"][-1].startswith("verify FAIL  nothing verified or recorded: " + why), said["cmd_verify"])
        self.assertFalse((self.proj / "results" / "verify.json").exists())

    def test_stack_top_commands_refuse_while_a_verify_holds_the_stack_top(self):
        (self.proj / "verify.json").write_text(json.dumps({"review_base": "main"}))
        live = fc.fc_update.live_dir(); live.mkdir(parents=True, exist_ok=True)
        held = live / f"{os.getppid()}.json"  # the pid of a process that stays alive; its record is removed when the test ends
        held.write_text(json.dumps({"pid": os.getppid(), "kind": "verify", "project": "p", "repo": str(self.repo), "since": "21:01"}))
        self.addCleanup(held.unlink, missing_ok=True)
        ns = lambda **kw: argparse.Namespace(repo=str(self.repo), project="p", trunk="compose/p", **kw)
        for name, cmd, args in (("stack", fc.fc_ship.cmd_stack, ns(submit=False, update=False)), ("stack --submit", fc.fc_ship.cmd_stack, ns(submit=True, update=False)),
                                ("top", fc.fc_status.cmd_top, ns(detach=False)), ("top --detach", fc.fc_status.cmd_top, ns(detach=True))):
            with self.subTest(name), self.assertRaises(SystemExit) as e:
                cmd(args)
            self.assertIn(f"verify is running on p (pid {os.getppid()}, since 21:01)", str(e.exception))
            self.assertEqual(self.layers(), [])

    def hold(self, kind):
        live = fc.fc_update.live_dir(); live.mkdir(parents=True, exist_ok=True)
        held = live / f"{os.getppid()}.json"  # the pid of a process that stays alive; its record is removed when the test ends
        held.write_text(json.dumps({"pid": os.getppid(), "kind": kind, "project": "p", "repo": str(self.repo), "since": "07:01"}))
        self.addCleanup(held.unlink, missing_ok=True)

    def test_a_verify_stops_while_a_run_or_resume_is_live_and_a_run_stops_while_a_verify_is_live(self):
        """A run registers its stack in the same CLI worktree a verify stands on: a verify begun beside it judged the project branch, not the stack top."""
        (self.proj / "verify.json").write_text(json.dumps({"review_base": "main"}))
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

    def test_a_verify_whose_checkout_moved_off_its_branch_during_a_stage_is_void_and_records_nothing(self):
        self.stack()
        wt = fc.fc_worktree.stack_worktree(self.repo, self.proj, "compose/p")
        def stack_moves_it(*a, **k):
            self.git("-C", str(wt), "checkout", "-q", "--detach", "compose/p")
            return True, {"checks": []}, ""
        a = argparse.Namespace(repo=str(self.repo), project="p", fix=0, skip_review=True, skip_e2e=True, detach=False)
        out = []
        with mock.patch.object(fc.fc_core, "say", out.append), mock.patch.object(fc.fc_verify, "run_stages", side_effect=stack_moves_it), self.assertRaises(SystemExit) as e:
            fc.fc_verify.cmd_verify(a)
        self.assertEqual(e.exception.code, 1)
        self.assertTrue(any(l.startswith("verify VOID  nothing verified or recorded: ") and "compose/p-003" in l and "detached" in l for l in out), out)
        self.assertFalse((self.proj / "results" / "verify.json").exists())

    def test_layers_that_do_not_add_up_to_the_chain_stop_the_stack_with_layers_and_gh_stack_untouched(self):
        real, broken = fc.fc_layers.review_units, mock.patch.object(fc.fc_layers, "review_units", lambda *a: real(*a)[:-1])
        stops = self.stop(broken.start)  # the top task's unit goes missing
        broken.stop()
        self.assertEqual(stops, ["stack  STOP  the layers differ from compose/p-003 in 003.src, t003.test; the layers and the stack are left as they were"])

    def submit(self):
        """Run `compose stack --submit`; return its lines and exit code (0 when it returned)."""
        out = []
        with mock.patch.object(fc.fc_core, "say", out.append):
            try:
                fc.fc_ship.cmd_stack(argparse.Namespace(repo=str(self.repo), project="p", trunk="compose/p", submit=True, update=False))
            except SystemExit as e:
                return out, e.code
        return out, 0

    def test_a_base_ahead_of_its_upstream_warns_and_a_submit_stops_with_nothing_changed(self):
        self.git("update-ref", "refs/remotes/origin/main", "main"); self.git("branch", "-q", "--set-upstream-to=origin/main", "main")
        self.git("checkout", "-q", "main")
        for n in ("1", "2"):
            self.write(f"unpushed{n}.txt", "u\n"); self.commit(f"unpushed {n}")
        self.git("checkout", "-q", "compose/p")
        self.assertIn("stack  WARN  main is 2 commits ahead of origin/main, so the PRs would carry them; --submit stops until main is pushed", self.stack())
        refs = lambda: self.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/compose/p*")
        before = (refs(), (self.repo / ".git" / "fake-gh-stack.json").read_text(), (self.repo / ".git" / "fake-gh-calls").read_text())
        out, code = self.submit()
        self.assertEqual(code, 1)
        self.assertEqual(out, ["stack  STOP  main is 2 commits ahead of origin/main, so the PRs would carry them; nothing was built or pushed. "
                               "Push main, or wait until origin/main has them, then re-run"])
        self.assertEqual((refs(), (self.repo / ".git" / "fake-gh-stack.json").read_text(), (self.repo / ".git" / "fake-gh-calls").read_text()), before)

    def test_a_base_behind_its_upstream_submits_without_a_word(self):
        self.git("update-ref", "refs/remotes/origin/main", "main"); self.git("branch", "-q", "--set-upstream-to=origin/main", "main")
        self.git("checkout", "-q", "-b", "elsewhere", "main"); self.write("theirs.txt", "t\n"); self.commit("pushed by someone else")
        self.git("update-ref", "refs/remotes/origin/main", "elsewhere"); self.git("checkout", "-q", "compose/p")
        (self.proj / "verify.json").write_text(json.dumps({"review_base": "main"}))
        out, code = self.submit()
        self.assertEqual(code, 0)
        self.assertFalse([l for l in out if "WARN" in l or "STOP" in l])
        self.assertIn("stack submit --auto", (self.repo / ".git" / "fake-gh-calls").read_text().splitlines())

    def test_stack_without_submit_prints_each_layers_pr_title_and_body_and_calls_no_gh_pr(self):
        (self.proj / "verify.json").write_text(json.dumps({"review_base": "main"}))
        cache = json.loads((self.proj / "results" / "tasks.json").read_text())
        (self.proj / "results" / "tasks.json").write_text(json.dumps({**cache, "feature": {"title": "Build it", "description": "Fixes o/r#9."}}))
        out = []
        with mock.patch.object(fc.fc_core, "say", out.append):
            fc.fc_ship.cmd_stack(argparse.Namespace(repo=str(self.repo), project="p", trunk="compose/p", submit=False, update=False))
        prs = [l for l in out if l.startswith("pr ")]
        self.assertEqual([l.split()[1] for l in prs], self.layers())
        for i, l in enumerate(prs, 1):
            self.assertIn(f"({i}/3)", l.splitlines()[0])
            self.assertIn("## What changed", l)
            self.assertNotRegex(l, r" lines|attempt")  # no size line or attempt count reaches a reviewer
        self.assertIn("## What changed\n    - Build 001\n    - Build 002\n\n", prs[1])  # one line per task, its test commit folded in
        self.assertIn("- Part of o/r#9", prs[1])  # as submit writes it: the issue is fixed by the layer with its last task
        self.assertIn("- Fixes o/r#9", prs[2])
        calls =(self.repo / ".git" / "fake-gh-calls").read_text().splitlines()
        self.assertFalse([c for c in calls if not c.startswith("stack ")])
        self.assertFalse((self.proj / "pr_text.json").exists())

    def preview(self, **cfg):
        """`compose stack`'s pr lines without --submit: ({branch: its pr line and body}, the other pr lines)."""
        (self.proj / "verify.json").write_text(json.dumps({"review_base": "main", **cfg}))
        out = []
        with mock.patch.object(fc.fc_core, "say", out.append):
            fc.fc_stack.register_stack(self.repo, self.proj, "compose/p", preview=True)
        prs = [l for l in out if l.startswith("pr ")]
        return {l.split()[1]: l for l in prs if l.split()[1] in self.layers()}, [l for l in prs if l.split()[1] not in self.layers()]

    def test_a_layers_written_pr_text_replaces_its_title_why_and_what_changed_and_compose_keeps_the_facts(self):
        cache = json.loads((self.proj / "results" / "tasks.json").read_text())
        (self.proj / "results" / "tasks.json").write_text(json.dumps({**cache, "feature": {"title": "Build it", "description": "Fixes o/r#9."}}))
        self.pr_file("001-002", self.WRITTEN)
        self.pr_file("groundwork", "# Prepare the build\n\n## Why\nIt needs stubs.\n\n## What changed\n- Add stubs\n\n## Notes\nmore\n")
        prs, rest = self.preview()
        self.assertEqual(rest, [])
        self.assertEqual(prs["compose/p-layer-02"],
                         "pr     compose/p-layer-02  Keep the toggle's state across reloads (2/3)  [written: .compose/p/pr/001-002.md]\n"
                         "    ## Why\n    The toggle forgot its state on every reload.\n    This stores it.\n    - Part of o/r#9\n\n"
                         "    ## What changed\n    - Store the toggle's state\n    - Read it back on load\n\n"
                         "    ## How it was tested\n    - `make 001`\n    - `make 002`\n\n    ## Tickets\n    - ENG-001\n    - ENG-002")
        self.assertIn("Build 003 (3/3)  [generated: write .compose/p/pr/003.md]\n", prs["compose/p-layer-03"])
        self.assertIn("- Fixes o/r#9", prs["compose/p-layer-03"])
        self.assertIn("  [generated: .compose/p/pr/groundwork.md refused: after the title it must be `## Why` then `## What changed`, nothing else]\n",
                      prs["compose/p-layer-01"])
        self.assertNotIn("Prepare the build", prs["compose/p-layer-01"])

    def test_a_pr_text_file_follows_its_tasks_to_a_new_layer_and_one_whose_tasks_were_regrouped_is_never_applied(self):
        self.pr_file("003", self.WRITTEN)
        self.pr_file("001-002", self.WRITTEN.replace("Keep the toggle's", "Show the panel's"))
        prs, rest = self.preview(review_layer_lines=1)  # two groundwork layers, then one layer per task
        self.assertEqual(list(prs), [f"compose/p-layer-0{i}" for i in range(1, 6)])
        self.assertIn("Keep the toggle's state across reloads (5/5)  [written: .compose/p/pr/003.md]", prs["compose/p-layer-05"])
        self.assertIn("[generated: write .compose/p/pr/groundwork-2.md]", prs["compose/p-layer-02"])
        self.assertFalse([l for l in prs.values() if "Show the panel's" in l])
        self.assertEqual(rest, ["pr     .compose/p/pr/001-002.md matches no layer, so no PR uses it; the layers' files are "
                                "groundwork.md, groundwork-2.md, 001.md, 002.md, 003.md"])


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
        f.write_text(LayersBase.WRITTEN.replace("\n\n## Why", "\n\n\n## Why\n").replace("on load\n", "on load\n\n\n"))
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



if __name__ == "__main__":
    unittest.main()
