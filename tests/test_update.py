import hermetic  # first: no test sees the real home
import io, json, os, shutil, subprocess, sys, tempfile, time, unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import fc_core, fc_init, fc_links, fc_pin, fc_update

done = lambda out="", err="", code=0: SimpleNamespace(stdout=out, stderr=err, returncode=code)


class Fixture(unittest.TestCase):
    def setUp(self):  # git is faked at fc_core.sh: `answers` maps a git subcommand to what it prints, `ran` lists what the update asked
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); root = Path(os.path.realpath(t.name))
        self.co = root / "my checkout"; self.co.mkdir()
        self.answers, self.ran = {}, []
        p = mock.patch.object(fc_core, "sh", self.sh); p.start(); self.addCleanup(p.stop)
        self.relink = mock.Mock(return_value=["linked compose CLI link"])
        self.live = root / "live"  # the registry of runs and verifies: this test's, not the machine's
        p = mock.patch.object(fc_update, "live_dir", return_value=self.live); p.start(); self.addCleanup(p.stop)
        self.pins = root / "pins"  # the version pins of this test's projects, not the machine's
        p = mock.patch.object(fc_pin, "pins_dir", return_value=self.pins); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(fc_update.fc_herdr, "live_agents", lambda: []); p.start(); self.addCleanup(p.stop)
    def sh(self, cmd, cwd, check=True):
        self.ran.append(" ".join(cmd[1:])); return self.answers.get(cmd[1], done())
    def pulled(self): return "pull --ff-only" in self.ran
    def update(self, doctor_code=0, force=False):
        def doctor(a):
            print("doctor says hi"); sys.exit(doctor_code) if doctor_code else None
        out = io.StringIO()  # sys.exit("message") prints it and exits 1
        with mock.patch.object(fc_update, "__file__", str(self.co / "bin" / "fc_update.py")), \
             mock.patch.object(fc_links, "relink_owned", self.relink), mock.patch.object(fc_init, "cmd_doctor", doctor), \
             redirect_stdout(out), redirect_stderr(out), self.assertRaises(SystemExit) as e:
            fc_update.cmd_update(mock.Mock(force=force))
        code = e.exception.code
        return (1, out.getvalue() + code + "\n") if isinstance(code, str) else (code or 0, out.getvalue())


