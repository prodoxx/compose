import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, sys, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

ORCH = "hw-orchestrator"
stamp = lambda: time.strftime("%Y-%m-%d %H:%M:%S")


def snap(tasks=None, crew=None, verify=None, halt=None):
    return {"tasks": {tid: {"state": st, "attempts": 1, "cost": 0.0, "note": "", "linear": "", "title": f"task {tid}"}
                      for tid, st in (tasks or {}).items()},
            "agents": {}, "crew": crew or {}, "run": False, "ended": None, "verify": verify, "halt": halt, "run_end": 0, "last": [0, ""]}


class Yolo(unittest.TestCase):
    """Yolo is a posture in a file: who answers a product-owner decision while they are away, and what they read on return."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)
        self.file = self.proj / "results" / "yolo.json"

    def yolo(self, *words, push=False, clear_words=False, snapshot=None, pane="w1:p1"):
        """Run as the engineering manager session in herdr pane `pane`."""
        out = io.StringIO()
        a = SimpleNamespace(repo=".", project="hw", push=push, clear_words=clear_words, words=list(words), cmd="yolo")
        env = {k: v for k, v in os.environ.items() if k not in ("COMPOSE_ROLE", "HERDR_PANE_ID")} | {"HERDR_PANE_ID": pane}
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(fc.fc_watch, "watch_snapshot", return_value=snapshot or snap()), \
                mock.patch.object(fc.fc_worktree, "stack_order", return_value=[]), \
                mock.patch.object(fc.fc_cost, "rows", return_value=[]), contextlib.redirect_stdout(out):
            fc.fc_yolo.cmd_yolo(a)
        return out.getvalue()

    def test_entry_writes_the_file_and_reads_the_words_back(self):
        out = self.yolo("use", "Stripe;", "skip", "the", "design", "review", push=True)
        d = json.loads(self.file.read_text())
        self.assertEqual(d["words"], "use Stripe; skip the design review")
        self.assertTrue(d["push"])
        self.assertEqual(d["decisions"], [])
        self.assertTrue(d["since"])
        self.assertIn("use Stripe; skip the design review", out)
        self.assertIn("push: yes", out)
        self.assertIn("never pre-authorizable whatever the words say", out)
        self.assertIn("force-push", out)
        self.assertNotIn(chr(0x2014), out)

    def test_reentry_replaces_the_words_and_keeps_since_and_decisions(self):
        self.yolo("first")
        d = json.loads(self.file.read_text())
        d["since"] = "2026-01-01 09:00:00"
        d["decisions"] = [{"ts": "2026-01-01 09:30:00", "text": "db?: postgres"}]
        self.file.write_text(json.dumps(d))
        out = self.yolo("second")
        d = json.loads(self.file.read_text())
        self.assertEqual((d["since"], d["words"], len(d["decisions"])), ("2026-01-01 09:00:00", "second", 1))
        self.assertIn("push: no", out)

    def test_clear_words_empties_them_and_keeps_since_push_and_decisions(self):
        self.yolo("ship", push=True)
        self.yolo("decided", "db?: postgres")
        since = json.loads(self.file.read_text())["since"]
        out = self.yolo(clear_words=True)
        d = json.loads(self.file.read_text())
        self.assertEqual((d["words"], d["since"], d["push"], [x["text"] for x in d["decisions"]]), ("", since, True, ["db?: postgres"]))
        self.assertIn("words cleared", out)
        for words, push in ((["new"], False), ([], True)):
            with self.assertRaises(SystemExit):
                self.yolo(*words, push=push, clear_words=True)
        self.assertEqual(json.loads(self.file.read_text()), d)

    def test_clear_words_without_yolo_exits_1(self):
        with self.assertRaises(SystemExit) as e:
            self.yolo(clear_words=True)
        self.assertEqual(e.exception.code, 1)
        self.assertFalse(self.file.exists())

    def test_words_that_look_like_a_command_are_refused_with_a_quoting_hint(self):
        self.yolo("ship", "it", push=True)
        d = json.loads(self.file.read_text())
        for words in (["--project hw decided db?: postgres"], ["--repo . --project hw ship it"], ["decided --project hw db?: pg"],
                      ["db?", "decided", "postgres"]):
            with self.assertRaises(SystemExit) as e:
                self.yolo(*words)
            self.assertIn("quote", str(e.exception.code))
            self.assertIn('compose yolo decided --project hw "<question>: <answer taken>"', str(e.exception.code))
        self.assertEqual(json.loads(self.file.read_text()), d)
        self.yolo("the", "decidedly", "safe", "option")
        self.assertEqual(json.loads(self.file.read_text())["words"], "the decidedly safe option")

    def test_decided_appends(self):
        self.yolo()
        self.yolo("decided", "which db?: postgres, the recommendation")
        self.yolo("decided", "cards NEB-4:", "accept-all")
        d = json.loads(self.file.read_text())
        self.assertEqual([x["text"] for x in d["decisions"]], ["which db?: postgres, the recommendation", "cards NEB-4: accept-all"])
        self.assertTrue(all(x["ts"] for x in d["decisions"]))

    def test_digest_sections_render_the_same_digest(self):
        self.yolo("go", "with", "Stripe")
        self.yolo("decided", "db?: postgres")
        y = json.loads(self.file.read_text())
        s = snap(tasks={"001": "passed"}, halt=["2026-01-01 10:00:00", "openrouter", 1])
        (self.proj / "results" / "001.json").write_text("{}")
        with mock.patch.object(fc.fc_watch, "watch_snapshot", return_value=s), mock.patch.object(fc.fc_worktree, "stack_order", return_value=[]), \
                mock.patch.object(fc.fc_cost, "rows", return_value=[]):
            sections = fc.fc_yolo.digest_sections(Path("."), self.proj, y)
            text = fc.fc_yolo.yolo_digest(Path("."), self.proj, y, "digest")
        want = [f"{y['mode']} digest for hw: on since {y['since']}"]
        for head, items in sections:
            want += ["", head] + [f"- {i}" for i in items or ["none"]]
        self.assertEqual(text, "\n".join(want))
        self.assertEqual([h for h, _ in sections], ["Your instructions", "Decided for you", "To confirm", "Waiting on you", "Tried and failed", "Done", "Cost"])

    def test_off_prints_the_digest_from_disk_then_archives(self):
        self.yolo("go", "with", "Stripe")
        d = json.loads(self.file.read_text())
        d["since"] = "2026-01-01 09:00:00"
        self.file.write_text(json.dumps(d))
        self.yolo("decided", "provider?: Stripe")
        for tid in ("001", "002", "003"):
            (self.proj / "results" / f"{tid}.json").write_text("{}")
        crew = {ORCH: [{"at": "2025-12-31 10:00:00", "state": "needs-user", "message": "before yolo"},
                       {"at": stamp(), "state": "needs-user", "message": "delete prod data?"}],
                "hw-q": [{"at": stamp(), "state": "blocked", "message": "answered later"},
                         {"at": stamp(), "state": "working", "message": "going again"}]}
        s = snap(tasks={"001": "passed", "002": "failed", "003": "rejected_size"}, crew=crew,
                 verify=[int(time.time()), False], halt=["2026-01-01 10:00:00", "openrouter", 2])
        out = self.yolo("off", snapshot=s)
        heads = ["Your instructions", "Decided for you", "To confirm", "Waiting on you", "Tried and failed", "Done", "Cost"]
        pos = [out.index(h) for h in heads]
        self.assertEqual(pos, sorted(pos), out)
        sec = lambda h: out.split(h, 1)[1].split(heads[heads.index(h) + 1], 1)[0] if h != "Cost" else out.split(h, 1)[1]
        self.assertIn("go with Stripe", sec("Your instructions"))
        self.assertIn("provider?: Stripe", sec("Decided for you"))
        self.assertIn("(yolo recommendation, unconfirmed, <date>)", sec("To confirm"))
        self.assertIn("1 decision(s)", sec("To confirm"))
        waiting = sec("Waiting on you")
        self.assertIn("delete prod data?", waiting)
        self.assertNotIn("before yolo", waiting)
        self.assertNotIn("answered later", waiting)
        self.assertIn("openrouter", waiting)
        self.assertIn("verify FAIL", waiting)
        failed = sec("Tried and failed")
        self.assertIn("002", failed)
        self.assertIn("003", failed)
        self.assertNotIn("001", failed)
        self.assertIn("001", sec("Done"))
        self.assertFalse(self.file.exists())
        self.assertTrue((self.proj / "results" / "yolo-2026-01-01T090000.json").exists())

    def test_off_with_no_decisions_has_nothing_to_confirm(self):
        self.yolo("go")
        out = self.yolo("off")
        self.assertEqual(out.split("To confirm", 1)[1].split("Waiting on you", 1)[0].split(), ["-", "none"])

    def test_decided_points_at_compose_decide_which_tags_the_map_line(self):
        self.yolo("go")
        out = self.yolo("decided", "which db?: postgres")
        self.assertIn('`compose decide --project hw --new "<decision>" --closes <id>` (no --quote under yolo)', out)
        self.assertIn('"D<n>: <decision> (closes <id>) (yolo recommendation', out)
        self.assertNotIn("card", out)
        self.assertIn(f"(yolo recommendation, unconfirmed, {time.strftime('%Y-%m-%d')})", out)
        self.assertIn("never as the product owner's", out)

    def test_off_and_decided_without_yolo_exit_1(self):
        for words in (["off"], ["decided", "x: y"]):
            with self.assertRaises(SystemExit) as e:
                self.yolo(*words)
            self.assertEqual(e.exception.code, 1)

    def test_manager_attention_stays_silent_for_needs_user_and_fires_for_blocked(self):
        self.yolo()
        calls = []
        ev = [f"event crew={ORCH} state=needs-user message='Stripe or Adyen? I recommend Stripe'"]
        env = {k: v for k, v in os.environ.items() if k != "COMPOSE_ROLE"}
        with mock.patch.object(fc.fc_crew, "desktop_notify", lambda *args: calls.append(args)), mock.patch.dict(os.environ, env, clear=True):
            fc.fc_crew.manager_attention(self.proj, ev)
            self.assertEqual(calls, [])
            fc.fc_crew.manager_attention(self.proj, [f"event crew={ORCH} state=blocked message='no key'"])
        self.assertEqual(calls, [("hw: the engineering manager is blocked", f"no key  (from {ORCH})")])

    def watch_now(self, s, pane="w1:p1"):
        """`watch --now` as the engineering manager session in herdr pane `pane`."""
        out = []
        a = SimpleNamespace(repo=".", project="hw", now=True, from_=None, timeout=0, interval=0.01, stall=10_000)
        env = {k: v for k, v in os.environ.items() if k not in ("COMPOSE_ROLE", "HERDR_PANE_ID")} | {"HERDR_PANE_ID": pane}
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_watch, "watch_snapshot", return_value=s), \
                mock.patch("builtins.print", out.append), mock.patch.dict(os.environ, env, clear=True):
            fc.fc_watch.cmd_watch(a)
        return out

    def test_watch_now_prints_yolo_first_and_marks_needs_user(self):
        self.assertFalse(any("yolo" in line for line in self.watch_now(snap())))
        self.yolo("ship", "it", push=True)
        self.file.write_text(json.dumps(json.loads(self.file.read_text()) | {"mode": "yolo"}))
        out = self.watch_now(snap(crew={"hw-q": [{"at": stamp(), "state": "needs-user", "message": "db?"}]}))
        self.assertTrue(out[0].startswith("event yolo=on push=yes since="), out)
        self.assertIn("by='this session'", out[0])
        self.assertIn("words='ship it'", out[0])
        line = next(l for l in out if "state=needs-user" in l)
        self.assertTrue(line.endswith("  yolo=on (answer with the recommendation; log it: compose yolo decided ...)"), line)

    def test_watch_under_a_night_posture_leaves_needs_user_to_the_night_answerer(self):
        self.file.write_text(json.dumps({"since": stamp(), "decisions": [], "mode": "night", "push": False, "words": "", "by": {"role": "night", "session": None}}))
        out = self.watch_now(snap(crew={"hw-q": [{"at": stamp(), "state": "needs-user", "message": "db?"}]}))
        line = next(l for l in out if "state=needs-user" in l)
        self.assertTrue(line.endswith("  night=on (the night answerer takes it: do not answer; wait for factory=answered|waiting|answer-failed)"), line)
        self.assertNotIn("decided", line)

    def test_another_session_sees_who_set_yolo(self):
        """yolo is per project: a second engineering manager session on it is told the posture is not its own."""
        self.yolo("ship", "it", pane="w1:p2")
        self.file.write_text(json.dumps(json.loads(self.file.read_text()) | {"mode": "yolo"}))
        self.assertEqual(json.loads(self.file.read_text())["by"], {"role": "manager", "session": "w1:p2"})
        out = self.watch_now(snap(crew={"hw-q": [{"at": stamp(), "state": "needs-user", "message": "db?"}]}), pane="w1:p1")
        self.assertIn("by='an engineering manager session in herdr pane w1:p2'", out[0])
        line = next(l for l in out if "state=needs-user" in l)
        self.assertTrue(line.endswith("  yolo=on set by an engineering manager session in herdr pane w1:p2 (answer with the recommendation; "
                                      "log it: compose yolo decided ...)"), line)

    def test_status_shows_the_header(self):
        self.yolo(push=True)
        d = json.loads(self.file.read_text())
        since = d["since"]
        self.file.write_text(json.dumps(d | {"mode": "yolo"}))
        with mock.patch.object(fc.fc_tasks, "load_tasks", return_value=[]), mock.patch.object(fc.fc_core, "current_branch", return_value="main"), \
                mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(stdout="abc\n", returncode=0)), \
                mock.patch.object(fc.fc_worktree, "stack_order", return_value=[]), mock.patch.object(fc.fc_cost, "rows", return_value=[]):
            lines = fc.fc_status.render_status(Path("."), [self.proj]).splitlines()
            self.assertIn(f"  yolo on (push) since {since}, set by an engineering manager session in herdr pane w1:p1", lines)  # the status tab is no session's
            self.file.write_text(json.dumps({"since": since, "push": False, "words": "", "decisions": []}))  # from before `by` was recorded
            lines = fc.fc_status.render_status(Path("."), [self.proj]).splitlines()
        self.assertIn(f"  away on since {since}", lines)

    def test_help_and_the_slash_command_list_yolo(self):
        root = Path(__file__).resolve().parents[1]
        cmd = (root / "commands" / "yolo.md").read_text()
        self.assertIn("allowed-tools: Bash(compose yolo:*)", cmd)
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["compose", "--help"]), contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
            fc.main()
        self.assertIn("yolo", out.getvalue())

    def test_main_parses_decided_after_the_project_flag(self):
        seen = []
        with mock.patch.object(sys, "argv", ["compose", "yolo", "decided", "--project", "hw", "db?: postgres"]), \
                mock.patch.object(fc.fc_yolo, "cmd_yolo", seen.append), mock.patch.object(fc.fc_config, "apply_config", return_value="default"):
            fc.main()
        self.assertEqual(seen[0].words, ["decided", "db?: postgres"])

    def test_main_parses_clear_words_as_a_flag(self):
        seen = []
        with mock.patch.object(sys, "argv", ["compose", "yolo", "--project", "hw", "--clear-words"]), \
                mock.patch.object(fc.fc_yolo, "cmd_yolo", seen.append), mock.patch.object(fc.fc_config, "apply_config", return_value="default"):
            fc.main()
        self.assertEqual((seen[0].clear_words, seen[0].words), (True, []))


class StatusNamesPosture(unittest.TestCase):
    """The posture file's mode, `away` or `yolo`, is what `yolo_state` and `compose status` name; a file without one reads as away."""
    SINCE = "2026-01-01 09:00:00"

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)
        self.file = self.proj / "results" / "yolo.json"

    def write(self, **extra):
        self.file.write_text(json.dumps({"since": self.SINCE, "push": False, "words": "", "decisions": []} | extra))

    def status(self):
        with mock.patch.object(fc.fc_tasks, "load_tasks", return_value=[]), mock.patch.object(fc.fc_core, "current_branch", return_value="main"), \
                mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(stdout="abc\n", returncode=0)), \
                mock.patch.object(fc.fc_worktree, "stack_order", return_value=[]), mock.patch.object(fc.fc_cost, "rows", return_value=[]):
            return fc.fc_status.render_status(Path("."), [self.proj]).splitlines()

    def test_yolo_state_fills_away_and_keeps_a_stored_mode(self):
        self.write()
        self.assertEqual(fc.fc_tasks.yolo_state(self.proj)["mode"], "away")
        for mode in ("away", "yolo"):
            self.write(mode=mode)
            self.assertEqual(fc.fc_tasks.yolo_state(self.proj)["mode"], mode)
        self.assertEqual(fc.fc_tasks.yolo_state(self.proj)["since"], self.SINCE)

    def test_yolo_state_is_none_for_a_missing_or_unreadable_file(self):
        self.assertIsNone(fc.fc_tasks.yolo_state(self.proj))
        self.file.write_text("{not json")
        self.assertIsNone(fc.fc_tasks.yolo_state(self.proj))

    def test_status_names_yolo_with_push_and_who(self):
        self.write(mode="yolo", push=True, by={"role": "manager", "session": "w1:p1"})
        self.assertIn(f"  yolo on (push) since {self.SINCE}, set by an engineering manager session in herdr pane w1:p1", self.status())

    def test_status_names_away_for_a_file_without_mode_or_by(self):
        self.write()
        self.assertIn(f"  away on since {self.SINCE}", self.status())

    def test_status_names_away_with_push_and_who_for_a_stored_mode(self):
        self.write(mode="away", push=True, by={"role": "manager", "session": "w1:p1"})
        self.assertIn(f"  away on (push) since {self.SINCE}, set by an engineering manager session in herdr pane w1:p1", self.status())

    def test_status_says_nothing_when_no_posture_is_on(self):
        self.assertFalse([l for l in self.status() if l.startswith(("  away on", "  yolo on"))])


