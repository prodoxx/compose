import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, sys, unittest
from pathlib import Path
from unittest import mock
# tests/test_modules.py fails a test file without the next line
os.environ["HERDR_ENV"] = ""
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_config
import argparse, json, shutil, subprocess, tempfile, time

IDLE_BUFFER = ["  Next I write the tech doc.", "", "────────────", "❯", "────────────", "  Opus 5.5 | ctx: 7%"]
TYPING_BUFFER = ["  Next I write the tech doc.", "", "────────────", "❯ go ahead with the tech doc", "────────────", "  Opus 5.5 | ctx: 7%"]
ROWS = []
real_records = fc.fc_transcript.records


def rec(kind, **k):
    base = {"type": kind, "timestamp": k.pop("at", "2020-01-01T10:00:00.000Z"), "uuid": str(len(ROWS))}
    base.update(k); ROWS.append(base); return base


def write():
    path = Path(tempfile.mkdtemp()) / "s.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in ROWS))
    return path


def now_stamp():
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % int((t % 1) * 1000)


class Gate(unittest.TestCase):
    def test_the_input_line_is_read_from_the_last_prompt_mark(self):
        self.assertEqual(fc.fc_compactor.input_line(IDLE_BUFFER), "")
        self.assertEqual(fc.fc_compactor.input_line(TYPING_BUFFER), "go ahead with the tech doc")
        self.assertIsNone(fc.fc_compactor.input_line(["no prompt here"]))

    def test_an_echoed_prompt_above_the_input_line_does_not_count(self):
        self.assertEqual(fc.fc_compactor.input_line(["❯ the earlier prompt", "  reply", "❯", "  status"]), "")
        self.assertEqual(fc.fc_compactor.input_line(["❯", "  reply", "> typing", "  status"]), "typing")

    def test_text_waiting_in_the_input_line_is_not_quiet(self):
        with mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: {"agent_status": "idle"}), \
                mock.patch.object(fc.fc_herdr, "herdr_text", lambda *a, **k: "\n".join(TYPING_BUFFER)):
            self.assertFalse(fc.fc_compactor.pane_quiet("w1:p2"))
        with mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: {"agent_status": "idle"}), \
                mock.patch.object(fc.fc_herdr, "herdr_text", lambda *a, **k: "\n".join(IDLE_BUFFER)):
            self.assertTrue(fc.fc_compactor.pane_quiet("w1:p2"))
        with mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: {"agent_status": "working"}), \
                mock.patch.object(fc.fc_herdr, "herdr_text", lambda *a, **k: "\n".join(IDLE_BUFFER)):
            self.assertFalse(fc.fc_compactor.pane_quiet("w1:p2"))
        with mock.patch.object(fc.fc_herdr, "herdr", lambda *a, **k: None), mock.patch.object(fc.fc_herdr, "herdr_text", lambda *a, **k: ""):
            self.assertFalse(fc.fc_compactor.pane_quiet("w1:p2"))

    def test_the_handoff_prompts_name_each_roles_sections(self):
        p = fc.fc_compactor
        self.assertIn("`## Decisions`", p.HANDOFF_PROMPTS["manager"])
        self.assertIn("`## Pending go's`", p.HANDOFF_PROMPTS["lead"])
        self.assertTrue(p.HANDOFF_PROMPTS["lead"].startswith("compose: before a compaction, bring your handoff at {path}"))


