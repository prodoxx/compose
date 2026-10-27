import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, shutil, signal, subprocess, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout_bp
CHECKOUT_BP = checkout_bp.use_checkout_bp()
os.environ["COMPOSE_E2E_HEADLESS"] = "1"  # testing the tool itself never opens a browser window; real projects keep theirs
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

DETACHED = "python3 -c \"import os,subprocess; subprocess.Popen(['python3','-m','http.server',os.environ['E2E_CLIENT_PORT']],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\""


FAKE_CHROMIUM = """#!/usr/bin/env python3
# Chromium's debug port alone: /json/version answers on --remote-debugging-port, headless when launched with --headless
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port = next(int(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--remote-debugging-port="))
browser = ("HeadlessChrome" if any(a.startswith("--headless") for a in sys.argv) else "Chrome") + "/150.0.0.0"
class Version(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"Browser": browser, "User-Agent": f"Mozilla/5.0 {browser}"}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", port), Version).serve_forever()
"""


class E2eLifecycle(unittest.TestCase):
    """An e2e stage ends everything it started, so no server or browser answers the next round."""
    def stage(self, cfg):
        report, findings = {"e2e": []}, []
        ok = fc.fc_verify.e2e_stage(Path(self.dir), cfg, {}, report, findings)
        return ok, report, findings

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_setup_processes_and_detached_listeners_are_gone_afterwards(self):
        ports = Path(self.dir) / "ports"
        setup = (f"echo $E2E_PORT $E2E_CLIENT_PORT > {ports}; sleep 300 & echo $! > {self.dir}/sleep.pid; "
                 f"python3 -m http.server $E2E_PORT >/dev/null 2>&1 & {DETACHED}; sleep 300")
        cfg = {"e2e_setup": setup, "e2e_setup_wait": 2,
               "e2e": [{"name": "probe", "driver": "curl -sf -o /dev/null http://127.0.0.1:$E2E_PORT/ && curl -sf -o /dev/null http://127.0.0.1:$E2E_CLIENT_PORT/"}]}
        ok, report, _ = self.stage(cfg)
        self.assertTrue(ok, report)
        server, client = map(int, ports.read_text().split())
        time.sleep(0.5)
        self.assertFalse(fc.fc_core.port_listening(server))
        self.assertFalse(fc.fc_core.port_listening(client))  # detached from the setup's group, like `bp launch`
        self.assertFalse(self.alive(int((Path(self.dir) / "sleep.pid").read_text())))

    def test_the_cases_start_once_the_ports_their_setup_names_accept(self):
        late = "sleep 1; exec python3 -m http.server {} >/dev/null 2>&1"  # a server still starting when the configured wait is over
        probe = "curl -sf -o /dev/null http://127.0.0.1:$E2E_PORT/ && curl -sf -o /dev/null http://127.0.0.1:$E2E_CLIENT_PORT/"
        ok, report, _ = self.stage({"e2e_setup": late.format("$E2E_PORT"), "e2e_setup_wait": 0,
                                    "e2e": [{"name": "late", "driver": probe, "setup": late.format("$E2E_CLIENT_PORT"), "setup_wait": 0}]})
        self.assertTrue(ok, report)

    def test_every_stage_gets_fresh_ports(self):
        seen = []
        for _ in range(2):
            out = Path(self.dir) / "p"
            self.stage({"e2e": [{"name": "p", "driver": f"echo $E2E_PORT >> {out}"}]})
            seen = out.read_text().split()
        self.assertEqual(len(set(seen)), 2)

    def test_the_stage_launches_and_ends_its_own_browser(self):
        chromium = Path(os.environ["HOME"]) / "chromium"  # the fake stands in for Playwright's Chromium, which bp launches
        chromium.write_text(FAKE_CHROMIUM); chromium.chmod(0o755)
        p = mock.patch.dict(os.environ, {"BP_CHROME": str(chromium)}); p.start(); self.addCleanup(p.stop)
        out = Path(self.dir) / "cdp"
        ua = Path(self.dir) / "ua"
        ok, report, _ = self.stage({"e2e": [{"name": "b", "driver": f"curl -sf $BU_CDP_URL/json/version > {ua} && echo $BU_CDP_URL $BU_NAME > {out}"}]})
        self.assertTrue(ok, report)
        self.assertIn("Headless", ua.read_text())  # no window on the product owner's screen
        url, name = out.read_text().split()
        port = int(url.rsplit(":", 1)[1])
        self.assertNotIn(port, (9222, 9333))  # never the product owner's Chrome or a fixed shared port
        self.assertTrue(name)
        time.sleep(0.5)
        self.assertFalse(fc.fc_core.port_listening(port))

    def test_a_project_without_a_browser_launches_none(self):
        launched = Path(self.dir) / "launched"
        chromium = Path(os.environ["HOME"]) / "chromium"
        chromium.write_text(f"#!/bin/sh\necho started >> {launched}\nsleep 5\n"); chromium.chmod(0o755)
        p = mock.patch.dict(os.environ, {"BP_CHROME": str(chromium)}); p.start(); self.addCleanup(p.stop)
        out = Path(self.dir) / "cdp"
        ok, report, _ = self.stage({"e2e_browser": False, "e2e": [{"name": "d", "driver": f"echo ${{BU_CDP_URL:-none}} > {out}"}]})
        self.assertTrue(ok, report)
        self.assertFalse(launched.exists())
        self.assertEqual(out.read_text().strip(), "none")

    def test_a_configured_browser_is_used_instead(self):
        out = Path(self.dir) / "cdp"
        self.stage({"e2e_env": {"BU_CDP_URL": "http://127.0.0.1:1", "BU_NAME": "ext"},
                    "e2e": [{"name": "b", "driver": f"echo $BU_CDP_URL $BU_NAME > {out}"}]})
        self.assertEqual(out.read_text().split(), ["http://127.0.0.1:1", "ext"])

    def test_a_driver_case_runs_its_assert_after_the_driver(self):
        out = Path(self.dir) / "assert"
        ok, report, findings = self.stage({"e2e": [{"name": "a", "driver": "true", "assert": f"echo $E2E_PORT $PWD > {out}; echo 'no row' >&2; false"}]})
        self.assertFalse(ok)
        self.assertIn("no row", findings[0])
        port, cwd = out.read_text().split()
        self.assertTrue(port.isdigit())
        self.assertEqual(Path(cwd).resolve(), Path(self.dir).resolve())
        ok, _, _ = self.stage({"e2e": [{"name": "a", "driver": "true", "assert": "true"}]})
        self.assertTrue(ok)

    def test_a_case_line_shows_the_run_only_when_the_driver_reports_one(self):
        result = 'echo \'E2E_RESULT {"steps": 3, "elapsed_ms": 40, "status": "done"}\''
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.stage({"e2e_env": {"BU_CDP_URL": "http://127.0.0.1:1", "BU_NAME": "ext"},
                        "e2e": [{"name": "bare", "driver": "true"}, {"name": "run", "driver": result}]})
        lines = [l for l in out.getvalue().splitlines() if "e2e    ok" in l]
        self.assertTrue(lines[0].endswith("  bare"), lines)
        self.assertTrue(lines[1].endswith("  run  steps=3 run=40ms status=done"), lines)

    def test_a_failing_driver_skips_its_assert(self):
        out = Path(self.dir) / "assert"
        ok, _, _ = self.stage({"e2e": [{"name": "a", "driver": "exit 1", "assert": f"touch {out}"}]})
        self.assertFalse(ok)
        self.assertFalse(out.exists())


