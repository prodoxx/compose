import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


DRIVER = """
const handlers = {};
const mod = await import(process.argv[2]);
mod.default({on: (name, fn) => { handlers[name] = fn; }});
if (!handlers.tool_call) throw new Error("no tool_call handler registered");
const out = [];
for (const [line, event, path] of JSON.parse(process.argv[3])) {
  if (path) process.env.PATH = path;
  if (line === null) delete process.env.COMPOSE_READGUARD; else process.env.COMPOSE_READGUARD = line;
  out.push(await handlers.tool_call(event, {}));
}
console.log(JSON.stringify(out.map((r) => r === undefined ? null : r)));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class PiReadGuard(unittest.TestCase):
    """Task 006 (NEB-954): bin/pi_readguard.ts loaded with node through a stub of the pi API."""

    def drive(self, steps):
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        (d / "driver.mjs").write_text(DRIVER)
        env = {k: v for k, v in os.environ.items() if k != "COMPOSE_READGUARD"}
        p = subprocess.run(["node", str(d / "driver.mjs"), str(fc.fc_readguard.PI_EXT), json.dumps(steps)],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_a_reply_blocks_an_empty_one_passes_and_the_guard_gets_the_tool_and_its_input(self):
        log = Path(tempfile.mkdtemp()) / "stdin.log"
        self.addCleanup(shutil.rmtree, log.parent, True)
        rec = "cat >> '%s'; echo >> '%s'; " % (log, log)
        ev = lambda tool, args: {"type": "tool_call", "toolName": tool, "toolCallId": "c1", "input": args}
        steps = [[rec + "printf '  refused: caf\u00e9 is outside the task\\n\\n'", ev("read", {"path": "../caf\u00e9.py"})],
                 [rec, ev("bash", {"command": "cat /etc/hosts | head"})],
                 [None, ev("read", {"path": "/etc/passwd"})],
                 [rec + "echo refused >&2; exit 3", ev("grep", {"pattern": "def ", "path": "src"})],
                 [rec + "echo no", ev("ls", {})],
                 [rec + "kill -9 $$", ev("find", {"pattern": "*.py"})],
                 ["no-such-guard-cmd-006", ev("read", {"path": "a.py"})],
                 [rec, ev("read", {"path": "a.py"}), str(log.parent)]]
        out = self.drive(steps)
        self.assertEqual(out[:3] + out[4:5], [{"block": True, "reason": "refused: caf\u00e9 is outside the task"},
                                              None, None, {"block": True, "reason": "no"}])
        for i, said in [(3, "refused"), (5, ""), (6, "no-such-guard-cmd-006"), (7, "ENOENT")]:
            self.assertIs(out[i]["block"], True, out[i]); self.assertIn(said, out[i]["reason"])
        got = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(got, [{"tool": "read", "args": {"path": "../caf\u00e9.py"}},
                               {"tool": "bash", "args": {"command": "cat /etc/hosts | head"}},
                               {"tool": "grep", "args": {"pattern": "def ", "path": "src"}},
                               {"tool": "ls", "args": {}}, {"tool": "find", "args": {"pattern": "*.py"}}])


if __name__ == "__main__":
    unittest.main()
