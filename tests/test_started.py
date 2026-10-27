import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, subprocess, sys, time, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_started


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class Started(unittest.TestCase):
    """What compose starts is recorded per pid under the home, oldest first."""
    def dir(self):
        return Path.home() / ".compose" / "started"

    def test_round_trip_holds_every_field_and_leaves_no_tmp(self):
        with mock.patch("time.time", return_value=1234.5):
            fc_started.record(os.getpid(), "test", "proj", waiter=7, until=99.0, cwd="/w")
        want = {"pid": os.getpid(), "kind": "test", "project": "proj", "waiter": 7, "until": 99.0, "cwd": "/w", "at": 1234.5}
        self.assertEqual(json.loads((self.dir() / f"{os.getpid()}.json").read_text()), want)
        self.assertEqual(fc_started.entries(), [{**want, "alive": True}])
        self.assertEqual([p.name for p in self.dir().iterdir()], [f"{os.getpid()}.json"])

    def test_oldest_first_and_a_bad_file_is_skipped(self):
        for pid, at in ((30, 3.0), (10, 1.0), (20, 2.0)):
            with mock.patch("time.time", return_value=at):
                fc_started.record(pid, "k", "p")
        (self.dir() / "40.json").write_text("{not json")
        self.assertEqual([e["pid"] for e in fc_started.entries()], [10, 20, 30])

    def test_alive_follows_the_process(self):
        fc_started.record(os.getpid(), "k", "p")
        fc_started.record(dead_pid(), "k", "p")
        self.assertEqual(sorted(e["alive"] for e in fc_started.entries()), [False, True])

    def test_forget_removes_one_record_and_a_missing_one_is_fine(self):
        fc_started.record(1, "k", "p")
        fc_started.record(2, "k", "p")
        fc_started.forget(1)
        fc_started.forget(1)
        fc_started.forget(999999)
        self.assertEqual([e["pid"] for e in fc_started.entries()], [2])