FAKE_AGENT = '''"""A stand-in for jev's Agent, steered by $FAKE_JEV, under the real `python -m jev_ultrafast`."""
import json, os, pathlib

class Agent:
    def __init__(self, url, goals, record_dir=None, secrets=None, **_):
        self.spec, self.url, self.record, self.history = json.loads(os.environ.get("FAKE_JEV") or "{}"), url, record_dir, []
        self.status, self.reason = "ready", None
        if self.spec.get("seen"):
            pathlib.Path(self.spec["seen"]).write_text(json.dumps({"secrets": secrets, "bu": os.environ.get("BU_CDP_URL")}))
        if self.spec.get("tab"):  # the case's tab, open until the agent closes
            pathlib.Path(self.spec["tab"]).write_text(url)
        if record_dir:
            pathlib.Path(record_dir).mkdir(parents=True, exist_ok=True)
            (pathlib.Path(record_dir) / "000000.jpg").write_bytes(b"jpg")

    def snapshot(self):
        return {"status": self.status, "stop_reason": self.reason, "elapsed_ms": 40 * len(self.history), "history": list(self.history),
                "page": {"url": self.spec.get("url", self.url)}, "decisions": [], "text_calls": []}

    def run(self):
        for i, status in enumerate(self.spec.get("statuses", ["done"])):
            self.history.append({"step": i + 1, "kind": "click", "action": f"Next {i + 1}", "page_changed": status != "blocked"})
            self.status, self.reason = status, self.spec.get("reason") if status == "blocked" else None
            yield self.snapshot()
        if self.spec.get("raise"):
            raise ValueError(self.spec["raise"])

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.spec.get("tab"):
            pathlib.Path(self.spec["tab"]).unlink()
'''

# The jev case's `uv run --quiet [--env-file F] python ...`, run with this interpreter: no real uv, no python of uv's choosing.
FAKE_UV = f'''#!{sys.executable}
import os, sys
args = sys.argv[1:]
assert args[:2] == ["run", "--quiet"], args
args = args[2:]
if args[0] == "--env-file":  # as uv does, the process env wins
    for line in open(args[1]).read().splitlines():
        k, eq, v = line.partition("=")
        if eq and not k.lstrip().startswith("#"):
            os.environ.setdefault(k.strip(), v.strip().strip("\\"'"))
    args = args[2:]
assert args[0] == "python", args
os.execv(sys.executable, [sys.executable, *args[1:]])
'''


def fake_uv(test):
    """Put FAKE_UV first on PATH for the test."""
    d = Path(tempfile.mkdtemp()); (d / "uv").write_text(FAKE_UV); (d / "uv").chmod(0o755)
    p = mock.patch.dict(os.environ, {"PATH": f"{d}{os.pathsep}{os.environ['PATH']}"}); p.start(); test.addCleanup(p.stop)