class Update(Fixture):
    def test_fast_forward_relinks_then_runs_doctor_and_exits_with_its_code(self):
        code, out = self.update()
        self.assertEqual(code, 0); self.assertTrue(self.pulled())
        self.relink.assert_called_once_with(self.co)
        self.assertLess(out.index("linked compose CLI link"), out.index("doctor says hi"))
        self.assertEqual(self.update(doctor_code=1)[0], 1)  # already up to date: still relinks and runs doctor
        self.assertEqual(self.relink.call_count, 2)

    def test_local_changes_or_untracked_files_stop_before_pulling(self):
        for dirty in (" M a", "?? new"):
            with self.subTest(dirty):
                self.answers["status"] = done(dirty + "\n")
                code, out = self.update()
                self.assertEqual(code, 1); self.assertFalse(self.pulled())
                line = next(l for l in out.splitlines() if str(self.co) in l)
                self.assertRegex(line, "commit|stash"); self.relink.assert_not_called()

    def diverge(self, *commits):  # the pull cannot fast-forward, and `git cherry` lists these (mark, sha, subject)
        self.answers["pull"] = done(err="fatal: Not possible to fast-forward, aborting.", code=128)
        p = mock.patch.object(fc_update, "local_commits", return_value=("main", "origin/main", list(commits))); p.start(); self.addCleanup(p.stop)

    def test_commits_only_here_are_rebased_onto_upstream_and_the_update_goes_on(self):
        self.diverge(("+", "abcdef0123456789", "c"))
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn("rebase origin/main", self.ran)
        self.assertIn("main had 1 commit origin/main lacked; rebased onto it, 0 already upstream dropped", out)
        self.assertRegex(out, r"\+ abcdef012345 c\b.*only here"); self.assertIn("stay only in this checkout", out)
        self.relink.assert_called_once_with(self.co); self.assertIn("doctor says hi", out)

    def test_commits_already_upstream_under_another_hash_are_dropped_not_replayed(self):
        self.diverge(("-", "abcdef0123456789", "c, reworded here"))
        code, out = self.update()
        self.assertEqual(code, 0, out); self.assertRegex(out, r"- \w+ c, reworded here.*already on origin/main")
        self.assertIn("1 already upstream dropped", out); self.assertNotIn("stay only in this checkout", out)

    def test_a_rebase_that_conflicts_is_aborted_and_the_state_and_fix_are_printed(self):
        self.diverge(("+", "abcdef0123456789", "mine"))
        self.answers["rebase"] = done("CONFLICT (content): Merge conflict in a\n", code=1); self.answers["rev-parse"] = done("a1b2c3d\n")
        code, out = self.update()
        self.assertEqual(code, 1); self.relink.assert_not_called(); self.assertIn("rebase --abort", self.ran)
        self.assertIn("stopped", out); self.assertIn("back at a1b2c3d", out)
        self.assertIn("main has 1 commit that origin/main does not", out)
        self.assertRegex(out, r"\+ abcdef012345 mine.*only here"); self.assertIn(f"git -C '{self.co}' rebase origin/main", out)
        self.assertIn("Then run compose update again.", out)

    def test_local_commits_with_an_unreachable_remote_are_not_rebased_over_a_failed_fetch(self):
        self.diverge(("+", "abcdef0123456789", "c"))
        self.answers["pull"] = done(err="fatal: '/x/gone.git' does not appear to be a git repository", code=128); self.answers["fetch"] = done(code=128)
        code, out = self.update()
        self.assertEqual(code, 1); self.relink.assert_not_called(); self.assertNotIn("rebased", out)
        self.assertNotIn("rebase origin/main", self.ran)
        self.assertEqual(len([l for l in out.splitlines() if "gone.git" in l]), 1, out)

    def test_other_pull_failures_print_gits_message_on_one_line_and_relink_nothing(self):
        self.answers["pull"] = done(err="fatal: '/x/gone.git' does not appear to be a git repository\nfatal: Could not read", code=128)
        with mock.patch.object(fc_update, "local_commits", return_value=None):
            code, out = self.update()
        self.assertEqual(code, 1); self.relink.assert_not_called()
        self.assertEqual(len([l for l in out.splitlines() if "gone.git" in l]), 1, out)


