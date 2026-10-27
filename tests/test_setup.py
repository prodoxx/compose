import hermetic  # first: no test sees the real home
import contextlib, inspect, io, os, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import fc_prompt, fc_steps, fc_steprun, fc_keys, fc_finish, fc_config, fc_perms, fc_setup

CHECKOUT = Path(fc_setup.__file__).resolve().parents[1]
RESULTS = [("gh", "done")]

class Out(io.StringIO): isatty = lambda self: self.tty

class Flow(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); self.home, self.links = Path(os.path.realpath(t.name)), []
        (self.home / "config.json").write_text('{"workspace": "acme", "workspaces": {"acme": {}}}')
        for p in (mock.patch.dict(os.environ, {"HOME": str(self.home)}), mock.patch.object(fc_config, "CONFIG_PATH", self.home / "config.json"),
                  mock.patch.object(fc_setup.fc_checks, "owned_links", side_effect=lambda c: self.links)):
            p.start(); self.addCleanup(p.stop)

    def run_setup(self, yes=False, stdin=True, stdout=True, mode=1, designer=True):
        log, out = [], Out(); out.tty = stdout
        def fake(module, name, ret=None):
            sig = inspect.signature(getattr(module, name))
            def call(*a, **k):
                b = sig.bind(*a, **k); b.apply_defaults(); log.append((name, *b.arguments.values())); return ret
            return mock.patch.object(module, name, side_effect=call)
        @contextlib.contextmanager
        def spin(label):
            log.append(("spin", label)); yield; log.append(("spun",))
        with fake(fc_prompt, "intro"), fake(fc_prompt, "select", mode), fake(fc_prompt, "note"), fake(fc_prompt, "outro"), \
             fake(fc_setup, "walk_steps", (RESULTS, designer)), fake(fc_keys, "ask_keys"), fake(fc_finish, "finish", (["a", "b", "end"], 3)), \
             mock.patch.object(fc_perms, "offer", return_value=[]), \
             mock.patch.object(fc_prompt, "spin", spin), mock.patch.object(fc_steps, "STEPS", ()), mock.patch("shutil.which", return_value=None), \
             mock.patch("os.execvp", side_effect=AssertionError("exec")), mock.patch.object(sys, "stdin", SimpleNamespace(isatty=lambda: stdin)), \
             mock.patch.object(sys, "stdout", out), self.assertRaises(SystemExit) as r:
            fc_setup.cmd_setup(SimpleNamespace(yes=yes, dry_run=False))
        return r.exception.code, out.getvalue(), log

    def assert_leftover(self):
        code, text, log = self.run_setup()
        self.assertEqual((code, len(text.strip().splitlines()), log), (1, 1, []))
        self.assertIn(str(CHECKOUT / "bin" / "migrate-to-compose.sh"), text)

    def test_fast_coding_dir_stops_setup(self):
        (self.home / ".fast-coding").mkdir(); self.assert_leftover()

    def test_owned_link_into_fast_coding_stops_setup(self):
        old = self.home / "src" / "fast-coding"; (old / "bin").mkdir(parents=True); (old / "bin" / "compose").touch()
        link = self.home / ".local" / "bin" / "compose"; link.parent.mkdir(parents=True); link.symlink_to(old / "bin" / "compose")
        self.links = [("compose CLI link", link, CHECKOUT / "bin" / "compose")]; self.assert_leftover()

    def test_terminal_runs_each_part_in_order_and_exits_with_finish_code(self):
        self.assertEqual(fc_setup.MODES, ("Quick start (every default, designer extras off)", "Custom (choose each step)"))
        for mode, designer in ((1, True), (0, False)):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_setup(mode=mode, designer=designer), (3, "", [
                    ("intro", "compose setup"), ("select", "How do you want to set up?", fc_setup.MODES, 0),
                    ("walk_steps", CHECKOUT, mode == 1, False, False), ("ask_keys", "acme", fc_steps.FRAMELINK_ENV, designer, False),
                    ("spin", "checking your model"), ("finish", RESULTS, False, False), ("spun",), ("note", "Summary", ["a", "b"]), ("outro", "end")]))

    def test_yes_or_no_terminal_asks_nothing_and_finish_gets_yes(self):
        for kw in ({"yes": True}, {"stdin": False}, {"stdout": False}):
            with self.subTest(**kw):
                self.assertEqual(self.run_setup(**kw), (3, "", [("intro", "compose setup"), ("walk_steps", CHECKOUT, False, True, False),
                                                                ("finish", RESULTS, True, False), ("note", "Summary", ["a", "b"]), ("outro", "end")]))