class JevCases(unittest.TestCase):
    """A jev (non-driver) case through the real runner, `python -m jev_ultrafast`, with a stand-in Agent: no browser, no model."""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        jev = Path(tempfile.mkdtemp())
        (jev / "jev_ultrafast").mkdir()
        (jev / "jev_ultrafast" / "__init__.py").write_text(FAKE_AGENT)
        shutil.copy(fc.fc_config.JEV_DIR / "jev_ultrafast" / "__main__.py", jev / "jev_ultrafast" / "__main__.py")
        p = mock.patch.object(fc.fc_config, "JEV_DIR", jev); p.start(); self.addCleanup(p.stop)
        k = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "ts-test"}); k.start(); self.addCleanup(k.stop)  # what apply_config sets from auth.json
        fake_uv(self)

    def test_a_case_without_a_typesafe_key_says_why_instead_of_running_jev(self):
        """Under `compose timed` the scratch config holds no keys: the case fails as a harness error naming that, never jev's bare KeyError."""
        seen = self.dir / "seen"
        del os.environ["TYPESAFE_API_KEY"]
        with mock.patch.object(fc.fc_config, "CONFIG_PATH", Path(fc.fc_core.scratch_config())):
            ok, r, findings, out = self.stage({"env": {"FAKE_JEV": json.dumps({"seen": str(seen)})}})
        self.assertFalse(ok)
        self.assertFalse(seen.exists())  # jev never started
        self.assertTrue(r["tool_error"]); self.assertEqual(findings, [])
        self.assertIn("jev case flow needs a TypeSafe key, but runs on a scratch config", r["stderr"])
        self.assertIn("run compose's own commands directly", r["stderr"])
        self.assertNotIn("KeyError", r["stdout"] + r["stderr"])

    def stage(self, case, results=None, **cfg):
        report, findings, out = {"e2e": []}, [], io.StringIO()
        case = {"name": "flow", "url": "http://127.0.0.1:$E2E_PORT/", "goal": "Log in", "assert": "true", **case}
        cfg = {"e2e_env": {"BU_CDP_URL": "http://127.0.0.1:1"}, **cfg, "e2e": [case]}
        with contextlib.redirect_stdout(out):
            ok = fc.fc_verify.e2e_stage(self.dir, cfg, {}, report, findings, results)
        return ok, report["e2e"][-1], findings, out.getvalue()

    def test_a_jev_case_runs_its_own_setup_around_jev_and_its_assert(self):
        setup = "echo $E2E_CLIENT_PORT > port; exec python3 -m http.server $E2E_CLIENT_PORT >/dev/null 2>&1"
        ok, r, _, _ = self.stage({"setup": setup, "setup_wait": 0, "assert": "curl -sf -o /dev/null http://127.0.0.1:$E2E_CLIENT_PORT/"})
        self.assertTrue(ok, r)
        self.assertEqual(r["assert_exit"], 0)
        time.sleep(0.3)
        self.assertFalse(fc.fc_core.port_listening(int((self.dir / "port").read_text())))

    def test_a_failed_assert_with_the_marker_is_flagged_even_when_its_output_is_cut(self):
        said = lambda script: self.stage({"assert": script})[1].get("assert_failed")
        self.assertTrue(said("echo \"ASSERT FAILED (flow):\"; head -c 4000 /dev/zero | tr \"\\0\" x; echo; exit 1"))
        self.assertFalse(said("echo no button; exit 1"))  # an assert that merely says no
        self.assertFalse(said("echo \"ASSERT FAILED (flow)\"; exit 0"))
        self.assertFalse(said("echo \"the ASSERT FAILED marker\"; exit 1"))  # at the start of a line only

    def test_ports_expand_in_a_case_env_and_in_e2e_env(self):
        seen = self.dir / "seen"
        ok, r, _, _ = self.stage({"env": {"FAKE_JEV": json.dumps({"seen": str(seen)}), "OWN": "${E2E_PORT}"},
                                  "assert": f"echo $APP $OWN > {self.dir}/env"},
                                 e2e_env={"BU_CDP_URL": "http://127.0.0.1:$E2E_CLIENT_PORT", "APP": "http://127.0.0.1:$E2E_PORT/"})
        self.assertTrue(ok, r)
        app, own = (self.dir / "env").read_text().split()
        self.assertEqual(app, f"http://127.0.0.1:{own}/")
        self.assertRegex(json.loads(seen.read_text())["bu"], r"^http://127\.0\.0\.1:\d+$")

    def test_a_jev_run_that_raises_ends_in_error_with_its_reason_and_the_assert_still_runs(self):
        # At jev's 60-action budget it raised: no result was printed and the assert, which reset the user, never ran.
        spec = {"statuses": ["ready", "ready"], "raise": "Stopped at the 60-action demo budget"}
        ok, r, findings, out = self.stage({"env": {"FAKE_JEV": json.dumps(spec)}, "assert": f"touch {self.dir}/asserted"})
        self.assertFalse(ok)
        self.assertTrue((self.dir / "asserted").exists())
        self.assertEqual((r["run"]["status"], r["run"]["steps"], r["assert_exit"]), ("error", 2, 0))
        self.assertEqual(r["run"]["stop_reason"], "ValueError: Stopped at the 60-action demo budget")
        self.assertTrue(r["tool_error"]); self.assertEqual(findings, [])
        self.assertIn("status=error assert=0  stop: ValueError: Stopped at the 60-action demo budget", out)

    def test_the_assert_reads_the_case_tab_before_jev_closes_it(self):
        # jev closed its tab on leaving `with agent:`, before verify ran the assert: every assert that read the page found no tab.
        tab = self.dir / "tab"
        for spec in ({}, {"statuses": ["ready"], "raise": "Model provider returned HTTP 500"}):
            ok, r, _, _ = self.stage({"env": {"FAKE_JEV": json.dumps({**spec, "tab": str(tab)})}, "assert": f"test -f {tab}"})
            self.assertEqual(r["assert_exit"], 0, r)
            self.assertEqual(ok, not spec)
            self.assertFalse(tab.exists())  # and closed once the assert is done

    def test_a_blocked_case_says_why_and_where_and_keeps_its_steps_and_screenshots(self):
        url = "https://app.test/billing?" + "&".join(f"p{i}=v{i}" for i in range(40))
        spec = {"statuses": ["ready"] * 4 + ["blocked"] * 3, "reason": "The last 3 actions did not change the page", "url": url}
        results = self.dir / "results"
        ok, r, _, out = self.stage({"env": {"FAKE_JEV": json.dumps(spec)}}, results=results)
        self.assertFalse(ok)
        self.assertIn("status=blocked assert=0  stop: The last 3 actions did not change the page", out)
        self.assertIn(f"           at {url}\n", out)  # whole, where verify.log used to cut JEV_RESULT at 160 characters
        self.assertIn("           step  7  click  Next 7  (page unchanged)\n", out)
        self.assertNotIn("JEV_RESULT", out + r["stdout"])
        self.assertEqual([h["label"] for h in r["run"]["history"]], [f"Next {i}" for i in range(1, 8)])
        self.assertEqual(r["screenshots"], str(results / "e2e" / "01-flow"))
        self.assertTrue((results / "e2e" / "01-flow" / "000000.jpg").exists())
        ok, r, _, _ = self.stage({}, results=results)  # a pass keeps none, and the last run's are the only ones kept
        self.assertTrue(ok, r)
        self.assertNotIn("screenshots", r); self.assertFalse((results / "e2e" / "01-flow").exists())

    def test_secrets_reach_jev_from_the_env_and_nowhere_else(self):
        seen, password = self.dir / "seen", "pw-Sekr3t-0042"
        held = mock.patch.object(fc.fc_jev, "run_held", wraps=fc.fc_jev.run_held)
        with held as t:
            ok, r, _, out = self.stage({"secrets": {"Password": "$E2E_PASSWORD"}, "env": {"FAKE_JEV": json.dumps({"seen": str(seen)})},
                                        "assert": "echo logged in with $E2E_PASSWORD"},
                                       e2e_env={"BU_CDP_URL": "http://127.0.0.1:1", "E2E_PASSWORD": password})
        self.assertTrue(ok, r)
        self.assertEqual(json.loads(seen.read_text())["secrets"], {"Password": password})
        self.assertNotIn(password, t.call_args[0][0])  # never on a command line
        self.assertNotIn(password, json.dumps(r) + out)
        self.assertIn("logged in with <redacted>", r["stdout"])


