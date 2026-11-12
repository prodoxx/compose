import hermetic  # first: no test sees the real home
import json, os, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from tests.test_trail_setup import fc
import fc_rex, fc_host  # noqa: E402
os.environ["HERDR_ENV"] = ""

S, W, B = "session:01a", "window:02b", "block:03c"
SESSIONS = json.dumps({"sessions": [{"session_id": S, "label": "shop"}]})
BLOCKS = json.dumps({"blocks": [{"block_id": "block:00s", "label": "shell", "flavor": "com.superlogical.terminal.shell", "window_id": "window:00w"},
                                {"block_id": B, "label": "shop-001 · 001 Parse", "flavor": "com.superlogical.terminal.shell", "window_id": W},
                                {"block_id": "block:04d", "label": "notes · x", "flavor": "com.superlogical.notes", "window_id": W}]})
WORKING = json.dumps({"records": [{"state": "working", "app": "pi"}]})


class Rex:
    """A fake `subprocess.run` for rex: `answers` maps an argv prefix (after the binary) to stdout or (stdout, returncode, stderr)."""
    def __init__(self, **answers):
        self.calls, self.answers = [], {tuple(k.split("__")): v for k, v in answers.items()}

    def __call__(self, argv, **kw):
        self.calls.append(list(argv[1:]))
        for key, value in sorted(self.answers.items(), key=lambda kv: -len(kv[0])):  # the most specific answer wins
            if tuple(argv[1:1 + len(key)]) == key:
                out, code, err = (value, 0, "") if isinstance(value, str) else value
                return SimpleNamespace(returncode=code, stdout=out, stderr=err)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    def argv(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


class Calls(unittest.TestCase):
    def call(self, rex, *args, **kw):
        with mock.patch.object(fc_rex.subprocess, "run", rex), mock.patch.object(fc_host, "rex_bin", return_value="/bin/rex"):
            return fc_rex.rex_err(*args, **kw)

    def test_json_goes_before_the_program_and_acting_commands_take_none(self):
        rex = Rex(window__new='{"window_id": "w", "block_ids": ["b"]}')
        self.assertEqual(self.call(rex, "window", "new", "-s", S, "x", "--", "env", "A=1", "cat")[0], {"window_id": "w", "block_ids": ["b"]})
        self.assertEqual(rex.calls[0], ["window", "new", "-s", S, "x", "--json", "--", "env", "A=1", "cat"])
        self.assertEqual(self.call(rex, "send", "-b", B, "hi"), ({}, None))
        self.assertEqual(self.call(rex, "block", "rename", B, "x"), ({}, None))
        self.assertEqual(self.call(rex, "block", "call", "-b", B, "process"), ({}, None))
        self.assertEqual(rex.calls[1:], [["send", "-b", B, "hi"], ["block", "rename", B, "x"], ["block", "call", "-b", B, "process"]])

    def test_a_call_reaches_every_session_not_only_the_one_compose_runs_in(self):
        # Rex scopes `-b BLOCK` to REX_SESSION when it is set, so a block in the project's session is "not found" from the manager's
        # terminal: every call runs without this terminal's REX_SESSION and REX_BLOCK, and names a session itself where it needs one.
        seen = []
        rex = Rex()
        def run(argv, **kw):
            seen.append(kw.get("env") or {})
            return rex(argv, **kw)
        with mock.patch.dict(os.environ, {"REX_SESSION": "session:me", "REX_BLOCK": "block:me", "REX_SERVER": "unix:///s"}):
            self.call(run, "block", "call", "-b", B, "process")
        self.assertNotIn("REX_SESSION", seen[0])
        self.assertNotIn("REX_BLOCK", seen[0])
        self.assertEqual(seen[0].get("REX_SERVER"), "unix:///s")

    def test_exit_3_is_not_found_and_capture_is_text(self):
        rex = Rex(block__inspect=("", 3, 'Error: block target "x" not found'), capture="one\ntwo\n")
        self.assertEqual(self.call(rex, "block", "inspect", "x"), (None, "not_found"))
        self.assertEqual(self.call(rex, "capture", "-b", B, text=True), ("one\ntwo\n", None))

    def test_without_the_cli_nothing_runs(self):
        with mock.patch.object(fc_host, "rex_bin", return_value=None):
            self.assertEqual(fc_rex.rex_err("ls"), (None, "rex is not installed"))


class Names(unittest.TestCase):
    def test_a_composed_label_names_its_agent_a_plain_one_does_not(self):
        self.assertEqual(fc_rex.name_of("shop-001 · 001 Parse · attempt 1/3"), "shop-001")
        self.assertIsNone(fc_rex.name_of("shell"))
        self.assertIsNone(fc_rex.name_of("manager: shop"))
        self.assertIsNone(fc_rex.name_of("Shop-001 · x"))


class Agents(unittest.TestCase):
    def setUp(self):
        self.rex = Rex(ls=SESSIONS, block__ls=BLOCKS, block__call=WORKING)
        self.patches = [mock.patch.object(fc_rex.subprocess, "run", self.rex), mock.patch.object(fc_host, "rex_bin", return_value="/bin/rex")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_labelled_terminal_blocks_over_every_session_with_their_status(self):
        self.assertEqual(fc_rex.agents(), [{"name": "shop-001", "pane_id": B, "workspace_id": S, "tab_id": W, "agent_status": "working"}])
        self.assertEqual(self.rex.argv("block", "call"), [["block", "call", "-b", B, "program_status"]])

    def test_one_agent_by_name_or_block_with_its_process(self):
        self.rex.answers[("block", "call", "-b", B, "process")] = json.dumps({"foreground": {"pid": 77, "cwd": "/w/shop"}})
        for ref in ("shop-001", B):
            self.assertEqual(fc_rex.agent(ref), {"name": "shop-001", "pane_id": B, "workspace_id": S, "tab_id": W, "agent_status": "working", "pids": [77], "cwd": "/w/shop", "label": "shop-001 · 001 Parse"})
        self.assertIsNone(fc_rex.agent("nobody"))

    def test_status_words_and_a_blocked_block_refuses_a_prompt(self):
        self.rex.answers[("block", "call")] = json.dumps({"records": [{"state": "error", "app": "pi"}, {"id": "sub", "state": "idle"}]})
        self.assertEqual(fc_rex.status(B), "blocked")
        self.rex.answers[("block", "call")] = json.dumps({"records": []})
        self.assertIsNone(fc_rex.status(B))
        self.rex.answers[("block", "call")] = json.dumps({"records": [{"state": "blocked", "kind": "question"}]})
        self.assertEqual(fc_rex.prompt("shop-001", "hi"), (None, "agent_blocked"))
        self.assertEqual(fc_rex.prompt("nobody", "hi"), (None, "agent_not_found"))

    def test_a_prompt_is_pasted_then_entered_and_a_wait_follows_the_status(self):
        self.rex.answers[("block", "call")] = json.dumps({"records": [{"state": "idle"}]})
        self.assertEqual(fc_rex.prompt("shop-001", "hello\nworld"), ({"block": B}, None))
        self.assertEqual(self.rex.calls[-2:], [["send", "-b", B, "hello\nworld"], ["send-key", "-b", B, "enter"]])
        states = iter(["idle", "working", "working", "done"])
        self.rex.__class__ = type("R", (Rex,), {"__call__": lambda s, argv, **kw: (s.calls.append(argv[1:]) or SimpleNamespace(returncode=0, stderr="", stdout=(
            json.dumps({"records": [{"state": next(states, "done")}]}) if argv[1:3] == ["block", "call"] else SESSIONS if argv[1] == "ls" else BLOCKS if argv[1:3] == ["block", "ls"] else "{}")))})
        with mock.patch.object(fc_rex.time, "sleep"):
            self.assertEqual(fc_rex.prompt("shop-001", "go", wait=True, timeout=30), ({"block": B}, None))

    def test_a_turn_that_never_shows_is_a_stall_and_one_that_never_ends_a_timeout(self):
        self.rex.answers[("block", "call")] = json.dumps({"records": [{"state": "idle"}]})
        with mock.patch.object(fc_rex.time, "sleep"):
            self.assertEqual(fc_rex.prompt("shop-001", "go", wait=True, timeout=30)[1], "agent_prompt_stalled")
        clock = SimpleNamespace(now=1000.0)
        with mock.patch.object(fc_rex, "time", SimpleNamespace(time=lambda: clock.now, sleep=lambda s: setattr(clock, "now", clock.now + s))):
            self.assertIsNone(fc_rex.wait("shop-001", timeout=10))  # idle: the turn is over
        self.rex.answers[("block", "call")] = WORKING
        with mock.patch.object(fc_rex, "time", SimpleNamespace(time=lambda: clock.now, sleep=lambda s: setattr(clock, "now", clock.now + s))):
            self.assertEqual(fc_rex.wait("shop-001", timeout=10), "timeout")
            self.assertEqual(fc_rex.wait("shop-001", timeout=10, until="idle"), "timeout")
        self.assertEqual(fc_rex.wait("nobody"), "agent_not_found")

    def test_keys_and_read(self):
        self.rex.answers[("capture",)] = "a\nb\nc\nd\n"
        self.assertTrue(fc_rex.send_keys("shop-001", "esc"))
        self.assertEqual(fc_rex.read("shop-001", lines=2), "c\nd")
        self.assertEqual(fc_rex.read("nobody"), "")
        self.assertEqual(self.rex.argv("send-key"), [["send-key", "-b", B, "esc"]])
        self.assertEqual(self.rex.argv("capture"), [["capture", "-b", B, "--trim", "--unwrap"]])


class Start(unittest.TestCase):
    def setUp(self):
        self.rex = Rex(ls=SESSIONS, block__ls=BLOCKS, window__ls=json.dumps({"windows": [{"window_id": W, "label": "implementers"}, {"window_id": "window:00w", "label": "shell"}]}),
                       window__new=json.dumps({"window_id": "window:NEW", "block_ids": ["block:NEW"]}), split=json.dumps({"block_ids": ["block:SPLIT"]}))
        for p in (mock.patch.object(fc_rex.subprocess, "run", self.rex), mock.patch.object(fc_host, "rex_bin", return_value="/bin/rex"),
                  mock.patch.object(fc_host, "pane_env", return_value={"HOME": "/s/home"})):
            p.start()
            self.addCleanup(p.stop)

    def test_a_tab_of_its_own_is_a_window_named_for_it_and_the_block_is_labelled(self):
        self.assertEqual(fc_rex.start(S, "architect", "/w/shop", {"COMPOSE_ROLE": "crew"}, "shop-architect", ["claude", "--x"], title="architect"), {"tab": "window:NEW", "pane": "block:NEW"})
        self.assertEqual(self.rex.argv("window", "new"), [["window", "new", "-s", S, "architect", "--focus=false", "-c", "/w/shop", "--json", "--", "env", "HOME=/s/home", "COMPOSE_ROLE=crew", "claude", "--x"]])
        self.assertEqual(self.rex.argv("block", "rename"), [["block", "rename", "block:NEW", "shop-architect · architect"]])

    def test_joining_the_implementers_window_splits_its_newest_block(self):
        self.assertEqual(fc_rex.start(S, "implementers", "/w/shop", {}, "shop-002", ["pi"], join=True, title="002 Store"), {"tab": W, "pane": "block:SPLIT"})
        self.assertEqual(self.rex.argv("split"), [["split", "-s", S, "-b", B, "--split", "right", "--label", "shop-002 · 002 Store", "--focus=false", "-c", "/w/shop", "--json", "--", "env", "HOME=/s/home", "pi"]])

    def test_without_a_session_or_on_a_refusal_it_raises(self):
        with self.assertRaises(RuntimeError):
            fc_rex.start(None, "x", "/w", {}, "a", ["cat"])
        self.rex.answers[("window", "new")] = ("", 1, "boom")
        with self.assertRaises(RuntimeError) as e:
            fc_rex.start(S, "x", "/w", {}, "a", ["cat"])
        self.assertIn("boom", str(e.exception))


class Rule(unittest.TestCase):
    def test_a_teammate_in_rex_is_told_to_keep_the_sessions_name(self):
        import fc_fleet
        with mock.patch.dict(os.environ, {"COMPOSE_HOST": "rex", "HERDR_ENV": ""}):
            self.assertIn("never rename the Rex session", fc_fleet.teammate_rule())
        with mock.patch.dict(os.environ, {"COMPOSE_NO_HERDR": "1"}):
            self.assertEqual(fc_fleet.teammate_rule(), "")


class Sessions(unittest.TestCase):
    def setUp(self):
        self.rex = Rex(ls=SESSIONS, block__ls=BLOCKS, new=json.dumps({"session_id": "session:NEW"}), session__inspect=json.dumps({"label": "shop"}),
                       window__inspect=("", 3, "not found"), window__ls=json.dumps({"windows": [{"window_id": W, "label": "verify"}]}))
        for p in (mock.patch.object(fc_rex.subprocess, "run", self.rex), mock.patch.object(fc_host, "rex_bin", return_value="/bin/rex")):
            p.start()
            self.addCleanup(p.stop)

    def test_found_by_label_else_made_with_a_shell_window(self):
        self.assertEqual(fc_rex.space("shop", "/w/shop"), S)
        self.assertEqual(fc_rex.space("cart", "/w/cart"), "session:NEW")
        self.assertEqual(self.rex.argv("new"), [["new", "cart", "-c", "/w/cart", "--window", "shell", "--json", "--", *fc_rex.shell_tab()]])
        self.assertIsNone(fc_rex.find_space("nobody", "/w"))

    def test_the_shell_tab_opens_at_a_prompt_not_at_the_update_question(self):
        with mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}):
            self.assertEqual(fc_rex.shell_tab(), ["env", "DISABLE_AUTO_UPDATE=true", "DISABLE_UPDATE_PROMPT=true", "/bin/zsh", "-l"])

    def test_a_new_session_sits_under_the_session_this_one_runs_in_after_its_other_projects(self):
        self.rex.answers[("ls",)] = json.dumps({"sessions": [{"session_id": "session:A", "label": "dtt"}, {"session_id": "session:B", "label": "dtt › recommender"},
                                                             {"session_id": "session:C", "label": "other"}]})
        with mock.patch.dict(os.environ, {"REX_SESSION": "session:A"}):
            self.assertEqual(fc_rex.space("cart", "/w/cart"), "session:NEW")
        self.assertEqual(self.rex.argv("new"), [["new", "dtt › cart", "-c", "/w/cart", "--window", "shell", "--json", "--", *fc_rex.shell_tab()]])
        self.assertEqual(self.rex.argv("session", "move"), [["session", "move", "session:NEW", "--after", "session:B"]])
        self.assertEqual(fc_rex.find_space("recommender", "/w"), "session:B")

    def test_a_renamed_session_is_still_the_projects_and_gets_its_name_back(self):
        # A Claude Code teammate in the session is told by Rex's SessionStart hook to name "its" session after its task, so the
        # project's session turned `shop-architect`: compose keeps the session's id with the project and names it back.
        import tempfile
        wt = Path(tempfile.mkdtemp())
        (wt / ".compose" / "cart").mkdir(parents=True)
        self.assertEqual(fc_rex.space("cart", wt), "session:NEW")
        self.rex.answers[("ls",)] = json.dumps({"sessions": [{"session_id": "session:NEW", "label": "cart-architect"}]})
        self.assertEqual(fc_rex.find_space("cart", wt), "session:NEW")
        self.assertEqual(self.rex.argv("session", "rename"), [["session", "rename", "session:NEW", "cart"]])
        self.rex.answers[("ls",)] = json.dumps({"sessions": []})  # the session was closed: the record no longer counts
        self.assertIsNone(fc_rex.find_space("cart", wt))

    def test_tabs_gone_closing_focus_and_the_managers_label(self):
        self.assertEqual(fc_rex.tabs(S), {"verify": {"tab_id": W, "label": "verify"}})
        self.assertTrue(fc_rex.tab_gone(W))
        self.assertTrue(fc_rex.space_is(S, "shop"))
        self.rex.answers[("block", "inspect")] = ("", 3, "not found")
        self.assertFalse(fc_rex.live("block:gone"))
        fc_rex.close_pane(B)
        fc_rex.close_tab(W)
        fc_rex.close_space(S)
        fc_rex.focus(space=S, tab=W, pane=B)
        self.assertEqual(self.rex.argv("block", "close"), [["block", "close", B]])
        self.assertEqual(self.rex.argv("window", "close"), [["window", "close", W]])
        self.assertEqual(self.rex.argv("kill"), [["kill", S]])
        self.assertEqual(self.rex.argv("focus"), [["focus", "-s", S, W]])
        self.assertEqual(self.rex.argv("do"), [["do", "session.select", f"session_id={S}"]])
        self.rex.answers[("block", "ls")] = json.dumps({"blocks": [{"block_id": "block:m", "label": "manager: shop", "flavor": "com.superlogical.terminal.shell", "window_id": W}]})
        self.assertEqual(fc_rex.manager_label("block:m"), "manager: shop")
        self.assertEqual(fc_rex.manager_panes(), {"block:m": "manager: shop"})
        fc_rex.set_manager_label("block:m", None)
        self.assertEqual(self.rex.argv("block", "rename")[-1], ["block", "rename", "block:m", "shell"])

    def test_focus_says_when_the_app_did_not_switch(self):
        # Rex.app takes client actions only with Remote Control on: the server's focus moves, the app keeps showing what it showed.
        self.assertIsNone(fc_rex.focus(space=S, tab=W, pane=B))
        self.rex.answers[("do",)] = ("", 1, "Error: Rex.app is not accepting actions. Turn on Remote Control in its Settings under Rex Server.")
        note = fc_rex.focus(space=S, tab=W, pane=B)
        self.assertIn("Remote Control", note)
        self.assertIn("the session 'shop'", note)

    def test_the_label_keeps_what_it_is_not_told_now(self):
        fc_rex.pane_label(B, state="attempt 2/3")
        self.assertEqual(self.rex.argv("block", "rename")[-1], ["block", "rename", B, "shop-001 · 001 Parse · attempt 2/3"])
        self.rex.answers[("block", "ls")] = json.dumps({"blocks": [{"block_id": B, "label": "shop-001 · 001 Parse · attempt 2/3", "flavor": "com.superlogical.terminal.shell", "window_id": W}]})
        fc_rex.pane_label(B, tokens={"cost": "$0.01", "model": "glm"})
        self.assertEqual(self.rex.argv("block", "rename")[-1], ["block", "rename", B, "shop-001 · 001 Parse · attempt 2/3 · cost=$0.01 model=glm"])
        fc_rex.pane_label(B, title="001 Parse the node", state="")
        self.assertEqual(self.rex.argv("block", "rename")[-1], ["block", "rename", B, "shop-001 · 001 Parse the node"])

        fc_rex.pane_label(B, title="shop-001: 001 Parse")  # crew start titles a herdr pane `<name>: <title>`; the label has the name already
        self.assertEqual(self.rex.argv("block", "rename")[-1], ["block", "rename", B, "shop-001 · 001 Parse · attempt 2/3"])


