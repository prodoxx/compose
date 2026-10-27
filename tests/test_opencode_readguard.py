import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


DRIVER = """
const hook = (await (await import(process.argv[2])).ReadGuard({directory: process.cwd()}))["tool.execute.before"];
const out = [];
for (const [line, tool, args, path] of JSON.parse(process.argv[3])) {
  if (path) process.env.PATH = path;
  if (line === null) delete process.env.COMPOSE_READGUARD; else process.env.COMPOSE_READGUARD = line;
  try { await hook({tool, sessionID: "s", callID: "c"}, {args}); out.push(null); } catch (e) { out.push(String(e.message)); }
}
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class OpencodeReadGuard(unittest.TestCase):
    """Task 007 (NEB-955): bin/opencode_readguard.js loaded with node through a stub."""

    def test_a_reply_throws_an_empty_one_passes_a_failing_guard_throws_and_the_guard_gets_the_renamed_args(self):
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True); log = d / "stdin.log"
        (d / "driver.mjs").write_text(DRIVER); rec = "cat >> '%s'; echo >> '%s'; " % (log, log)
        steps = [[rec + "printf '  refused: caf\u00e9 is outside\\n\\n'", "read", {"filePath": "/etc/caf\u00e9", "offset": 3}],
                 [rec, "bash", {"command": "cat a | head", "description": "x"}],
                 [None, "read", {"filePath": "/etc/passwd"}],
                 [rec + "echo boom-007 >&2; exit 3", "grep", {"pattern": "def ", "path": "src"}],
                 [rec + "kill -9 $$", "list", {"path": "."}],
                 ["no-such-guard-cmd-007", "read", {"filePath": "a.py"}],
                 [rec, "read", {"filePath": "a.py"}, str(d)]]
        p = subprocess.run(["node", str(d / "driver.mjs"), str(fc.fc_readguard.OC_PLUGIN), json.dumps(steps)],
                           cwd=d, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((out := json.loads(p.stdout))[:3], ["refused: caf\u00e9 is outside", None, None])
        for i, said in [(3, "boom-007"), (4, ""), (5, "no-such-guard-cmd-007"), (6, "ENOENT")]:
            self.assertIsInstance(out[i], str, i); self.assertIn(said, out[i])
        self.assertEqual([json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()],
                         [{"tool": "read", "args": {"file_path": "/etc/caf\u00e9"}}, {"tool": "bash", "args": {"command": "cat a | head"}},
                          {"tool": "grep", "args": {"path": "src"}}, {"tool": "list", "args": {"path": "."}}])


if __name__ == "__main__":
    unittest.main()