class WatchNamesPosture(unittest.TestCase):
    """`watch --now` names the posture in its event line and in the hint on each needs-user line; a file without a mode reads as away."""
    SINCE = "2026-01-01 09:00:00"
    HINT = "(answer with the recommendation; log it: compose {m} decided ...)"
    CREW = {"hw-q": [{"at": stamp(), "state": "needs-user", "message": "db?"}], "hw-r": [{"at": stamp(), "state": "blocked", "message": "no key"}]}

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)

    def watch_now(self, posture=True, **file):
        """`watch --now` in herdr pane w1:p1 with a posture file holding `file`; returns the event, needs-user, and blocked lines."""
        if posture:
            (self.proj / "results" / "yolo.json").write_text(json.dumps({"since": self.SINCE, "push": False, "words": "", "decisions": []} | file))
        a = SimpleNamespace(repo=".", project="hw", now=True, from_=None, timeout=0, interval=0.01, stall=10_000)
        env = {k: v for k, v in os.environ.items() if k not in ("COMPOSE_ROLE", "HERDR_PANE_ID")} | {"HERDR_PANE_ID": "w1:p1"}
        out = []
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_watch, "watch_snapshot", return_value=snap(crew=self.CREW)), \
                mock.patch("builtins.print", out.append), mock.patch.dict(os.environ, env, clear=True):
            fc.fc_watch.cmd_watch(a)
        return out[0], next(l for l in out if "state=needs-user" in l), next(l for l in out if "state=blocked" in l)

    def test_the_event_names_the_mode_and_a_file_without_one_reads_as_away(self):
        for mode, push in (("yolo", True), ("away", False), (None, True)):
            event, *_ = self.watch_now(**({"mode": mode} if mode else {}), push=push)
            self.assertTrue(event.startswith(f"event {mode or 'away'}=on push={'yes' if push else 'no'} since="), event)

    def test_the_needs_user_line_ends_with_the_mode_hint_and_other_lines_do_not(self):
        for mode in ("away", "yolo"):
            for by in ({"role": "manager", "session": "w1:p1"}, None):
                _, ask, blocked = self.watch_now(mode=mode, **({"by": by} if by else {}))
                self.assertTrue(ask.endswith(f"  {mode}=on {self.HINT.format(m=mode)}"), ask)
                self.assertNotIn("=on", blocked)

    def test_another_session_is_named_after_the_mode_under_each_mode(self):
        for mode in ("away", "yolo"):
            event, ask, _ = self.watch_now(mode=mode, by={"role": "manager", "session": "w1:p2"})
            self.assertIn("by='an engineering manager session in herdr pane w1:p2'", event)
            self.assertTrue(ask.endswith(f"  {mode}=on set by an engineering manager session in herdr pane w1:p2 {self.HINT.format(m=mode)}"), ask)

    def test_no_line_names_a_posture_when_none_is_on(self):
        event, ask, blocked = self.watch_now(posture=False)
        self.assertFalse([l for l in (event, ask, blocked) if "away=on" in l or "yolo=on" in l])