class Typist(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d)
        self.clock = 0.0
        self.prompts, self.statuses, self.tell_returns = [], [], True
        self.owner_types_after_handoff = self.compose_types_after_handoff = False
        self.compact_lands = True
        self.boundary_meta = {"trigger": "manual", "preTokens": 90000, "postTokens": 30000, "durationMs": 9000}
        self.records_calls = self.handoff_at = self.records_at_handoff = None
        self.records_calls = 0
        ROWS.clear()
        rec("user", message={"role": "user", "content": "go"}, origin={"kind": "human"})
        self.transcript = write()
        for target, name, val in ((fc.fc_config, "CONFIG_PATH", d / "config.json"), (fc.fc_config, "COMPACT", {**fc.fc_config.COMPACT_DEFAULTS, "idle_seconds": 0}),
                                  (fc.fc_herdr, "herdr", self.herdr), (fc.fc_herdr, "herdr_text", lambda *a, **k: "\n".join(IDLE_BUFFER)),
                                  (fc.fc_manager, "tell", self.tell), (time, "sleep", self.sleep), (time, "monotonic", lambda: self.clock),
                                  (fc.fc_transcript, "records", self.counted_records)):
            p = mock.patch.object(target, name, val); p.start(); self.addCleanup(p.stop)
        self.handoff = d / "handoff.md"

    def counted_records(self, path):
        self.records_calls += 1
        return real_records(path)

    def sleep(self, seconds):
        self.clock += seconds  # the waits run on a clock only sleep moves

    def herdr(self, *args, **k):
        if args[:2] != ("agent", "get"):
            return None
        return {"agent_status": self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]}

    def append_row(self, row):
        with open(self.transcript, "a") as f:
            f.write(json.dumps(row) + "\n")

    def tell(self, pane, text):
        if not self.tell_returns:
            return False
        self.prompts.append(text)
        if text.startswith("compose:"):
            self.handoff_at, self.records_at_handoff = self.clock, self.records_calls
            if self.owner_types_after_handoff:
                self.append_row({"type": "user", "timestamp": now_stamp(), "uuid": "o1", "origin": {"kind": "human"}, "message": {"role": "user", "content": "wait, not now"}})
            if self.compose_types_after_handoff:
                fc.fc_compactlog.typed(pane, "compose typed this")
                self.append_row({"type": "user", "timestamp": now_stamp(), "uuid": "c1", "origin": {"kind": "human"}, "message": {"role": "user", "content": "compose typed this"}})
        elif text.startswith("/compact") and self.compact_lands:
            self.append_row({"type": "system", "subtype": "compact_boundary", "timestamp": now_stamp(), "uuid": "b1",
                             "compactMetadata": self.boundary_meta})
        return True

    def run_compactor(self, mode="judged", statuses=("idle",), role="manager"):
        self.statuses = list(statuses)
        fc.fc_compactor.cmd_compactor(argparse.Namespace(pane="w1:p2", role=role, transcript=str(self.transcript), mode=mode, handoff=str(self.handoff), repo="."))

    def test_a_judged_compaction_types_the_handoff_then_the_compact(self):
        self.run_compactor(mode="judged", statuses=["idle", "idle", "working", "idle", "idle", "idle"])
        self.assertEqual([p.split()[0] for p in self.prompts], ["compose:", "/compact"])
        self.assertIn(str(self.handoff), self.prompts[0]); self.assertIn(str(self.handoff), self.prompts[1])
        last = fc.fc_compactlog.read()[-1]
        self.assertEqual((last["kind"], last["trigger"], last["outcome"], last["post"]), ("compaction", "compose", "done", 30000))
        self.assertEqual((last["pre"], last["duration_ms"]), (90000, 9000))
        self.assertIsNone(fc.fc_compactlog.inflight("w1:p2"))
        self.assertEqual(fc.fc_compactlog.pane_state("w1:p2")["baseline"], 30000)

    def test_a_compaction_resets_the_failure_count_and_drops_the_retry_time(self):
        fc.fc_compactlog.save_pane_state("w1:p2", {"baseline": 5, "failures": 3, "retry_after": 99, "last_asked_at": 7})
        self.run_compactor(statuses=["idle", "idle", "working", "idle", "idle", "idle"])
        self.assertEqual(fc.fc_compactlog.pane_state("w1:p2"), {"baseline": 30000, "failures": 0, "last_asked_at": 7})

    def test_the_lead_is_asked_for_its_own_sections(self):
        self.run_compactor(role="lead", statuses=["idle", "idle", "working", "idle", "idle", "idle"])
        self.assertIn("`## Holds`", self.prompts[0])

    def test_an_owner_prompt_after_the_handoff_stops_before_the_compact(self):
        self.owner_types_after_handoff = True
        self.run_compactor(mode="judged", statuses=["idle", "idle", "working", "idle", "idle"])
        self.assertEqual(len(self.prompts), 1)
        last = fc.fc_compactlog.read()[-1]
        self.assertEqual((last["outcome"], last["trigger"]), ("aborted-owner-typing", "compose"))
        self.assertIsNone(fc.fc_compactlog.inflight("w1:p2"))

    def test_a_prompt_compose_typed_itself_does_not_count_as_the_owner(self):
        self.compose_types_after_handoff = True  # the fake appends an owner-looking record whose text was logged with typed()
        self.run_compactor(mode="judged", statuses=["idle", "idle", "working", "idle", "idle", "idle"])
        self.assertEqual(len(self.prompts), 2)

    def test_a_ceiling_compaction_waits_only_seconds(self):
        with mock.patch.object(fc.fc_config, "COMPACT", {**fc.fc_config.COMPACT_DEFAULTS, "idle_seconds": 9999}):
            self.run_compactor(mode="ceiling", statuses=["idle", "idle", "working", "idle", "idle", "idle"])
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(fc.fc_compactlog.read()[-1]["trigger"], "ceiling")
        self.assertGreaterEqual(self.clock, 5)
        self.assertLess(self.clock, 120)

    def test_a_ceiling_hold_is_about_five_seconds_whatever_the_idle_window(self):
        with mock.patch.object(fc.fc_config, "COMPACT", {**fc.fc_config.COMPACT_DEFAULTS, "idle_seconds": 9999}):
            self.run_compactor(mode="ceiling", statuses=["idle"])
        self.assertGreaterEqual(self.handoff_at, 5)
        self.assertLess(self.handoff_at, 10)

    def test_the_quiet_must_hold_for_the_whole_idle_window(self):
        with mock.patch.object(fc.fc_config, "COMPACT", {**fc.fc_config.COMPACT_DEFAULTS, "idle_seconds": 6}):
            self.run_compactor(statuses=["idle"])
        self.assertGreaterEqual(self.handoff_at, 6)
        self.assertLess(self.handoff_at, 10)

    def test_a_poll_that_is_not_quiet_restarts_the_window(self):
        with mock.patch.object(fc.fc_config, "COMPACT", {**fc.fc_config.COMPACT_DEFAULTS, "idle_seconds": 6}):
            self.run_compactor(statuses=["idle", "working", "idle"])  # idle at 0, working at 2, then idle from 4: the window ends at 10, not 6
        self.assertGreaterEqual(self.handoff_at, 10)
        self.assertEqual(len(self.prompts), 2)

    def test_an_owner_prompt_inside_the_window_restarts_it(self):
        base = time.time()
        orig_sleep = self.sleep
        def sleep(seconds):
            orig_sleep(seconds)
            if self.clock == 2:  # the owner submits a prompt mid-window
                self.append_row({"type": "user", "timestamp": now_stamp(), "uuid": "o1", "origin": {"kind": "human"}, "message": {"role": "user", "content": "one more thing"}})
        with mock.patch.object(time, "time", lambda: base + self.clock), mock.patch.object(time, "sleep", sleep), \
                mock.patch.object(fc.fc_config, "COMPACT", {**fc.fc_config.COMPACT_DEFAULTS, "idle_seconds": 6}):
            self.run_compactor(statuses=["idle"])
        self.assertGreaterEqual(self.handoff_at, 10)  # the prompt at 2 voided the window begun at 0; the new one begins at 4 and holds until 10, not 6

    def test_the_transcript_is_read_again_only_when_it_changed(self):
        with mock.patch.object(fc.fc_config, "COMPACT", {**fc.fc_config.COMPACT_DEFAULTS, "idle_seconds": 6}):
            self.run_compactor(statuses=["idle"])
        self.assertEqual(self.records_at_handoff, 1)  # four quiet polls, one read

    def test_the_marker_is_refreshed_through_a_long_idle_gate(self):
        base = time.time()
        seen = []
        orig_sleep = self.sleep
        def sleep(seconds):
            orig_sleep(seconds)
            seen.append((self.clock, fc.fc_compactlog.inflight("w1:p2")))
        with mock.patch.object(time, "time", lambda: base + self.clock), mock.patch.object(time, "sleep", sleep):
            self.run_compactor(statuses=["working"])
        late = [marker for clock, marker in seen if 900 <= clock < 1800]
        self.assertTrue(late)
        self.assertTrue(all(m is not None for m in late))
        self.assertGreater(max(m["at"] for m in late), base + 1700)

    def test_a_boundary_without_post_tokens_keeps_the_old_baseline(self):
        self.boundary_meta = {"trigger": "manual", "preTokens": 90000}
        fc.fc_compactlog.save_pane_state("w1:p2", {"baseline": 5, "failures": 2})
        self.run_compactor(statuses=["idle", "idle", "working", "idle", "idle", "idle"])
        self.assertEqual(fc.fc_compactlog.read()[-1]["outcome"], "done")
        self.assertEqual(fc.fc_compactlog.pane_state("w1:p2"), {"baseline": 5, "failures": 0})

    def test_an_unknown_role_leaves_another_compactors_marker_alone(self):
        fc.fc_compactlog.set_inflight("w1:p2", "compact", 1234, "judged")
        self.run_compactor(role="nobody")
        self.assertEqual(fc.fc_compactlog.read()[-1]["outcome"], "unknown-role")
        self.assertEqual(fc.fc_compactlog.inflight("w1:p2")["pid"], 1234)
        self.assertEqual(self.prompts, [])

    def test_a_refused_prompt_clears_the_marker(self):
        self.tell_returns = False
        self.run_compactor(mode="judged", statuses=["idle", "idle"])
        self.assertEqual(fc.fc_compactlog.read()[-1]["outcome"], "refused")
        self.assertIsNone(fc.fc_compactlog.inflight("w1:p2"))

    def test_a_pane_that_never_goes_quiet_ends_in_gave_up(self):
        self.run_compactor(statuses=["working"])
        self.assertEqual(self.prompts, [])
        last = fc.fc_compactlog.read()[-1]
        self.assertEqual((last["kind"], last["step"], last["outcome"]), ("step", "idle", "gave-up"))
        self.assertGreaterEqual(self.clock, 1800)
        self.assertIsNone(fc.fc_compactlog.inflight("w1:p2"))

    def test_a_pane_that_blocks_after_the_handoff_is_left_alone(self):
        self.run_compactor(statuses=["idle", "idle", "blocked"])
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(fc.fc_compactlog.read()[-1]["outcome"], "not-settled")
        self.assertIsNone(fc.fc_compactlog.inflight("w1:p2"))

    def test_a_compact_that_never_lands_is_a_timeout_and_keeps_the_baseline(self):
        self.compact_lands = False
        fc.fc_compactlog.save_pane_state("w1:p2", {"baseline": 5})
        self.run_compactor(statuses=["idle", "idle", "working", "idle", "idle", "idle"])
        last = fc.fc_compactlog.read()[-1]
        self.assertEqual((last["kind"], last["outcome"], last["post"]), ("compaction", "timeout", None))
        self.assertEqual(fc.fc_compactlog.pane_state("w1:p2")["baseline"], 5)
        self.assertIsNone(fc.fc_compactlog.inflight("w1:p2"))

    def test_an_unexpected_error_is_logged_and_never_raised(self):
        with mock.patch.object(fc.fc_compactor, "wait_quiet", side_effect=RuntimeError("boom")):
            self.run_compactor()
        last = fc.fc_compactlog.read()[-1]
        self.assertEqual((last["kind"], last["outcome"], last["error"]), ("step", "error", "RuntimeError"))
        self.assertIsNone(fc.fc_compactlog.inflight("w1:p2"))

    def test_start_detaches_the_subcommand(self):
        with mock.patch.object(fc.fc_compactor.subprocess, "Popen") as popen:
            popen.return_value.pid = 4242
            pid = fc.fc_compactor.start("w1:p2", "manager", Path("/t.jsonl"), "judged", Path("/h.md"), "/repo")
        self.assertEqual(pid, 4242)
        args, kw = popen.call_args
        argv = args[0]
        self.assertEqual((argv[0], argv[2], argv[3:5]), (sys.executable, "compactor", ["--repo", "/repo"]))
        self.assertTrue(argv[1].endswith("compose.py"))
        self.assertEqual(argv[argv.index("--mode") + 1], "judged")
        self.assertTrue(kw["start_new_session"])
        self.assertEqual((kw["stdin"], kw["stdout"], kw["stderr"]), (subprocess.DEVNULL,) * 3)


class Watching(unittest.TestCase):
    def test_a_transcript_is_parsed_again_only_after_it_grew(self):
        path = Path(tempfile.mkdtemp()) / "s.jsonl"
        path.write_text(json.dumps({"type": "user"}) + "\n")
        calls = []
        def counted(p):
            calls.append(p); return real_records(p)
        with mock.patch.object(fc.fc_transcript, "records", counted):
            w = fc.fc_compactor.Watched(path)
            self.assertEqual((len(w.rows()), len(w.rows())), (1, 1))
            self.assertEqual(len(calls), 1)
            with open(path, "a") as f:
                f.write(json.dumps({"type": "user"}) + "\n")
            self.assertEqual(len(w.rows()), 2)
            self.assertEqual(len(calls), 2)
            path.unlink()
            self.assertEqual(w.rows(), [])  # a failed stat reads again
            self.assertEqual(len(calls), 3)


class Registered(unittest.TestCase):
    def test_the_subcommand_takes_its_flags(self):
        p = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "compose.py"), "compactor", "--help"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        for flag in ("--pane", "--role", "--transcript", "--mode", "--handoff"):
            self.assertIn(flag, p.stdout)


if __name__ == "__main__":
    unittest.main()
