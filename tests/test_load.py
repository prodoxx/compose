import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, subprocess, sys, time, unittest
from pathlib import Path
from unittest import mock
# tests/test_modules.py fails a test file without the next line
os.environ["HERDR_ENV"] = ""
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_config, fc_load
class LoadState(unittest.TestCase):
    def setUp(self):
        saved = dict(vars(fc_config)); self.addCleanup(lambda: vars(fc_config).update(saved))  # apply_config sets module globals
        for p in (mock.patch("os.cpu_count", return_value=8), mock.patch.dict(os.environ)):
            p.start(); self.addCleanup(p.stop)
        os.environ.pop("COMPOSE_NO_LOAD", None)

    def apply(self, load):
        with mock.patch.object(fc_config, "read_config", return_value={"load": load}): fc_config.apply_config(move=False)

    def test_limits_follow_cores_and_config_wins(self):
        self.assertEqual(fc_load.limits(), {"cores": 8, "high": 12.0, "low": 8.0, "warn": 10.0})
        with mock.patch("os.cpu_count", return_value=None): self.assertEqual(fc_load.limits()["cores"], 1)
        with mock.patch.object(fc_config, "LOAD", {"high": 20, "low": 5}): self.assertEqual(fc_load.limits(), {"cores": 8, "high": 20, "low": 5, "warn": 10.0})

    def test_hold_above_high_release_below_low_nothing_written_between(self):
        f = Path.home() / ".compose" / "load.json"
        s = fc_load.update(avg=12.0)  # at high is not above it
        self.assertEqual((s["held"], s["since"], s["n"], s["log"], s["new"], s["avg"], f.exists()), (False, None, 0, [], [], 12.0, False))
        s = fc_load.update(avg=13.0); e = s["new"][0]; self.assertEqual((s["held"], s["n"], s["since"], s["log"], e["n"], e["kind"], e["avg"], type(e["at"])), (True, 1, e["at"], [e], 1, "held", 13.0, float))
        before = f.read_text()
        with mock.patch("os.getloadavg", return_value=(9.0, 9.0, 9.0)):  # held() reads the load: pin it between low and warn
            for avg in (9.0, 8.0, 14.0): self.assertEqual((fc_load.update(avg=avg)["new"], fc_load.held()), ([], True))
        self.assertEqual(f.read_text(), before)
        s = fc_load.update(avg=7.0)
        self.assertEqual((s["held"], s["since"], s["n"], [x["kind"] for x in s["log"]]), (False, None, 2, ["held", "released"]))
        self.assertEqual((fc_load.since(1), fc_load.since(2), fc_load.since(0)), (s["new"], [], s["log"]))

    def test_state_is_replaced_whole_from_a_temp_file_in_the_same_directory(self):
        f, seen = Path.home() / ".compose" / "load.json", []
        real = os.replace
        def replace(src, dst):
            seen.append((Path(src).parent, Path(dst), f.read_text() if f.exists() else None, json.loads(Path(src).read_text())["n"]))  # the target is still the old file; the temp is whole
            real(src, dst)
        with mock.patch("os.replace", replace):
            fc_load.update(avg=13.0); fc_load.claim(1)
        self.assertEqual([(d, t, n) for d, t, _, n in seen], [(f.parent, f, 1)] * 2)
        self.assertEqual((seen[0][2], json.loads(seen[1][2])["held"], sorted(p.name for p in f.parent.iterdir() if p.name.endswith(".tmp"))), (None, True, []))

    def test_log_keeps_the_last_50(self):
        for i in range(60): fc_load.update(avg=13.0 if i % 2 == 0 else 7.0)
        s = fc_load.update(avg=13.0); self.assertEqual((s["n"], len(s["log"]), s["log"][0]["n"], s["log"][-1]["n"]), (61, 50, 12, 61))

    def test_two_processes_see_one_state(self):
        code = "import fc_load; from unittest import mock\nwith mock.patch('os.cpu_count', return_value=8): fc_load.update(avg=1000.0)"
        subprocess.run([sys.executable, "-c", code], env={**os.environ, "HOME": str(Path.home()), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "bin")}, check=True)
        s = fc_load.update(avg=9.0); self.assertEqual((s["held"], s["n"], s["new"]), (True, 1, []))

    def test_off_switch_and_held_honor_env_and_force(self):
        self.assertEqual((fc_load.off(), fc_load.off(force=True)), (False, True))
        with mock.patch("os.getloadavg", return_value=(13.0, 13.0, 13.0)):
            self.assertEqual((fc_load.held(), fc_load.held(force=True), fc_load.update()["avg"]), (True, False, 13.0))  # update() reads os.getloadavg
            for v, want in (("1", True), ("0", False)):
                os.environ["COMPOSE_NO_LOAD"] = v; self.assertEqual((fc_load.off(), fc_load.held()), (want, not want))

    def test_config_load_is_validated(self):
        self.apply({"high": 3, "low": 1.5}); self.assertEqual(fc_config.LOAD, {"high": 3, "low": 1.5})
        for load, msg in (({"high": 0}, "load.high must be a number above 0, not 0"), ({"low": "x"}, "load.low must be a number above 0, not 'x'"),
                          ({"warn": -1}, "load.warn must be a number above 0, not -1"), ({"high": 5, "low": 5}, "load.low must be below load.high")):
            with self.assertRaises(SystemExit) as c: self.apply(load)
            self.assertEqual(c.exception.code, f"{fc_config.CONFIG_PATH}: {msg}")