class Freeze(Fixture):
    """A run or verify live anywhere on this machine freezes the CLI: `compose update` names it and changes nothing, `--force` overrides."""
    def setUp(self):
        super().setUp()
        self.proc = subprocess.Popen(["sleep", "60"]); self.addCleanup(self.proc.wait); self.addCleanup(self.proc.kill)
    def register(self, pid, kind="run", project="shop", name=None):
        self.live.mkdir(exist_ok=True)
        (self.live / f"{name or pid}.json").write_text(json.dumps({"pid": pid, "kind": kind, "project": project, "repo": "/work/shop", "since": "10:02"}))

    def test_a_live_run_or_verify_stops_the_update_before_it_pulls_naming_the_project(self):
        for kind in ("run", "verify"):
            with self.subTest(kind):
                self.register(self.proc.pid, kind)
                code, out = self.update()
                self.assertEqual(code, 1); self.assertFalse(self.pulled()); self.relink.assert_not_called()
                self.assertIn(f"shop: {kind} live since 10:02 (pid {self.proc.pid}) in /work/shop", out); self.assertIn("--force", out)

    def test_force_updates_anyway(self):
        self.register(self.proc.pid)
        code, out = self.update(force=True)
        self.assertEqual(code, 0, out); self.assertTrue(self.pulled())

    def test_nothing_live_updates_as_before_and_a_dead_runs_file_is_ignored_and_removed(self):
        dead = subprocess.Popen(["true"]); dead.wait(); self.register(dead.pid)
        code, out = self.update()
        self.assertEqual(code, 0, out); self.assertTrue(self.pulled()); self.assertEqual(list(self.live.glob("*.json")), [])

    def test_a_broken_registry_file_is_skipped(self):
        self.register(self.proc.pid, name="x"); (self.live / "junk.json").write_text("{not json")
        self.assertEqual([w["pid"] for w in fc_update.live_work()], [self.proc.pid])

    def test_hold_registers_this_process_until_it_exits(self):
        script = ("import sys, json, pathlib; sys.path.insert(0, sys.argv[1]); import fc_update; "
                  "fc_update.live_dir = lambda: pathlib.Path(sys.argv[2]); "
                  "fc_update.hold('verify', 'shop', '.'); print(json.dumps(fc_update.live_work()))")
        out = subprocess.run([sys.executable, "-c", script, str(Path(fc_update.__file__).parent), str(self.live)],
                             capture_output=True, text=True, check=True).stdout
        [held] = json.loads(out); self.assertEqual((held["kind"], held["project"]), ("verify", "shop")); self.assertEqual(held["repo"], os.path.realpath("."))
        self.assertEqual(fc_update.live_work(), [])  # the process exited: its file is gone

    def test_hold_records_when_it_started_and_the_verify_stages_working_now(self):
        """`compose status` reads elapsed time and the stage names from the hold file; herdr_stage reports each stage change to it."""
        script = ("import sys, json, pathlib, time; sys.path.insert(0, sys.argv[1]); import fc_update, fc_herdr; "
                  "fc_update.live_dir = lambda: pathlib.Path(sys.argv[2]); fc_update.hold('verify', 'shop', '.'); "
                  "fc_herdr.herdr_stage({}, 'checks', 'working'); fc_herdr.herdr_stage({}, 'e2e login', 'working'); fc_herdr.herdr_stage({}, 'checks', 'idle'); "
                  "print(json.dumps(fc_update.live_work()))")
        out = subprocess.run([sys.executable, "-c", script, str(Path(fc_update.__file__).parent), str(self.live)],
                             capture_output=True, text=True, check=True).stdout
        [held] = json.loads(out)
        self.assertAlmostEqual(held["started"], time.time(), delta=60)
        self.assertEqual(list(held["stages"]), ["e2e login"])
        self.assertAlmostEqual(held["stages"]["e2e login"], time.time(), delta=60)

    def test_a_stage_change_in_a_process_that_holds_nothing_writes_nothing(self):
        with mock.patch.object(fc_update, "live_dir", return_value=self.live):
            fc_update.hold_stage("checks", "working")
        self.assertEqual(list(self.live.glob("*.json")), [])


class Overlap(unittest.TestCase):
    """A second run, resume or verify started on a project whose first is still alive (also after its last line): it stops before it empties the log the first writes."""
    def setUp(self):
        self.repo, self.proj = Path("/work/shop"), Path("/work/shop/.compose/p")
        self.cfg = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.cfg, True)
        p = mock.patch.object(fc_update, "live_dir", return_value=self.cfg / "live"); p.start(); self.addCleanup(p.stop)
        c = fc_core.Checkout((self.repo, self.repo / ".git", self.repo / ".git"))
        p = mock.patch.object(fc_core, "checkout", return_value=c); p.start(); self.addCleanup(p.stop)  # every path is in the one repository
        self.proc = subprocess.Popen(["sleep", "60"]); self.addCleanup(self.proc.wait); self.addCleanup(self.proc.kill)
    def hold(self, kind, pid=None):
        (self.cfg / "live").mkdir(parents=True, exist_ok=True)
        pid = pid or self.proc.pid
        (self.cfg / "live" / f"{pid}.json").write_text(json.dumps({"pid": pid, "kind": kind, "project": "p", "repo": str(self.repo), "since": "12:00"}))
    def test_a_start_beside_a_live_one_stops_before_it_touches_anything(self):
        for kind, cmd in (("run", "run"), ("run", "resume"), ("resume", "run"), ("verify", "verify")):
            with self.subTest(f"{kind} then {cmd}"):
                self.hold(kind)
                with self.assertRaises(SystemExit) as e:
                    fc_update.refuse_while_running(cmd, self.proj, self.repo)
                self.assertIn(f"{kind} is already running on p (pid {self.proc.pid}, since 12:00)", str(e.exception))

    def test_a_different_project_or_a_dead_process_does_not_stop_a_start(self):
        self.hold("run"); fc_update.refuse_while_running("run", Path("/work/shop/.compose/q"), self.repo)
        dead = subprocess.Popen(["true"]); dead.wait(); self.hold("run", dead.pid); (self.cfg / "live" / f"{self.proc.pid}.json").unlink()
        fc_update.refuse_while_running("run", self.proj, self.repo)