def row(name, level="default", mode="run", ok=False):
    return (name, lambda c: (ok, ""), f"install {name}", level, False, mode)

ROWS = (row("gh", "required"), row("git", ok=True), row("brew", mode="interactive"), row("figma", "designer"),
        row("OpenRouter key", "required", mode="key"), row("pi"))
SIG = {f.__name__: inspect.signature(f) for f in (fc_prompt.confirm, fc_prompt.step, fc_steprun.run_step)}
arg = lambda f, a, k: SIG[f].bind(*a, **k).arguments

class Walk(unittest.TestCase):
    def walk(self, custom, yes, answers=None, states=None, claude=True):
        self.inside, self.runs = None, []
        @contextlib.contextmanager
        def spin(label):
            self.inside = label
            try: yield
            finally: self.inside = None
        def run(*a, **k):
            got = arg("run_step", a, k); self.runs.append((got["row"][0], got["checkout"], got["yes"], got["claude"], got["terminal"], self.inside))
            return (states or {}).get(got["row"][0], ("done", ""))
        with mock.patch.object(fc_steps, "STEPS", ROWS), mock.patch.object(fc_steprun, "run_step", side_effect=run), \
             mock.patch.object(fc_prompt, "confirm", side_effect=lambda *a, **k: answers[a[0] if a else k["question"]]) as ask, \
             mock.patch.object(fc_prompt, "spin", spin), mock.patch.object(fc_prompt, "step") as step, \
             mock.patch("shutil.which", lambda n, *a, **k: f"/bin/{n}" if claude else None), mock.patch("sys.stdout", io.StringIO()):
            got = fc_setup.walk_steps(CHECKOUT, custom=custom, yes=yes)
        self.asks = [(b["question"], b["default"]) for b in (arg("confirm", c.args, c.kwargs) for c in ask.call_args_list)]
        self.steps = [(a["name"], a["word"], list(a.get("lines", ()))) for a in (arg("step", c.args, c.kwargs) for c in step.call_args_list)]
        return got

    def test_custom_asks_once_per_missing_step(self):
        answers = {"Set up gh?": False, "Set up brew?": True, "Set up figma?": True, "Set up pi?": False}
        got = self.walk(True, False, answers, {"figma": ("failed", "boom\nrun by hand: install figma")})
        self.assertEqual(self.asks, [("Set up gh?", True), ("Set up brew?", True), ("Set up figma?", False), ("Set up pi?", True)])
        self.assertEqual(self.steps, [("gh", "skipped", ["compose stops working without it; by hand: install gh"]), ("git", "ok", []),
                                      ("brew", "done", []), ("figma", "failed", ["boom", "run by hand: install figma"]), ("pi", "skipped", [])])
        self.assertEqual(self.runs, [("brew", CHECKOUT, False, True, True, None), ("figma", CHECKOUT, False, True, True, "figma")])
        self.assertEqual(got, ([("gh", "skipped"), ("git", "ok"), ("brew", "done"), ("figma", "failed"), ("pi", "skipped")], True))

    def test_custom_with_the_designer_row_declined_is_no_designer(self):
        got = self.walk(True, False, {"Set up gh?": True, "Set up brew?": True, "Set up figma?": False, "Set up pi?": True})
        self.assertEqual((got[1], ("figma", "skipped", []) in self.steps, [r[0] for r in self.runs]), (False, True, ["gh", "brew", "pi"]))

    def test_quick_start_and_yes_ask_nothing_and_run_every_row_but_designer_and_keys(self):
        for yes in (False, True):
            with self.subTest(yes=yes):
                got = self.walk(False, yes, {}, {"gh": ("failed", "boom\nrun by hand: install gh"), "git": ("ok", "")}, claude=not yes)
                self.assertEqual(self.asks, [])
                self.assertEqual(self.runs, [(n, CHECKOUT, yes, not yes, not yes, None if n == "brew" and not yes else n) for n in ("gh", "git", "brew", "pi")])
                self.assertEqual(self.steps, [("gh", "failed", ["boom", "run by hand: install gh"]), ("git", "ok", []), ("brew", "done", []),
                                              ("figma", "skipped", ["designer extra, skipped"]), ("pi", "done", [])])
                self.assertEqual(got, ([("gh", "failed"), ("git", "ok"), ("brew", "done"), ("figma", "skipped"), ("pi", "done")], False))

    def test_a_claude_state_is_shown_as_waiting_on_claude_code(self):
        got = self.walk(False, True, {}, {"pi": ("claude", "")}, claude=False)
        self.assertEqual((self.steps[-1], got[0][-1]), (("pi", "waiting on Claude Code", []), ("pi", "claude")))

