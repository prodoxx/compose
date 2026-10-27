import hermetic  # first: no test sees the real home
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
import fc_pi as pi  # noqa: E402

MODEL = "z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast"


class PiChecks(unittest.TestCase):
    """The read side of pi's openrouter-fast provider, the TEXT_MODEL lines, and pi's own auth.json overrides."""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def file(self, text, name="f.json"):
        (self.dir / name).write_text(text if isinstance(text, str) else json.dumps(text))
        return self.dir / name

    def test_provider_declared(self):
        self.assertEqual(pi.PROVIDER["models"][0]["id"], MODEL)
        self.assertEqual((pi.PROVIDER["apiKey"], pi.PROVIDER["baseUrl"]), ("$OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"))
        self.assertEqual(pi.TEXT_MODEL_DEFAULTS, {"TEXT_MODEL": "inception/mercury-2.5", "TEXT_MODEL_BASE_URL": "https://openrouter.ai/api/v1"})
        other = {"anthropic": {"models": [{"id": "claude"}]}}
        key = {"apiKey": "$OPENROUTER_API_KEY"}
        self.assertTrue(pi.provider_declared(self.file({"providers": {**other, "openrouter-fast": {**key, "models": [{"id": "x"}, {"id": MODEL}]}}})))
        for bad in ({"providers": other}, {"providers": {"openrouter-fast": {**key, "models": [{"id": "x"}]}}},
                    {"providers": {"openrouter": {**key, "models": [{"id": MODEL}]}}}, {"providers": {"openrouter-fast": {}}}, "{not json", "[]",
                    {"providers": {"openrouter-fast": {"models": [{"id": MODEL}]}}}, {"providers": {"openrouter-fast": {"apiKey": "", "models": [{"id": MODEL}]}}}):
            self.assertFalse(pi.provider_declared(self.file(bad)), bad)
        self.assertFalse(pi.provider_declared(self.dir / "missing.json"))

    def test_pi_auth_overrides(self):
        o = pi.pi_auth_overrides
        key = {"type": "api_key", "key": "sk-or-test"}
        self.assertEqual(o(self.file({"openrouter-fast": key, "anthropic": key, "openrouter": key})), ["openrouter", "openrouter-fast"])
        self.assertEqual(o(self.file({"openrouter-fast": key})), ["openrouter-fast"])
        self.assertEqual(o(self.file({"anthropic": {"openrouter": key}})), [])
        self.assertEqual(o(self.file("{oops")), [])
        self.assertEqual(o(self.dir / "missing.json"), [])