class LoadWait(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch("os.cpu_count", return_value=8), mock.patch.dict(os.environ), mock.patch("time.sleep")):
            m = p.start(); self.addCleanup(p.stop)
        self.sleep = m; os.environ.pop("COMPOSE_NO_LOAD", None)

    def run_wait(self, avgs, **kw):
        said = []; it = iter(avgs)
        with mock.patch.object(fc_load, "update", side_effect=lambda avg=None: {"held": (a := next(it)) > 8.0, "avg": a}) as upd:
            return fc_load.wait(said.append, **kw), said, upd

    def test_held_says_once_polls_until_release_and_returns_the_wait(self):
        waited, said, upd = self.run_wait([13.0, 13.5, 12.0, 9.0, 7.0])
        self.assertEqual(said, ["load 13.0 on 8 cores is above 12.0: waiting until it falls below 8.0"])
        self.assertEqual((upd.call_count, self.sleep.call_args_list), (5, [mock.call(fc_load.WAIT_POLL)] * 4))
        self.assertGreaterEqual(waited, 0.0)

    def test_off_switch_and_force_never_wait(self):
        for env, kw in (({"COMPOSE_NO_LOAD": "1"}, {}), ({}, {"force": True})):
            with mock.patch.dict(os.environ, env):
                waited, said, upd = self.run_wait([99.0], **kw)
            self.assertEqual((waited, said, upd.call_count, self.sleep.call_count), (0.0, [], 0, 0))

class LoadWarn(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch("os.cpu_count", return_value=8), mock.patch.dict(os.environ)):
            p.start(); self.addCleanup(p.stop)  # warn is 10.0, low 8.0, high 12.0
        os.environ.pop("COMPOSE_NO_LOAD", None)
        self.file = Path.home() / ".compose" / "load.json"

    def state(self): return json.loads(self.file.read_text())

    def test_warn_logs_once_when_crossed_and_cleared_below_low_nothing_between(self):
        s = fc_load.update(avg=9.9); self.assertEqual((bool(s.get("warn")), s["new"], self.file.exists()), (False, [], False))
        s = fc_load.update(avg=10.0); e = s["new"][0]  # at warn counts
        self.assertEqual((s["warn"], s["held"], s["n"], e["n"], e["kind"], e["avg"]), (True, False, 1, 1, "warn", 10.0))
        before = self.file.read_text()
        for avg in (11.5, 10.0, 9.0, 8.0): self.assertEqual((fc_load.update(avg=avg)["new"], self.state()["warn"]), ([], True))
        self.assertEqual(self.file.read_text(), before)
        s = fc_load.update(avg=7.9); self.assertEqual((s["warn"], s["n"], [x["kind"] for x in s["new"]]), (False, 2, ["cleared"]))
        self.assertEqual(fc_load.update(avg=7.0)["new"], [])
        s = fc_load.update(avg=11.0); self.assertEqual((s["warn"], s["n"], [x["kind"] for x in s["log"]]), (True, 3, ["warn", "cleared", "warn"]))

    def test_claim_is_true_once_per_entry_and_kept_in_the_state(self):
        self.assertEqual((fc_load.claim(1), fc_load.claim(1), fc_load.claim(2), fc_load.claim(2), fc_load.claim(1)), (True, False, True, False, False))
        self.assertEqual(self.state()["claimed"], [1, 2])
        fc_load.update(avg=11.0); fc_load.claim(3); s = self.state()  # update and claim keep each other's fields
        self.assertEqual((s["claimed"], s["warn"]), ([1, 2, 3], True))
        fc_load.update(avg=11.5); self.assertEqual(self.state()["claimed"], [1, 2, 3])

    def test_one_caller_across_processes_wins(self):
        env = {**os.environ, "HOME": str(Path.home()), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "bin")}
        procs = [subprocess.Popen([sys.executable, "-c", "import fc_load; print(fc_load.claim(7))"], env=env, stdout=subprocess.PIPE, text=True) for _ in range(4)]
        won = [p.communicate()[0].strip() for p in procs]
        self.assertEqual((sorted(won), fc_load.claim(7)), (["False"] * 3 + ["True"], False))