class PostureEntry(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)
        self.file = self.proj / "results" / "yolo.json"

    def put(self, mode, **extra):
        self.file.write_text(json.dumps({"since": "2026-01-01 09:00:00", "push": False, "words": "", "mode": mode,
                                         "decisions": [{"ts": "2026-01-01 09:30:00", "text": "db?: postgres"}]} | extra))

    def run_as(self, cmd, *words, push=False, clear_words=False):
        out = io.StringIO()  # stdout, plus the exit message when cmd_yolo exits
        a = SimpleNamespace(repo=".", project="hw", push=push, clear_words=clear_words, words=list(words), cmd=cmd)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_watch, "watch_snapshot", return_value=snap()), \
                mock.patch.object(fc.fc_worktree, "stack_order", return_value=[]), mock.patch.object(fc.fc_cost, "rows", return_value=[]), contextlib.redirect_stdout(out):
            try:
                fc.fc_yolo.cmd_yolo(a)
            except SystemExit as e:
                print(e.code)
        return out.getvalue()

    def test_entry_stores_the_mode_and_names_it_in_the_entry_and_back_lines(self):
        for p in ("away", "yolo"):
            self.file.unlink(missing_ok=True)
            out = self.run_as(p, "ship", "it")
            d = json.loads(self.file.read_text())
            self.assertEqual(d["mode"], p)
            self.assertIn(f"{p} on for hw since {d['since']}: the engineering manager answers with the recommendation and logs each answer\n", out)
            self.assertIn(f"back: `compose {p} off --project hw` prints what happened", out)
            self.assertIn(fc.fc_yolo.YOLO_NEVER, out)
            self.assertEqual("yolo" in out, p == "yolo")

    def test_switching_keeps_since_decisions_and_words_and_push_unless_both_were_off(self):
        saved = lambda: json.loads(self.file.read_text())
        for old, new in (("away", "yolo"), ("yolo", "away")):
            self.put(old, push=True, words="ship it")
            before = saved()
            self.run_as(new)
            self.assertEqual({k: saved()[k] for k in before}, before | {"mode": new}, "keeps since, decisions, words, and push")
            self.run_as(old, "fresh", "words")
            self.assertEqual((saved()["mode"], saved()["words"], saved()["push"]), (old, "fresh words", True))
            for was, now in ((False, False), (False, True)):
                self.put(old, push=was)
                self.run_as(new, push=now)
                self.assertEqual((saved()["mode"], saved()["push"]), (new, was or now))

    def test_decided_and_the_digest_name_the_posture_and_its_tag(self):
        for p in ("away", "yolo"):
            self.put(p)
            out = self.run_as(p, "decided", "which db?: postgres")
            self.assertIn(f'`compose decide --project hw --new "<decision>" --closes <id>` (no --quote under {p})', out)
            self.assertIn(f"({p} recommendation, unconfirmed, {time.strftime('%Y-%m-%d')})", out)
            confirm = self.run_as(p, "off").split("To confirm", 1)[1].split("Waiting on you", 1)[0]
            self.assertIn(f"({p} recommendation, unconfirmed, <date>)", confirm)
            self.assertEqual("yolo" in out + confirm, p == "yolo")

    def test_the_refusals_name_the_posture(self):
        for p in ("away", "yolo"):
            self.file.unlink(missing_ok=True)
            for words, clear in (("off", False), ("decided", False), ("", True)):
                self.assertIn(f"{p} is not on for hw; `compose {p} --project hw [--push] [words]` turns it on", self.run_as(p, *words.split(), clear_words=clear))
            self.put(p)
            msgs = [self.run_as(p, "decided"), self.run_as(p, "x", clear_words=True), self.run_as(p, "--project hw decided db?: postgres")]
            self.assertIn(f'{p} decided needs "<question>: <answer taken>"', msgs[0])
            self.assertIn(f"compose {p} --project hw --clear-words", msgs[1])
            self.assertTrue(all(h in msgs[2] for h in (f'compose {p} --project hw [--push] "<words>"', f'compose {p} decided --project hw "<question>: <answer taken>"', f"mislaid `{p} decided`")), msgs[2])
            self.assertEqual("yolo" in "".join(msgs), p == "yolo")


