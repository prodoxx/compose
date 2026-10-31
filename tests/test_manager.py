import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, sys, tempfile, unittest
from types import SimpleNamespace
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

OLD, NEW = "w1:p1", "w1:p2"


class FakeHerdr:
    """The herdr calls the manager seat makes: which panes hold a live agent, their labels, and the prompts sent."""
    def __init__(self, live, labels=None, blocked=()):
        self.live, self.labels, self.blocked, self.prompts = list(live), dict(labels or {}), set(blocked), []

    def __call__(self, *args):
        if args[:2] == ("agent", "list"):
            return {"agents": [{"pane_id": p, "agent_status": "idle", "workspace_id": "w1", "terminal_title_stripped": f"title {p}"} for p in self.live]}
        if args[:2] == ("agent", "prompt"):
            if args[2] in self.blocked:
                return None
            self.prompts.append((args[2], args[3]))
            return {}
        if args[:2] == ("pane", "get"):
            return {"pane": {"label": self.labels.get(args[2])}}
        if args[:2] == ("pane", "list"):
            return {"panes": [{"pane_id": p, "label": l} for p, l in self.labels.items()]}
        if args[:2] == ("pane", "rename"):
            if args[3] == "--clear":
                self.labels.pop(args[2], None)
            else:
                self.labels[args[2]] = args[3]
            return {}