class PiWrites(unittest.TestCase):
    """The write side: declare pi's openrouter-fast provider and append the TEXT_MODEL lines, never touching what is there."""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def twice(self, fn, path):
        """Run fn twice; the second run must change no byte. Returns the first line."""
        line = fn(path)
        before = path.read_bytes()
        self.assertIsInstance(fn(path), str)
        self.assertEqual(path.read_bytes(), before)
        return line

    def test_declare_provider(self):
        path = self.dir / "agent" / "models.json"
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}):
            line = self.twice(pi.declare_provider, path)
        self.assertEqual(json.loads(path.read_text()), {"providers": {"openrouter-fast": pi.PROVIDER}})
        self.assertNotIn("sk-or-test", path.read_text() + line)
        self.assertTrue(pi.provider_declared(path))
        other = {"version": 2, "providers": {"anthropic": {"models": [{"id": "claude"}]}}}
        path.write_text(json.dumps(other))
        self.assertIn("openrouter-fast", self.twice(pi.declare_provider, path))
        self.assertEqual(json.loads(path.read_text()), {"version": 2, "providers": {**other["providers"], "openrouter-fast": pi.PROVIDER}})
        path.write_text(json.dumps({"version": 2}))
        pi.declare_provider(path)
        self.assertEqual(json.loads(path.read_text()), {"version": 2, "providers": {"openrouter-fast": pi.PROVIDER}})

    def test_declare_provider_completes_existing(self):
        path, model = self.dir / "models.json", pi.PROVIDER["models"][0]
        mine = {"baseUrl": "http://mine", "api": "openai-completions", "models": [{"id": "x"}, {"id": MODEL, "cost": {"input": 1}}]}
        for have, want in ((mine, {**mine, "apiKey": "$OPENROUTER_API_KEY"}), ({**mine, "apiKey": "!pass or"}, {**mine, "apiKey": "!pass or"}),
                           ({"models": []}, {"models": [model], "apiKey": "$OPENROUTER_API_KEY"}), ({"apiKey": ""}, {"apiKey": "$OPENROUTER_API_KEY", "models": [model]})):
            path.write_text(json.dumps({"version": 2, "providers": {"openrouter-fast": have}}))
            self.assertIn("openrouter-fast", self.twice(pi.declare_provider, path))
            self.assertEqual(json.loads(path.read_text()), {"version": 2, "providers": {"openrouter-fast": want}}, have)
            self.assertTrue(pi.provider_declared(path), have)

    def test_declare_provider_leaves_malformed(self):
        path = self.dir / "models.json"
        for text in ("{not json", "[]", '{"providers": []}', '{"providers": {"openrouter-fast": []}}', '{"providers": {"openrouter-fast": {"models": {}}}}'):
            path.write_text(text)
            self.assertIn("by hand", pi.declare_provider(path), text)
            self.assertEqual(path.read_text(), text)

    def test_write_text_model(self):
        env, d = self.dir / ".env", pi.TEXT_MODEL_DEFAULTS
        self.twice(pi.write_text_model, env)
        self.assertEqual(env.read_text().splitlines(), [f"{k}={v}" for k, v in d.items()])
        self.assertTrue(pi.text_model_set(env))
        env.write_text("A=1\nTEXT_MODEL_API_KEY=sk-or-test\nTEXT_MODEL_BASE_URL=http://mine\nB=2")
        line = self.twice(pi.write_text_model, env)
        self.assertNotIn("sk-or-test", line)
        self.assertEqual(env.read_text().splitlines(),
                         ["A=1", "TEXT_MODEL_API_KEY=sk-or-test", "TEXT_MODEL_BASE_URL=http://mine", "B=2", f"TEXT_MODEL={d['TEXT_MODEL']}"])
        mine = "TEXT_MODEL=my/model\nTEXT_MODEL_BASE_URL=http://mine\n"
        env.write_text(mine)
        self.twice(pi.write_text_model, env)
        self.assertEqual(env.read_text(), mine)


class PreviewProvider(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def preview(self, path):
        """preview_provider's line, checking the file's bytes and whether it and its directory exist stay as they were."""
        state = lambda: (path.parent.exists(), path.exists(), path.exists() and path.read_bytes())
        before = state()
        line = pi.preview_provider(path)
        self.assertEqual(state(), before)
        return line

    def test_lacking_provider_would_get_what_declare_adds(self):
        path, glm = self.dir / "models.json", [{"id": "x"}, {"id": MODEL}]
        for have, what in (({"models": glm}, "apiKey"), ({"apiKey": "", "models": glm}, "apiKey"), ({"apiKey": "k", "models": [{"id": "x"}]}, "the GLM preset model"),
                           ({"apiKey": "k"}, "the GLM preset model"), ({"models": []}, "apiKey and the GLM preset model"), ({}, "apiKey and the GLM preset model")):
            path.write_text(json.dumps({"version": 2, "providers": {"openrouter-fast": have}}))
            self.assertEqual(self.preview(path), f"would add {what} to openrouter-fast in {path}", have)
            self.assertEqual(pi.declare_provider(path), f"added {what} to openrouter-fast in {path}", have)
            self.assertEqual(self.preview(path), f"openrouter-fast already declared in {path}; left as is", have)
            self.assertEqual(pi.declare_provider(path), f"openrouter-fast already declared in {path}; left as is", have)

    def test_broken_file_gets_declares_fix_by_hand_line(self):
        path = self.dir / "models.json"
        for text in ("{not json", "[]", '{"providers": []}', '{"providers": {"openrouter-fast": []}}', '{"providers": {"openrouter-fast": {"models": {}}}}'):
            path.write_text(text)
            line = self.preview(path)
            self.assertEqual((line, "by hand" in line), (pi.declare_provider(path), True), text)

    def test_provider_lacks_in_order(self):
        for have, want in (({"apiKey": "k", "models": [{"id": "x"}, {"id": MODEL}]}, []), ({"models": [{"id": MODEL}]}, ["apiKey"]),
                           ({"apiKey": "k", "models": [{"id": "x"}]}, ["the GLM preset model"]), ({"apiKey": "", "models": []}, ["apiKey", "the GLM preset model"])):
            self.assertEqual(list(pi.provider_lacks(have)), want, have)