class UnattendedBrowser(unittest.TestCase):
    """The stage's browser outlives neither a sleeping display nor a killed verify."""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        chromium = Path(os.environ["HOME"]) / "chromium"
        chromium.write_text(FAKE_CHROMIUM); chromium.chmod(0o755)
        bin_ = self.dir / "bin"
        bin_.mkdir()
        (bin_ / "caffeinate").write_text(f"#!/bin/sh\necho $$ \"$@\" > {self.dir}/caffeinate\nexec sleep 30\n")
        (bin_ / "caffeinate").chmod(0o755)
        env = {"BP_CHROME": str(chromium), "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "COMPOSE_NO_AWAKE": ""}  # this case asks for the fake caffeinate
        p = mock.patch.dict(os.environ, env); p.start(); self.addCleanup(p.stop)
        os.environ.pop("COMPOSE_E2E_HEADLESS")  # restored with the rest of the environment after the test
        held = mock.patch.dict(fc.fc_proc.AWAKE, clear=True); held.start(); self.addCleanup(held.stop)  # no hold of an earlier test hides the fake
        self.addCleanup(lambda: [(h.terminate(), h.wait()) for h in list(fc.fc_proc.AWAKE.values())])

    def browser(self, wait="", **cfg):
        ua = self.dir / "ua"
        report = {"e2e": []}
        ok = fc.fc_verify.e2e_stage(self.dir, {**cfg, "e2e": [{"name": "b", "driver": f"{wait}curl -sf $BU_CDP_URL/json/version > {ua}"}]},
                                    {}, report, [])
        self.assertTrue(ok, report)
        return json.loads(ua.read_text())["Browser"]

    def test_the_browser_is_headless_unless_the_project_asks_to_watch_it(self):
        # A headed Chromium stops painting once the display sleeps, and an unattended verify failed every case.
        self.assertTrue(self.browser().startswith("HeadlessChrome"))
        self.assertFalse((self.dir / "caffeinate").exists())
        awake = f"for i in $(seq 100); do [ -s {self.dir}/caffeinate ] && break; sleep 0.05; done; "  # it runs while the case does
        self.assertTrue(self.browser(awake, e2e_headed=True).startswith("Chrome/"))
        pid, *args = (self.dir / "caffeinate").read_text().split()
        self.assertEqual(args, ["-dims", "-w", str(os.getpid())])  # the Mac stays awake while it is watched, for as long as this process lives
        self.assertTrue(fc.fc_jev.alive(int(pid)))  # the stage does not end it: cmd_verify holds it for the whole verify

    def test_a_sigterm_ends_the_stages_browser_setup_and_driver(self):
        repo = self.dir / "repo"
        for args in (["init", "-q", "-b", "main", str(repo)], ["-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"]):
            subprocess.run(["git", *args], check=True)
        proj = repo / ".compose" / "p"
        proj.mkdir(parents=True)
        setup = f"sleep 30 & echo $! > {self.dir}/setup.pid; wait"
        driver = f"echo $BU_CDP_URL > {self.dir}/cdp; sleep 30 & echo $! > {self.dir}/driver.pid; wait"
        (proj / "verify.json").write_text(json.dumps({"review": False, "jev_acceptance": False, "e2e_setup": setup, "e2e_setup_wait": 0,
                                                      "e2e": [{"name": "slow", "driver": driver}]}))
        compose = Path(__file__).resolve().parents[1] / "bin" / "compose"
        p = subprocess.Popen([str(compose), "verify", "--repo", str(repo), "--project", "p", "--skip-review", "--fix", "0"],
                             env={**os.environ, "COMPOSE_DETACHED": "1"}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 60
        while not (self.dir / "driver.pid").exists() and time.time() < deadline and p.poll() is None:
            time.sleep(0.1)
        port = int((self.dir / "cdp").read_text().rsplit(":", 1)[1])
        profiles = lambda: list(Path(tempfile.gettempdir()).glob(f"fc-e2e-browser-{p.pid}-*"))  # named after its verify
        self.assertTrue(fc.fc_core.port_listening(port)); self.assertEqual(len(profiles()), 1)
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM, out)
        time.sleep(0.5)
        self.assertFalse(fc.fc_core.port_listening(port), out)
        for name in ("setup.pid", "driver.pid"):
            self.assertFalse(fc.fc_jev.alive(int((self.dir / name).read_text())), name)
        self.assertEqual(profiles(), [])

    def test_the_next_stage_ends_the_browser_of_a_verify_that_is_gone(self):
        gone = subprocess.Popen(["true"]); gone.wait()  # a verify killed outright
        browsers = {}
        for owner in (gone.pid, os.getpid()):
            profile, port = Path(tempfile.mkdtemp(prefix=f"fc-e2e-browser-{owner}-")), fc.fc_core.free_port()
            b = subprocess.Popen([os.environ["BP_CHROME"], f"--user-data-dir={profile}", f"--remote-debugging-port={port}"], start_new_session=True)
            self.addCleanup(lambda b=b: (b.kill(), b.wait()))
            browsers[owner] = (profile, port)
        deadline = time.time() + 10
        while not all(fc.fc_core.port_listening(port) for _, port in browsers.values()) and time.time() < deadline:
            time.sleep(0.1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fc.fc_verify.e2e_stage(self.dir, {"e2e_browser": False, "e2e": [{"name": "d", "driver": "true"}]}, {}, {"e2e": []}, [])
        self.assertIn(f"ended the browser of a verify that is gone: {browsers[gone.pid][0]}", out.getvalue())
        time.sleep(0.3)
        self.assertFalse(fc.fc_core.port_listening(browsers[gone.pid][1]))
        self.assertFalse(browsers[gone.pid][0].exists())
        self.assertTrue(fc.fc_core.port_listening(browsers[os.getpid()][1]))  # its verify still runs: this one is
        self.assertTrue(browsers[os.getpid()][0].exists())


class ToolErrors(unittest.TestCase):
    def test_a_harness_crash_is_retried_once_and_never_a_finding(self):
        d = tempfile.mkdtemp()
        crash = f"echo x >> {d}/n; echo 'File \"/x/jev_ultrafast/model.py\", line 193' >&2; exit 1"
        report, findings = {"e2e": []}, []
        ok = fc.fc_verify.e2e_stage(Path(d), {"e2e": [{"name": "c", "driver": crash}]}, {}, report, findings)
        self.assertFalse(ok)
        self.assertEqual(findings, [])
        self.assertTrue(report["e2e"][0]["tool_error"])
        self.assertEqual(len((Path(d) / "n").read_text().split()), 2)

    def test_an_app_failure_is_a_finding(self):
        report, findings = {"e2e": []}, []
        ok = fc.fc_verify.e2e_stage(Path(tempfile.mkdtemp()), {"e2e": [{"name": "a", "driver": "echo 'AssertionError: no line' >&2; exit 1"}]}, {}, report, findings)
        self.assertFalse(ok)
        self.assertEqual(len(findings), 1)
        self.assertFalse(report["e2e"][0]["tool_error"])

    def check(self, cmd):
        with mock.patch.object(fc.fc_herdr, "herdr_verify_panes", return_value={}):
            return fc.fc_verify.run_stages(Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()), {"checks": [cmd]}, SimpleNamespace(skip_e2e=True, skip_review=True))

    def test_a_failing_check_whose_early_output_names_a_harness_error_is_one_run_and_a_finding(self):
        d = tempfile.mkdtemp()
        noisy = f"echo x >> {d}/n; echo 'Chromium not found at /x'; python3 -c \"print('.' * 4000)\"; echo 'FAILED (failures=1)'; exit 1"
        ok, report, findings = self.check(noisy)
        self.assertFalse(ok)
        self.assertEqual(len((Path(d) / "n").read_text().split()), 1)
        self.assertFalse(report["checks"][0]["tool_error"])
        self.assertIn("FAILED (failures=1)", findings)

    def test_a_failing_check_keeps_its_full_output_in_a_file_and_lists_every_failing_test_for_the_fixer(self):
        script = ("import sys\n[print('FAIL: test_%d (mod.Case.test_%d)' % (i, i)) if i % 2 else print('ERROR: test_%d (mod.Case.test_%d)' % (i, i)) for i in range(29)]\n"
                  "print('x' * 9000)\nprint('FAILED (failures=7, errors=22)')\nsys.exit(1)")
        ok, report, findings = self.check(f"python3 -c \"{script}\"")
        check = report["checks"][0]
        self.assertEqual(len(check["output"]), 3000)
        self.assertGreater(len(Path(check["output_file"]).read_text()), 9000)
        self.assertEqual(sum(f"test_{i} (mod.Case.test_{i})" in findings for i in range(29)), 29)
        self.assertIn("FAILED (failures=7, errors=22)", findings)
        self.assertIn(check["output_file"], findings)
        self.assertLess(len(findings), 12000)

    def test_two_checks_with_one_command_prefix_started_in_one_second_keep_separate_output_files(self):
        results, started = tempfile.mkdtemp(), time.time()
        prefix = "cd app && . /Users/someone/workspace/apps/project/smoke.env && "
        first = fc.fc_verify.save_output(results, prefix + "bun test", started, "TEST OUTPUT")
        second = fc.fc_verify.save_output(results, prefix + "bun lint", started, "LINT OUTPUT")
        self.assertNotEqual(first, second)
        self.assertEqual(Path(first).read_text(), "TEST OUTPUT")
        self.assertEqual(Path(second).read_text(), "LINT OUTPUT")

    def test_a_check_past_its_time_limit_fails_as_a_timeout_naming_the_limit_and_verify_goes_on(self):
        with mock.patch.object(fc.fc_verify, "CHECK_TIMEOUT", 1):
            ok, report, findings = self.check("echo started; sleep 30")
        check = report["checks"][0]
        self.assertFalse(ok)
        self.assertEqual(check["exit"], 124)
        self.assertFalse(check["tool_error"])
        self.assertIn("started", check["output"])
        self.assertIn("TIMEOUT: did not finish within 1s", check["output"])
        self.assertIn("did not finish within 1s", findings)

    def test_a_driver_past_its_case_limit_fails_the_case_as_a_timeout(self):
        report, findings = {"e2e": []}, []
        ok = fc.fc_verify.e2e_stage(Path(tempfile.mkdtemp()), {"e2e": [{"name": "a", "driver": "sleep 30", "timeout": 1}]}, {}, report, findings)
        self.assertFalse(ok)
        self.assertEqual(report["e2e"][0]["exit"], 124)
        self.assertIn("TIMEOUT: did not finish within 1s", report["e2e"][0]["stderr"])
        self.assertEqual(len(findings), 1)

    def test_a_check_that_ends_on_a_harness_error_is_retried_once_and_never_a_finding(self):
        d = tempfile.mkdtemp()
        ok, report, findings = self.check(f"echo x >> {d}/n; echo 'bash: nosuchtool: command not found' >&2; exit 127")
        self.assertFalse(ok)
        self.assertEqual(len((Path(d) / "n").read_text().split()), 2)
        self.assertTrue(report["checks"][0]["tool_error"])
        self.assertEqual(findings, "")

    def test_a_failing_check_the_machine_slept_through_is_run_again_and_its_first_run_is_no_finding(self):
        d = tempfile.mkdtemp()
        t = {"wall": 1000.0, "awake": 50.0}
        clock = SimpleNamespace(time=lambda: t["wall"], monotonic=lambda: t["awake"])
        real = fc.fc_proc.run
        def run(*a, **k):
            p = real(*a, **k)
            if not t.get("napped"):  # the lid closed while the first run waited on its network
                t.update(napped=1, wall=t["wall"] + 5400, awake=t["awake"] + 60)
            return p
        said = []
        cmd = f"echo x >> {d}/n; test $(wc -l < {d}/n) -gt 1"
        with mock.patch.object(fc.fc_proc, "time", clock), mock.patch.object(fc.fc_verify, "time", clock), \
                mock.patch.object(fc.fc_proc, "run", run), mock.patch.object(fc.fc_core, "say", said.append):
            ok, report, findings = self.check(cmd)
        self.assertTrue(ok)
        self.assertEqual((len((Path(d) / "n").read_text().split()), findings), (2, ""))
        self.assertTrue(any("slept for 89 minutes" in s and "run again" in s for s in said), said)
        self.assertEqual(report["checks"][0]["seconds"], 60.0)  # the minute the check ran, not the hour and a half the lid was closed


class FixRoundChecks(unittest.TestCase):
    def stages(self, scoped, full, tests):
        """run_stages with its checks and a fix round's tests each appending their name to one log, in run order."""
        d = Path(tempfile.mkdtemp())
        cmd = lambda name, code: f"echo {name} >> {d}/log; exit {code}"
        with mock.patch.object(fc.fc_herdr, "herdr_verify_panes", return_value={}):
            ok, report, findings = fc.fc_verify.run_stages(d, d, {"checks": [cmd("full", full)]}, SimpleNamespace(skip_e2e=True, skip_review=True),
                                                           None if tests is None else [cmd("scoped", scoped)])
        return ok, report, findings, (d / "log").read_text().split()

    def test_a_fix_round_runs_its_tests_then_every_check_once(self):
        ok, report, findings, log = self.stages(0, 0, tests=True)
        self.assertEqual((ok, log, findings), (True, ["scoped", "full"], ""))
        self.assertEqual([len(report["scoped"]), len(report["checks"])], [1, 1])  # the PR body reads only the full checks

    def test_a_failing_fix_test_skips_the_full_checks(self):
        ok, report, findings, log = self.stages(1, 0, tests=True)
        self.assertEqual((ok, log, report["checks"]), (False, ["scoped"], []))
        self.assertIn("echo scoped", findings)

    def test_a_regression_the_fix_tests_miss_fails_the_round(self):
        ok, _, findings, log = self.stages(0, 1, tests=True)
        self.assertEqual((ok, log), (False, ["scoped", "full"]))
        self.assertIn("echo full", findings)

    def test_the_first_round_runs_only_the_full_checks(self):
        ok, report, _, log = self.stages(0, 0, tests=None)
        self.assertEqual((ok, log, report["scoped"]), (True, ["full"], []))


class CheckoutBp(unittest.TestCase):
    """The suite runs the checkout's bp, whatever bp sits on PATH."""
    target = os.path.realpath(Path(__file__).resolve().parents[1] / "bp" / "bin" / "bp.mjs")

    def stale(self, dangling):
        d = Path(tempfile.mkdtemp())
        if dangling:
            (d / "bp").symlink_to(d / "gone.mjs")
        else:
            (d / "bp").write_text("#!/bin/sh\necho other\n")
            (d / "bp").chmod(0o755)
        return str(d)

    def test_the_import_put_the_checkout_bp_first(self):
        self.assertEqual([p for p in os.environ["PATH"].split(os.pathsep) if p != str(hermetic.guard_dir())][0], CHECKOUT_BP)
        self.assertEqual(os.path.realpath(shutil.which("bp")), self.target)

    def test_it_wins_over_a_dangling_or_different_bp_ahead_and_keeps_the_rest_in_order(self):
        for dangling in (True, False):
            rest = [self.stale(dangling), "/usr/bin", "/bin"]
            with mock.patch.dict(os.environ, {"PATH": os.pathsep.join(rest)}):
                d = checkout_bp.use_checkout_bp()
                self.assertEqual(d, CHECKOUT_BP)
                self.assertEqual(os.environ["PATH"].split(os.pathsep), [d] + rest)
                self.assertEqual(os.path.realpath(shutil.which("bp")), self.target)


class BeforePlanning(unittest.TestCase):
    """Groundwork writes checks and e2e cases before any task exists; verify runs them without a sync when nothing reads the tasks."""
    TASK = {"id": "001", "title": "a", "files": ["x.py"], "protected": ["t.py"], "test": "true", "body": "## Goal\nx"}

    def verify(self, tasks=None, checks=None, last=None, **flags):
        repo = Path(tempfile.mkdtemp())
        for args in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *args], cwd=repo, check=True)
        self.proj = proj = repo / ".compose" / "p"
        (proj / "results").mkdir(parents=True)
        (proj / "verify.json").write_text(json.dumps({"checks": checks or [f"touch {repo}/checked"], "review": False, "jev_acceptance": False,
                                                      "e2e": [{"name": "probe", "driver": f"touch {repo}/driven"}]}))
        if tasks is not None:
            (proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        if last is not None:
            (proj / "results" / "verify.json").write_text(last)
        a = SimpleNamespace(**{"repo": str(repo), "project": "p", "skip_e2e": False, "skip_review": False, "fix": 2, **flags})
        self.out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_linear, "linear_reconcile") as self.lr, \
                mock.patch.object(fc.fc_verify, "verify_failed") as self.vf, contextlib.redirect_stdout(self.out), self.assertRaises(SystemExit) as cm:
            fc.fc_verify.cmd_verify(a)
        return cm.exception.code, repo

    def smoke(self, label, **kw):
        code, _ = self.verify(**kw)
        final = [l for l in self.out.getvalue().splitlines() if f"smoke {label}" in l or "verify PASS" in l or "verify FAIL" in l]
        self.assertEqual(len(final), 1, self.out.getvalue()); self.assertIn(f"smoke {label}", final[0])
        self.assertEqual((self.lr.call_count, self.vf.call_count), (0, 0))
        return code, json.loads((self.proj / "results" / "smoke.json").read_text())

    def test_an_unsynced_or_empty_sync_run_is_a_smoke_run_and_writes_no_verify_record(self):
        for kw, left_out in (({"skip_review": True, "fix": 0}, ["tasks", "review"]), ({"tasks": []}, ["tasks"]),
                             ({"tasks": [], "skip_e2e": True, "fix": 0}, ["tasks", "e2e"])):
            code, smoke = self.smoke(f"PASS ({', '.join('no ' + x for x in left_out)})", **kw)
            self.assertEqual((code, smoke["left_out"], smoke["passed"], len(smoke["rounds"])), (0, left_out, True, 1), kw)
            self.assertFalse((self.proj / "results" / "verify.json").exists(), kw)

    def test_a_skipped_stage_is_a_smoke_run_that_leaves_the_last_verify_record_alone(self):
        for kw, left_out in (({"skip_review": True}, ["review"]), ({"skip_e2e": True, "skip_review": True}, ["review", "e2e"]),
                             ({"skip_e2e": True, "checks": ["false"], "fix": 0}, ["e2e"])):
            label = ("FAIL" if "checks" in kw else "PASS") + f" ({', '.join('no ' + x for x in left_out)})"
            code, smoke = self.smoke(label, tasks=[self.TASK], last='{"passed": true}', **kw)
            self.assertEqual((code, smoke["left_out"], {"rejected_fixes", "seconds", "cost"} <= smoke.keys()), (1 if "checks" in kw else 0, left_out, True), kw)
            record = self.proj / "results" / "verify.json"
            self.assertEqual(record.read_text(), '{"passed": true}', kw)

    def test_a_synced_run_with_no_skip_flag_is_a_full_verify_even_with_review_and_e2e_off_in_its_config(self):
        code, _ = self.verify(tasks=[self.TASK])
        record = json.loads((self.proj / "results" / "verify.json").read_text())
        self.assertEqual((code, record["passed"], "left_out" in record), (0, True, False))
        self.assertFalse((self.proj / "results" / "smoke.json").exists())
        self.assertIn("verify PASS", self.out.getvalue()); self.assertNotIn("smoke", self.out.getvalue())
        self.assertEqual(self.lr.call_args_list, [mock.call(self.proj, verified=True)])

    def test_a_verify_that_reviews_or_fixes_still_needs_the_sync_and_names_the_way_without(self):
        for flags in ({}, {"skip_review": True}, {"fix": 0}):
            code, repo = self.verify(**flags)
            self.assertIn("no tasks synced for p", str(code), flags)
            self.assertIn("--skip-review --fix 0", str(code), flags)
            self.assertFalse((repo / "checked").exists(), flags)

    def test_a_smoke_fail_skips_the_failure_screen_and_keeps_its_record_line_and_exit(self):
        for kw, label in (({"skip_review": True}, "no tasks, no review"), ({"tasks": []}, "no tasks"),
                          ({"tasks": [self.TASK], "skip_e2e": True}, "no e2e"), ({"tasks": [self.TASK], "skip_review": True, "last": '{"passed": true}'}, "no review")):
            code, smoke = self.smoke(f"FAIL ({label})", checks=["false"], fix=0, **kw)
            self.assertEqual((code, smoke["passed"], self.vf.call_count), (1, False, 0), kw)

    def test_a_full_verify_fail_still_screens_its_rounds_and_findings(self):
        code, _ = self.verify(tasks=[self.TASK], checks=["false"], fix=0, last='{"passed": true}')
        record = json.loads((self.proj / "results" / "verify.json").read_text())
        self.assertEqual((code, record["passed"], self.vf.call_count), (1, False, 1))
        proj, rounds, findings = self.vf.call_args.args
        self.assertEqual((proj, len(rounds), rounds[0]["checks"]), (self.proj, 1, record["rounds"][0]["checks"]))
        self.assertIn("Command failed: false", findings)
        self.assertIn("verify FAIL", self.out.getvalue())