class Seat(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        self.repo = Path(d.name).resolve()
        self.projs = {}
        for name in ("chat-ui", "billing", "docs"):
            p = self.repo / ".compose" / name
            (p / "results").mkdir(parents=True)
            self.projs[name] = (self.repo, p)
        self.herdr = FakeHerdr([OLD, NEW])
        for target, name, new in ((fc.fc_core, "all_projects", lambda repo: self.projs), (fc.fc_herdr, "herdr_env", lambda: True),
                                  (fc.fc_herdr, "herdr", lambda *a: self.herdr(*a))):
            pa = mock.patch.object(target, name, new); pa.start(); self.addCleanup(pa.stop)

    def proj(self, name):
        return self.projs[name][1]

    def as_session(self, pane, **env):
        return mock.patch.dict(os.environ, {"HERDR_PANE_ID": pane, **env}, clear=True)

    def claim(self, pane, name, takeover=False):
        err = io.StringIO()
        with self.as_session(pane), redirect_stderr(err):
            fc.fc_manager.claim(self.proj(name), takeover, self.repo)
        return err.getvalue()

    def owner(self, name):
        return fc.fc_herdr.herdr_manager_record(self.proj(name)).get("session")

    def seat_factory(self, pane):
        with fc.fc_factorystate.edit(self.repo) as state:
            state["manager_pane"] = pane


class Takeover(Seat):
    def test_the_first_claim_labels_the_pane_and_tells_nobody(self):
        self.assertEqual(self.claim(OLD, "chat-ui"), "")
        self.assertEqual(self.herdr.labels, {OLD: "manager: chat-ui"})
        self.assertEqual(self.herdr.prompts, [])

    def test_a_takeover_tells_the_old_session_naming_the_new_pane_and_what_moved(self):
        self.claim(OLD, "chat-ui")
        said = self.claim(NEW, "chat-ui", takeover=True)
        self.assertEqual(self.owner("chat-ui"), NEW)
        (pane, text), = self.herdr.prompts
        self.assertEqual(pane, OLD)
        self.assertIn(f"herdr pane {NEW} took over chat-ui", text)
        self.assertIn("Stop every `compose watch`", text)
        self.assertIn("ask the product owner nothing more", text)
        self.assertEqual(said, f"took over chat-ui from the engineering manager session in herdr pane {OLD}; it was told to stop.\n")

    def test_a_takeover_carries_every_project_the_old_session_held_and_the_factory(self):
        for name in ("chat-ui", "billing"):
            self.claim(OLD, name)
        self.seat_factory(OLD)
        self.claim(NEW, "chat-ui", takeover=True)
        self.assertEqual((self.owner("chat-ui"), self.owner("billing")), (NEW, NEW))
        self.assertEqual(fc.fc_factorystate.load_state(self.repo)["manager_pane"], NEW)
        self.assertEqual(self.herdr.labels, {NEW: "manager: chat-ui, billing"})
        self.assertIn("chat-ui, billing and the factory", self.herdr.prompts[0][1])

    def test_a_takeover_leaves_what_another_session_holds_alone(self):
        self.claim(OLD, "chat-ui")
        self.herdr.live.append("w1:p3")
        self.claim("w1:p3", "docs")
        self.claim(NEW, "chat-ui", takeover=True)
        self.assertEqual(self.owner("docs"), "w1:p3")

    def test_the_old_record_is_kept_so_the_old_session_can_be_told_later(self):
        self.claim(OLD, "chat-ui")
        self.claim(NEW, "chat-ui", takeover=True)
        self.assertEqual(fc.fc_herdr.herdr_manager_record(self.proj("chat-ui"))["from"]["session"], OLD)

    def test_an_old_pane_at_a_question_cannot_be_prompted_and_the_words_say_so(self):
        self.herdr.blocked.add(OLD)
        self.claim(OLD, "chat-ui")
        said = self.claim(NEW, "chat-ui", takeover=True)
        self.assertEqual(self.owner("chat-ui"), NEW)
        self.assertIn("could not be prompted", said)

    def test_without_takeover_a_second_session_is_refused_and_nobody_is_told(self):
        self.claim(OLD, "chat-ui")
        with self.assertRaises(SystemExit):
            self.claim(NEW, "chat-ui")
        self.assertEqual((self.owner("chat-ui"), self.herdr.prompts), (OLD, []))

    def test_a_gone_owner_is_replaced_without_a_prompt(self):
        self.claim(OLD, "chat-ui")
        self.herdr.live = [NEW]
        self.claim(NEW, "chat-ui", takeover=True)
        self.assertEqual((self.owner("chat-ui"), self.herdr.prompts), (NEW, []))


class OwnerLines(Seat):
    def test_the_session_that_lost_the_project_gets_owner_taken_in_a_watch_and_in_now(self):
        self.claim(OLD, "chat-ui")
        self.claim(NEW, "chat-ui", takeover=True)
        with self.as_session(OLD):
            (line,) = fc.fc_manager.owner_lines(self.proj("chat-ui"), watching=True)
            (now,) = fc.fc_manager.owner_lines(self.proj("chat-ui"))
        self.assertTrue(line.startswith(f"event owner=taken project=chat-ui pane={NEW} "), line)
        self.assertEqual(now, line)
        self.assertTrue(fc.fc_watch.URGENT.search(line))

    def test_a_viewer_who_never_held_it_gets_owner_other_only_in_now(self):
        self.claim(OLD, "chat-ui")
        self.herdr.live.append("w1:p3")
        with self.as_session("w1:p3"):
            (line,) = fc.fc_manager.owner_lines(self.proj("chat-ui"))
        self.assertTrue(line.startswith(f"event owner=other pane={OLD} "), line)

    def test_the_holder_a_teammate_and_an_unheld_project_get_nothing(self):
        self.claim(OLD, "chat-ui")
        with self.as_session(OLD):
            self.assertEqual(fc.fc_manager.owner_lines(self.proj("chat-ui"), watching=True), [])
            self.assertEqual(fc.fc_manager.owner_lines(self.proj("docs"), watching=True), [])
        with self.as_session(NEW, COMPOSE_ROLE="orchestrator"):
            self.assertEqual(fc.fc_manager.owner_lines(self.proj("chat-ui"), watching=True), [])

    def test_a_filtered_watch_still_passes_the_line(self):
        line = "event owner=taken project=chat-ui pane=w1:p2 since='x'  (...)"
        self.assertEqual(fc.fc_watch.watch_filter([line], "chat-ui", ["hw-orchestrator"]), [line])


class ParkedWatch(Seat):
    """The old manager's own watch, still parked on the project, wakes with the news instead of sitting there."""
    SNAP = {"tasks": {"001": {"state": "pending", "attempts": 1, "cost": 0.0, "note": "", "linear": "", "title": "t"}}, "agents": {},
            "crew": {}, "run": False, "ended": None, "verify": None, "halt": None, "run_end": 0, "last": [0, ""]}

    def watch(self, pane, snapshot=None, **kw):
        a = SimpleNamespace(repo=str(self.repo), now=False, from_=None, timeout=0.5, interval=0.01, stall=10_000, rewake=False, settle=0, takeover=False)
        a.__dict__.update(kw)
        out = []
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj("chat-ui")), mock.patch.object(fc.fc_watch, "watch_snapshot", snapshot or (lambda *_a, **_k: self.SNAP)), \
                mock.patch("builtins.print", lambda *m, file=None: out.append(" ".join(map(str, m))) if file is None else None), self.as_session(pane):
            try:
                fc.fc_watch.cmd_watch(a)
            except SystemExit as e:
                out.append(f"exit {e.code}")
        return out

    def test_a_watch_that_lost_the_project_ends_with_owner_taken(self):
        self.claim(OLD, "chat-ui")
        self.claim(NEW, "chat-ui", takeover=True)
        out = self.watch(OLD, now=True)
        self.assertTrue(any(l.startswith(f"event owner=taken project=chat-ui pane={NEW}") for l in out), out)

    def test_a_parked_watch_wakes_the_moment_another_session_takes_the_project(self):
        self.claim(OLD, "chat-ui")
        calls = []
        def snapshot(*_a, **_k):
            calls.append(1)
            if len(calls) == 3:  # the product owner takes over from the other pane while this watch is parked
                self.claim(NEW, "chat-ui", takeover=True)
            return self.SNAP
        out = self.watch(OLD, snapshot, timeout=5)
        self.assertEqual(len(out), 1, out)
        self.assertIn(f"event owner=taken project=chat-ui pane={NEW} ", out[0])

    def test_the_new_managers_watch_is_not_told_it_lost_anything(self):
        self.claim(OLD, "chat-ui")
        self.claim(NEW, "chat-ui", takeover=True)
        out = self.watch(NEW)
        self.assertFalse(any("owner=" in l for l in out), out)
        self.assertEqual(self.owner("chat-ui"), NEW)

    def test_a_watch_takes_the_project_through_the_manager_claim(self):
        self.claim(OLD, "chat-ui")
        self.watch(NEW, takeover=True)
        self.assertEqual((self.owner("chat-ui"), self.herdr.prompts[0][0]), (NEW, OLD))