class LoadMemory(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch("os.cpu_count", return_value=8), mock.patch.dict(os.environ)):
            p.start(); self.addCleanup(p.stop)  # high is 12.0, low 8.0
        os.environ.pop("COMPOSE_NO_LOAD", None)
        self.file = Path.home() / ".compose" / "load.json"

    def update(self, level, avg):
        with mock.patch.object(fc_load, "memory_level", return_value=level): return fc_load.update(avg=avg)

    def test_warn_or_critical_holds_with_why_memory_at_any_load(self):
        for level in (2, 4):
            self.file.unlink(missing_ok=True)
            s = self.update(level, 1.0); e = s["new"][0]
            self.assertEqual((s["held"], s["memory"], e["kind"], e["why"], json.loads(self.file.read_text())["memory"]), (True, level, "held", "memory", level))
        self.file.unlink()
        self.assertEqual((self.update(2, 13.0)["new"][0]["why"], self.update(4, 13.0)["new"], self.update(2, 13.0)["held"]), ("memory", [], True))

    def test_held_for_memory_releases_only_at_level_1_or_none_with_load_below_low(self):
        self.update(2, 1.0)
        for level, avg in ((2, 1.0), (4, 1.0), (1, 9.0), (None, 9.0), (1, 8.0)): self.assertEqual((self.update(level, avg)["held"], self.update(level, avg)["new"]), (True, []))
        for level in (1, None):
            s = self.update(level, 7.0)
            self.assertEqual((s["held"], s["memory"], [e["kind"] for e in s["new"]], json.loads(self.file.read_text())["held"]), (False, level, ["released"], False))
            self.update(2, 1.0)

    def test_normal_or_missing_level_keeps_the_load_rules_with_why_load(self):
        for level in (1, None):
            self.file.unlink(missing_ok=True)
            self.assertEqual((self.update(level, 9.0)["held"], self.update(level, 12.0)["held"]), (False, False))
            s = self.update(level, 13.0); e = s["new"][0]
            self.assertEqual((s["held"], s["memory"], e["kind"], e["why"]), (True, level, "held", "load"))
            self.assertEqual((self.update(level, 7.0)["held"], self.update(None, 7.0)["new"]), (False, []))

    def test_memory_level_is_an_int_or_none_without_a_subprocess_and_none_when_the_call_fails(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")), mock.patch("subprocess.Popen", side_effect=AssertionError("no subprocess")):
            level = fc_load.memory_level()
        self.assertTrue(level is None or level in (1, 2, 4))
        failing = mock.Mock(); failing.sysctlbyname.return_value = -1
        for p in (mock.patch("ctypes.util.find_library", return_value="no-such-libc"), mock.patch("ctypes.CDLL", return_value=failing), mock.patch("ctypes.CDLL", side_effect=OSError)):
            with p: self.assertIsNone(fc_load.memory_level())