class PathsAtStackTop(unittest.TestCase):
    """verify at the stack top runs in a treehouse slot under the main checkout: absolute paths in verify.json move there once."""
    def test_every_path_under_the_worktree_or_the_main_checkout_moves_to_the_slot_once(self):
        base = Path(tempfile.mkdtemp()).resolve()
        main, repo = base / "app", base / "app-worktrees" / "feature-x"  # the worktree's path starts with the main checkout's
        main.mkdir()
        for args in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"],
                     ["worktree", "add", "-q", "-b", "compose/p", str(repo)]):
            subprocess.run(["git", *args], cwd=main, check=True)
        root = main / ".treehouse" / "app-123" / "1" / "app"
        proj = repo / ".compose" / "p"
        proj.mkdir(parents=True)
        e2e = f"{repo}/.compose/p/e2e"
        (proj / "e2e").mkdir()
        for name in ("e2e/refresh-verify-env.sh", "e2e/serve.sh", "e2e/login.sh", "e2e/check.py", "profile"):
            (proj / name).touch()
        root.mkdir(parents=True)
        (root / ".compose").symlink_to(repo / ".compose")  # what stack_worktree links: the scripts exist there too
        cfg = {"checks": [f"{e2e}/refresh-verify-env.sh", f"bash {main}/scripts/lint.sh", "npm test", f"{base}/app2/tool --x"],
               "e2e_setup": f"cd {main} && {e2e}/serve.sh $E2E_PORT &", "e2e_env": {"PROFILE": f"{repo}/.compose/p/profile"},
               "e2e": [{"name": "login", "url": f"file://{repo}/index.html", "goal": "Log in", "setup": f"{e2e}/login.sh",
                        "env": {"DATA": f"{main}/fixtures"}, "assert": f"python3 {e2e}/check.py --root={repo}"}]}
        (proj / "verify.json").write_text(json.dumps(cfg))
        seen = {}
        a = SimpleNamespace(repo=str(repo), project="p", skip_e2e=False, skip_review=True, fix=0)
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), \
                mock.patch.object(fc.fc_verify, "verify_root", return_value=(root, "main", None)), \
                mock.patch.object(fc.fc_verify, "verify_rounds", lambda r, p, c, *_: seen.update(c)):
            fc.fc_verify.cmd_verify(a)
        top = f"{root}/.compose/p/e2e"
        self.assertEqual(seen["checks"], [f"{top}/refresh-verify-env.sh", f"bash {root}/scripts/lint.sh", "npm test", f"{base}/app2/tool --x"])
        self.assertEqual(seen["e2e_setup"], f"cd {root} && {top}/serve.sh $E2E_PORT &")
        self.assertEqual(seen["e2e_env"], {"PROFILE": f"{root}/.compose/p/profile"})
        self.assertEqual(seen["e2e"], [{"name": "login", "url": f"file://{root}/index.html", "goal": "Log in", "setup": f"{top}/login.sh",
                                        "env": {"DATA": f"{root}/fixtures"}, "assert": f"python3 {top}/check.py --root={root}"}])

    def test_a_path_already_at_a_root_under_the_main_checkout_is_not_moved_again(self):
        main = Path(tempfile.mkdtemp()).resolve() / "app"
        repo, root = main / ".treehouse" / "app-1" / "2" / "app", main / ".treehouse" / "app-1" / "4" / "app"
        once = fc.fc_verify.at_root([f"bash {main}/scripts/lint.sh", f"cd {repo}/app && bun run build"], (repo, main), root)
        self.assertEqual(once, [f"bash {root}/scripts/lint.sh", f"cd {root}/app && bun run build"])
        self.assertEqual(fc.fc_verify.at_root(once, (repo, main), root), once)  # the slot sits under the main checkout: no double move

    def test_a_project_file_the_stack_top_does_not_have_stays_in_the_project_worktree(self):
        base = Path(tempfile.mkdtemp()).resolve()
        repo, root = base / "app-feature", base / "slot" / "app"
        (repo / ".compose" / "p" / "e2e").mkdir(parents=True)
        (repo / ".compose" / "p" / "e2e" / "case.py").touch()
        (root / ".compose" / "other").mkdir(parents=True)  # a real .compose of someone else's: the link was never made
        olds = (repo,)
        move = lambda v: fc.fc_verify.at_root(v, olds, root)
        self.assertEqual(move(f"python3 {repo}/.compose/p/e2e/case.py --root={repo}"), f"python3 {repo}/.compose/p/e2e/case.py --root={root}")
        self.assertEqual(move({"assert": f"{repo}/.compose/p"}), {"assert": f"{repo}/.compose/p"})
        self.assertEqual(move(f"{repo}/.composer/x"), f"{root}/.composer/x")
        (root / ".compose" / "p").mkdir()
        (root / ".compose" / "p" / "e2e").symlink_to(repo / ".compose" / "p" / "e2e")
        self.assertEqual(move(f"python3 {repo}/.compose/p/e2e/case.py"), f"python3 {root}/.compose/p/e2e/case.py")