class PostureMismatchAndDigest(unittest.TestCase):
    SINCE = "2026-01-01 09:00:00"

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        (self.proj / "results").mkdir(parents=True)
        self.file = self.proj / "results" / "yolo.json"

    def put(self, mode):
        self.file.write_text(json.dumps({"since": self.SINCE, "push": True, "words": "w", "decisions": [{"ts": "2026-01-01 09:30:00", "text": "db?: postgres"}]}
                                        | ({"mode": mode} if mode else {})))

    def run_as(self, cmd, *words, clear_words=False):
        """Everything the command printed, plus its exit code (None when it did not exit)."""
        out, code = io.StringIO(), None
        a = SimpleNamespace(repo=".", project="hw", push=False, clear_words=clear_words, words=list(words), cmd=cmd)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_watch, "watch_snapshot", return_value=snap()), \
                mock.patch.object(fc.fc_worktree, "stack_order", return_value=[]), mock.patch.object(fc.fc_cost, "rows", return_value=[]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                fc.fc_yolo.cmd_yolo(a)
            except SystemExit as e:
                code = e.code
        if isinstance(code, str):
            print(code, file=out)
        return out.getvalue(), code

    def test_the_other_postures_words_refuse_with_one_line_and_leave_the_file(self):
        for on, p in (("yolo", "away"), ("away", "yolo"), (None, "yolo")):  # no mode in the file reads as away
            for word in ("off", "digest", "decided", "--clear-words"):
                self.put(on)
                before = self.file.read_bytes()
                out, code = self.run_as(p, *({"decided": ["decided", "db?: postgres"], "--clear-words": []}.get(word, [word])), clear_words=word == "--clear-words")
                self.assertEqual(out, f"{on or 'away'} is on for hw, not {p}; run `compose {on or 'away'} {word} --project hw`\n")
                self.assertTrue(code == 1 or isinstance(code, str), code)
                self.assertEqual(self.file.read_bytes(), before)

    def test_a_night_posture_points_at_factory_day(self):
        for p in ("away", "yolo"):
            for word in ("off", "digest", "decided", "--clear-words"):
                self.put("night")
                before = self.file.read_bytes()
                out, code = self.run_as(p, *({"decided": ["decided", "db?: postgres"], "--clear-words": []}.get(word, [word])), clear_words=word == "--clear-words")
                self.assertEqual(out, f"night is on for hw, not {p}; run `compose factory day`\n")
                self.assertEqual(self.file.read_bytes(), before)

    def test_digest_prints_its_headline_and_sections_and_leaves_the_file_and_nothing_on_exits_1(self):
        for p in ("away", "yolo"):
            self.put(p)
            before = self.file.read_bytes()
            out, code = self.run_as(p, "digest")
            self.assertEqual(out.splitlines()[:3], [f"{p} digest for hw: on since {self.SINCE}", "", "Your instructions"])
            self.assertIn("2026-01-01 09:30:00  db?: postgres", out)
            self.assertIsNone(code)
            self.assertEqual(self.file.read_bytes(), before)
            self.file.unlink()
            out, code = self.run_as(p, "digest")
            self.assertEqual((out, code), (f"{p} is not on for hw; `compose {p} --project hw [--push] [words]` turns it on\n", 1))

    def test_off_prints_its_headline_first_then_archives_and_entering_the_other_posture_switches(self):
        for p, other in (("away", "yolo"), ("yolo", "away")):
            self.put(p)
            out, code = self.run_as(p, "off")
            self.assertEqual(out.splitlines()[0], f"{p} off for hw: on since {self.SINCE}")
            self.assertFalse(self.file.exists())
            self.assertTrue((self.proj / "results" / "yolo-2026-01-01T090000.json").exists())
            self.put(p)
            self.run_as(other)
            self.assertEqual(json.loads(self.file.read_text())["mode"], other)


class AwaySubcommand(unittest.TestCase):
    TAIL = "[--push] [words] enters, `decided \"<q>: <a>\"` logs an answer taken, --clear-words empties the words, `digest` prints the digest, `off` prints it and archives"
    AWAY = "the product owner is away until their first message: " + TAIL
    YOLO = "the engineering manager answers until an explicit off, and the product owner may steer: " + TAIL
    WORDS = "what pre-answers questions, verbatim; or `decided <q>: <a>`; or `digest`; or `off`"

    def parse(self, *argv):
        seen = []
        with mock.patch.object(sys, "argv", ["compose", *argv]), mock.patch.object(fc.fc_yolo, "cmd_yolo", seen.append), \
                mock.patch.object(fc.fc_config, "apply_config", return_value="default"):
            fc.main()
        self.assertEqual(len(seen), 1)
        return seen[0]

    def help(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["compose", *argv, "--help"]), mock.patch.dict(os.environ, {"COLUMNS": "400"}), \
                contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
            fc.main()
        return " ".join(out.getvalue().split())

    def test_each_name_reaches_cmd_yolo_with_its_own_cmd_and_the_same_parse(self):
        for name in ("away", "yolo"):
            a = self.parse(name, "--project", "hw", "--push", "ship", "it")
            self.assertEqual((a.cmd, a.push, a.clear_words, a.words), (name, True, False, ["ship", "it"]))
            a = self.parse(name, "decided", "--project", "hw", "db?: postgres")
            self.assertEqual((a.cmd, a.push, a.words), (name, False, ["decided", "db?: postgres"]))
            a = self.parse(name, "--project", "hw", "--clear-words")
            self.assertEqual((a.cmd, a.clear_words, a.words), (name, True, []))
            self.assertEqual(self.parse(name, "--project", "hw", "off").words, ["off"])
            self.assertEqual(self.parse(name, "--project", "hw").words, [])

    def test_extra_words_after_the_flags_fold_into_words_for_both(self):
        for name in ("away", "yolo"):
            self.assertEqual(self.parse(name, "digest", "--project", "hw").words, ["digest"])
            self.assertEqual(self.parse(name, "--project", "hw", "--push", "a", "b").words, ["a", "b"])

    def test_top_help_lists_away_right_before_yolo_with_both_helps(self):
        self.assertIn(f" away {self.AWAY} yolo {self.YOLO} ", self.help())

    def test_each_help_shows_the_words_help(self):
        for name in ("away", "yolo"):
            self.assertIn(self.WORDS, self.help(name))


if __name__ == "__main__":
    unittest.main()