class Pins(Fixture):
    """A project pins the compose version it starts on; an update refuses while one of its teammates is mid-turn, and once it has
    pulled it names, per project, the skill files that changed since the pin, then moves the pin."""
    def setUp(self):
        super().setUp()
        self.skill = Path(tempfile.mkdtemp()); (self.skill / "sections").mkdir()
        for n in ("SKILL.md", "sections/0b.md", "sections/3.md"):
            (self.skill / n).write_text(f"{n} v1\n")
        p = mock.patch.object(fc_pin.fc_hooks, "SKILL_DIR", self.skill); p.start(); self.addCleanup(p.stop)
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"; (self.proj / "results").mkdir(parents=True)

    def test_pin_records_once_and_renew_moves_it(self):
        d = fc_pin.pin(self.proj)
        self.assertEqual((d["project"], d["repo"]), ("shop", str(self.proj.resolve().parents[1])))
        self.assertEqual(sorted(fc_pin.skill_name(f) for f in d["skill"]), ["SKILL.md", "sections/0b.md", "sections/3.md"])
        (self.skill / "sections/3.md").write_text("v2\n")
        self.assertEqual(fc_pin.pin(self.proj), d)  # kept: the project runs on what it started with
        self.assertNotEqual(fc_pin.pin(self.proj, renew=True)["skill"], d["skill"])
        self.assertEqual([p["project"] for p in fc_pin.pinned()], ["shop"])

    def test_a_pin_of_a_cleaned_project_is_dropped(self):
        fc_pin.pin(self.proj)
        shutil.rmtree(self.proj.parents[1])
        self.assertEqual(fc_pin.pinned(), [])
        self.assertEqual(list(self.pins.glob("*.json")), [])

    def test_a_teammate_mid_turn_stops_the_update_and_force_overrides(self):
        fc_pin.pin(self.proj)
        agents = [{"name": "shop-tests-003", "agent_status": "working"}, {"name": "shop-orchestrator", "agent_status": "idle"}, {"name": "other-planner", "agent_status": "working"}]
        self.assertEqual(fc_update.mid_turn(fc_pin.pinned(), agents), [("shop", "shop-tests-003")])
        with mock.patch.object(fc_update.fc_herdr, "live_agents", lambda: agents):
            code, out = self.update()
            self.assertEqual(code, 1); self.assertFalse(self.pulled())
            self.assertIn("shop: shop-tests-003 is mid-turn", out); self.assertIn("between phases", out)
            code, out = self.update(force=True)
            self.assertEqual(code, 0, out); self.assertTrue(self.pulled())

    def test_the_update_names_the_changed_skill_files_per_project_and_moves_the_pin(self):
        before = fc_pin.pin(self.proj)
        quiet = Path(tempfile.mkdtemp()) / ".compose" / "blog"; quiet.mkdir(parents=True); fc_pin.pin(quiet)
        (self.skill / "sections/3.md").write_text("v2\n"); (self.skill / "sections/0h.md").write_text("new\n")
        fc_pin.pin(quiet, renew=True)  # blog started after the change: nothing to re-read
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn(f"update  shop: since its pin ({before['commit']}) these skill files changed: sections/0h.md, sections/3.md. Tell its team lead: "
                      '"compose updated: re-read sections/0h.md, sections/3.md; nothing else changed"', out)
        self.assertIn("update  blog: no skill file changed since its pin", out)
        self.assertEqual(fc_pin.pin(self.proj)["skill"], fc_pin.pin(quiet)["skill"])  # both pins moved to the installed version