T = 1_760_000_000  # fixed mtimes: the other record is 3 hours off, so its HH:MM never matches

def hm(t):
    return time.strftime("%H:%M", time.localtime(t))

class SmokeStatus(unittest.TestCase):
    def line(self, smoke=None, verify=None):
        """Each record is (dict, mtime); returns the project line of render_status."""
        repo = Path(tempfile.mkdtemp()); proj = repo / ".compose" / "p"; (proj / "results").mkdir(parents=True)
        for name, rec in (("smoke.json", smoke), ("verify.json", verify)):
            if rec:
                (proj / "results" / name).write_text(json.dumps(rec[0])); os.utime(proj / "results" / name, (rec[1], rec[1]))
        with mock.patch.object(fc.fc_tasks, "load_tasks", return_value=[]), mock.patch.object(fc.fc_core, "current_branch", return_value="main"), \
                mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(stdout="abc\n", returncode=0)), \
                mock.patch.object(fc.fc_worktree, "stack_order", return_value=[]), mock.patch.object(fc.fc_cost, "rows", return_value=[]):
            return fc.fc_status.render_status(repo, [proj]).splitlines()[0]

    def test_smoke_alone_shows_smoke(self):
        line = self.line(smoke=({"passed": True, "seconds": 2.0, "left_out": ["tasks"]}, T))
        self.assertIn(f"  smoke PASS (no tasks) 2.0s at {hm(T)}", line)
        self.assertNotIn("verify", line)

    def test_newer_smoke_wins_with_its_own_label_time_and_seconds(self):
        line = self.line(smoke=({"passed": True, "seconds": 125.6, "left_out": ["review", "e2e"]}, T), verify=({"passed": True, "seconds": 12.3}, T - 3 * 3600))
        self.assertIn(f"  smoke PASS (no review, no e2e) 2m05.6s at {hm(T)}", line)
        self.assertNotIn("verify", line)

    def test_newer_verify_wins_as_today(self):
        line = self.line(smoke=({"passed": True, "seconds": 2.0, "left_out": ["tasks", "review"]}, T - 3 * 3600), verify=({"passed": False, "seconds": 12.3}, T))
        self.assertTrue(line.endswith(f"0 tasks:   verify FAIL 12.3s at {hm(T)}"), line)
        self.assertNotIn("smoke", line)

    def test_failed_smoke_says_fail(self):
        line = self.line(smoke=({"passed": False, "seconds": 0.4, "left_out": ["tasks", "e2e"]}, T), verify=({"passed": True, "seconds": 12.3}, T - 3 * 3600))
        self.assertIn(f"  smoke FAIL (no tasks, no e2e) 0.4s at {hm(T)}", line)
        self.assertNotIn("PASS", line)


if __name__ == "__main__":
    unittest.main()