class ParallelStarts(unittest.TestCase):
    def test_tasks_starting_at_once_share_one_implementers_window(self):
        # `run` starts its tasks on threads: each looked for the implementers window before any had made it, so each made one.
        import threading, time as _time, fc_fleet
        state, lock = {"windows": [], "blocks": []}, threading.Lock()
        calls = []
        def run(argv, **kw):
            args = argv[1:]
            calls.append(args)
            with lock:
                if args[:2] == ["window", "ls"]:
                    out = {"windows": list(state["windows"])}
                elif args[:2] == ["block", "ls"]:
                    out = {"blocks": list(state["blocks"])}
                elif args[:2] == ["window", "new"]:
                    n = len(state["windows"])
                    state["windows"].append({"window_id": f"window:{n}", "label": args[4]})
                    state["blocks"].append({"block_id": f"block:w{n}", "label": "shell", "flavor": "com.superlogical.terminal.shell", "window_id": f"window:{n}"})
                    out = {"window_id": f"window:{n}", "block_ids": [f"block:w{n}"]}
                elif args[0] == "split":
                    n = len(state["blocks"])
                    state["blocks"].append({"block_id": f"block:s{n}", "label": "x", "flavor": "com.superlogical.terminal.shell", "window_id": "window:0"})
                    out = {"block_ids": [f"block:s{n}"]}
                else:
                    out = {}
            if args[:2] == ["window", "ls"]:
                _time.sleep(0.05)  # the gap between looking and making
            return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")
        with mock.patch.object(fc_rex.subprocess, "run", run), mock.patch.object(fc_host, "rex_bin", return_value="/bin/rex"), \
                mock.patch.dict(os.environ, {"COMPOSE_HOST": "rex", "HERDR_ENV": ""}), mock.patch.object(fc_rex, "wait"), \
                mock.patch.object(fc_fleet, "agent", return_value=None):
            ts = [threading.Thread(target=fc_fleet.start_implementer, args=("/p/shop", S, "/w", {}, f"shop-00{i}", f"00{i} T", [], "m")) for i in (1, 2, 3)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(len([c for c in calls if c[:2] == ["window", "new"]]), 1)
        self.assertEqual(len([c for c in calls if c[0] == "split"]), 2)


if __name__ == "__main__":
    unittest.main()