class FactorySeat(Seat):
    def claim(self, pane, takeover=False):
        err = io.StringIO()
        with self.as_session(pane), redirect_stderr(err):
            fc.fc_manager.factory_claim(self.repo, takeover)
        return err.getvalue()

    def seat(self):
        return fc.fc_factorystate.load_state(self.repo)["manager_pane"]

    def test_the_first_watcher_takes_the_seat_and_the_same_one_again_changes_nothing(self):
        self.claim(OLD)
        self.claim(OLD)
        self.assertEqual((self.seat(), self.herdr.prompts), (OLD, []))

    def test_a_second_live_session_is_refused_naming_the_first(self):
        self.claim(OLD)
        with self.assertRaises(SystemExit) as e:
            self.claim(NEW)
        self.assertIn(OLD, str(e.exception))
        self.assertIn("--takeover", str(e.exception))
        self.assertEqual(self.seat(), OLD)

    def test_takeover_moves_the_seat_and_tells_the_old_session(self):
        self.claim(OLD)
        said = self.claim(NEW, takeover=True)
        self.assertEqual(self.seat(), NEW)
        (pane, text), = self.herdr.prompts
        self.assertEqual(pane, OLD)
        self.assertIn("took over the factory", text)
        self.assertIn("it was told to stop", said)

    def test_a_seat_whose_pane_is_gone_is_taken_without_a_prompt(self):
        self.claim(OLD)
        self.herdr.live = [NEW]
        self.claim(NEW)
        self.assertEqual((self.seat(), self.herdr.prompts), (NEW, []))

    def test_a_session_with_no_pane_leaves_the_seat_alone(self):
        self.claim(OLD)
        with mock.patch.dict(os.environ, {}, clear=True):
            fc.fc_manager.factory_claim(self.repo, False)
        self.assertEqual(self.seat(), OLD)


class SeatLines(Seat):
    def lines(self, pane):
        with self.as_session(pane):
            return fc.fc_manager.seat_lines(self.repo, [p for _, p in self.projs.values()])

    def test_nothing_outside_herdr_with_no_records(self):
        with mock.patch.object(fc.fc_herdr, "herdr_env", lambda: False):
            self.assertEqual(self.lines(OLD), [])

    def test_the_manager_session_sees_where_it_lives_and_what_it_runs(self):
        self.claim(OLD, "chat-ui")
        self.claim(OLD, "billing")
        self.seat_factory(OLD)
        out = self.lines(OLD)
        self.assertEqual(len(out), 2, out)
        self.assertTrue(out[0].startswith(f"engineering manager  herdr pane {OLD} (idle, workspace w1, 'title {OLD}', labelled 'manager: chat-ui, billing') runs chat-ui, billing, the factory"), out[0])
        self.assertTrue(out[0].endswith("<- this session"))
        self.assertTrue(out[1].startswith("engineering manager  none yet for docs"), out[1])

    def test_another_session_is_told_where_the_manager_is_and_how_to_take_over(self):
        self.claim(OLD, "chat-ui")
        out = self.lines(NEW)
        self.assertIn(f"herdr pane {OLD}", out[0])
        self.assertNotIn("this session", out[0])
        self.assertTrue(any(f"this session is herdr pane {NEW} and runs nothing here" in l and "--takeover" in l for l in out), out)

    def test_a_gone_manager_pane_says_so(self):
        self.claim(OLD, "chat-ui")
        self.herdr.live = [NEW]
        self.assertIn("(gone", self.lines(NEW)[0])

    def test_each_project_says_which_session_manages_it(self):
        self.claim(OLD, "chat-ui")
        agents, _ = fc.fc_manager.panes()
        with self.as_session(NEW):
            self.assertEqual(fc.fc_manager.project_line(self.proj("chat-ui"), agents), f"  manager  herdr pane {OLD} (live)")
            self.assertEqual(fc.fc_manager.project_line(self.proj("docs"), agents), "  manager  none yet")
        with self.as_session(OLD):
            self.assertEqual(fc.fc_manager.project_line(self.proj("chat-ui"), agents), f"  manager  herdr pane {OLD} (this session)")
        self.assertEqual(fc.fc_manager.project_line(self.proj("chat-ui"), {}), f"  manager  herdr pane {OLD} (gone)")
        with mock.patch.object(fc.fc_herdr, "herdr_env", lambda: False):
            self.assertEqual(fc.fc_manager.project_line(self.proj("docs"), None), "")


if __name__ == "__main__":
    unittest.main()