ENDING = ["would add default models to ~/.compose/config.json: a", "copied herdr", "would move linear out of ~/.compose/.env", "not finished"]
class DryFlow(unittest.TestCase):
    def flow(self, yes, dry, rows=ROWS, keys=["openrouter"], ending=(ENDING, 1), offered=[]):
        self.log, out = [], Out(); out.tty = True
        def spy(module, name, ret):
            sig = inspect.signature(getattr(module, name))
            def call(*a, **k):
                b = sig.bind(*a, **k); b.apply_defaults(); self.log.append((name, b.arguments["dry"]))
                return ret(**b.arguments) if callable(ret) else ret
            return mock.patch.object(module, name, side_effect=call)
        run = lambda row, checkout, dry, **k: ("ok", "") if row[1](checkout)[0] else ("would", f"would run: {row[2]}") if dry else ("done", "")
        answers = {"Set up gh?": True, "Set up brew?": True, "Set up figma?": False, "Set up pi?": False}
        with mock.patch.object(fc_steps, "STEPS", rows), spy(fc_steprun, "run_step", run), spy(fc_keys, "ask_keys", keys), \
             mock.patch.object(fc_perms, "offer", return_value=offered) as self.offer, \
             spy(fc_finish, "finish", ending), mock.patch.object(fc_prompt, "spin", lambda label: contextlib.nullcontext()), \
             mock.patch.object(fc_prompt, "confirm", side_effect=lambda question, default: answers[question]) as self.confirm, \
             mock.patch.object(fc_prompt, "select", return_value=1) as self.select, mock.patch.object(fc_prompt, "password", return_value=""), \
             mock.patch.object(fc_keys, "key_status", return_value=dict.fromkeys(["openrouter", "typesafe", "linear", "figma"], True)), \
             mock.patch.object(fc_prompt, "intro") as intro, mock.patch.object(fc_prompt, "note") as note, mock.patch.object(fc_prompt, "outro") as outro, \
             mock.patch("shutil.which", lambda n, *a, **k: f"/bin/{n}"), mock.patch.dict(os.environ, {"NO_COLOR": "1"}), mock.patch("sys.stdout", out):
            code = fc_setup.run_setup(CHECKOUT, yes, dry=dry)
        return code, out.getvalue().splitlines(), intro.call_args.args, [c.args for c in note.call_args_list if c.args[0] == "Summary"], outro.call_args.args

    def test_dry_asks_what_a_real_run_asks_and_counts_each_change(self):
        real = self.flow(False, False), self.confirm.call_args_list, self.select.call_args_list
        code, lines, intro, summary, outro = self.flow(False, True)
        self.assertEqual((self.confirm.call_args_list, self.select.call_args_list), real[1:])
        self.assertEqual(([l for l in lines if "would" in l], {d for _, d in self.log}), (["◇  gh: would run: install gh", "◇  brew: would run: install brew"], {True}))
        self.assertEqual((code, intro, summary, outro), (1, ("compose setup (dry run)",), [("Summary", ENDING)],
                                                         ("dry run: nothing changed; 5 changes listed above",)))

    def test_yes_dry_run_asks_nothing(self):
        code, lines, intro, summary, outro = self.flow(True, True, ending=(ENDING[1:], 0))
        self.assertEqual((code, outro, self.confirm.call_count + self.select.call_count, [n for n, _ in self.log if n != "run_step"]),
                         (0, ("dry run: nothing changed; 4 changes listed above",), 0, ["finish"]))

    def test_set_up_machine_lists_no_change(self):
        rows = tuple(row(r[0], r[3], r[5], ok=True) for r in ROWS if r[3] != "designer")
        code, lines, intro, summary, outro = self.flow(False, True, rows, fc_keys.ask_keys, (["herdr config is current", "ready"], 0))
        self.assertEqual([l for l in lines if l.startswith("◇")], [f"◇  {r[0]}: ok" for r in rows if r[5] != "key"])
        self.assertEqual((code, outro, self.confirm.call_count, sum(l.endswith(" [set]") for l in lines)),
                         (0, ("dry run: nothing changed; 0 changes listed above",), 0, 3))

    def test_permission_rules_are_offered_after_the_steps_and_lead_the_summary(self):
        code, lines, intro, summary, outro = self.flow(False, True, offered=["would add 3 permission rule(s)"])
        self.assertEqual((self.offer.call_args, summary, outro), (mock.call(False, True), [("Summary", ["would add 3 permission rule(s)", *ENDING])],
                                                                  ("dry run: nothing changed; 6 changes listed above",)))
        self.flow(False, False)
        self.assertEqual(self.offer.call_args, mock.call(False, False))
