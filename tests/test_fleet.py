import hermetic  # first: no test sees the real home
import contextlib, io, json, os, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from tests.test_trail_setup import fc
import fc_fleet, fc_host, fc_hc, fc_rex, fc_herdr, fc_core, fc_lead, fc_crew  # noqa: E402
os.environ["HERDR_ENV"] = ""

HC_ENV = {"COMPOSE_HOST": "hc", "HC_TERMINAL_ID": "t1", "HC_TERMINAL_SOCKET": "/s/default.sock", "COMPOSE_ROLE": ""}


class InHc(unittest.TestCase):
    """Every verb reaches the Herdr app's backend when the session runs there, with compose's own records and names."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"
        self.proj.mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, HC_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        fc_fleet.SPACES.pop("shop", None)
        self.addCleanup(fc_fleet.SPACES.pop, "shop", None)

    def test_the_kind_its_name_and_the_pane_word(self):
        self.assertEqual((fc_fleet.kind(), fc_fleet.inside(), fc_fleet.host_name(), fc_fleet.pane_word(), fc_fleet.session_var()), ("hc", True, "the Herdr app", "the Herdr app pane", "HC_TERMINAL_ID"))
        with mock.patch.object(fc_herdr, "herdr_env", return_value=True):  # a test standing in for herdr wins
            self.assertEqual(fc_fleet.kind(), "herdr")
        with mock.patch.dict(os.environ, {"COMPOSE_HOST": "", "HC_TERMINAL_ID": ""}):
            self.assertEqual((fc_fleet.kind(), fc_fleet.inside(), fc_fleet.pane_word(), fc_fleet.session_var()), (None, False, "pane", "HERDR_PANE_ID"))

    def test_agents_and_one_agent_go_to_the_backend(self):
        listed = [{"name": "shop-001", "pane_id": "t3", "workspace_id": "sp1", "tab_id": "tb1", "agent_status": "working"}]
        with mock.patch.object(fc_hc, "agents", return_value=listed), mock.patch.object(fc_hc, "agent", return_value=listed[0]):
            self.assertEqual((fc_fleet.agents(), fc_fleet.agent_list(), fc_fleet.live_agents(), fc_fleet.agent("shop-001"), fc_fleet.agent_status("shop-001")),
                             (listed, {"agents": listed}, listed, listed[0], "working"))
        with mock.patch.object(fc_hc, "agents", return_value=None):
            self.assertEqual((fc_fleet.agents(), fc_fleet.agent_list(), fc_fleet.live_agents()), (None, None, []))

    def test_a_prompt_is_logged_as_typed_by_compose_and_check_raises(self):
        with mock.patch.object(fc_hc, "prompt", return_value=({"turn": 1}, None)) as p, mock.patch.object(fc_hc, "agent", return_value={"pane_id": "t3"}), \
                mock.patch.object(fc_fleet.fc_compactlog, "typed") as typed:
            self.assertEqual(fc_fleet.prompt("shop-001", "go", wait=True, timeout=30), ({"turn": 1}, None))
        p.assert_called_once_with("shop-001", "go", wait=True, timeout=30)
        typed.assert_called_once_with("t3", "go", agent="shop-001")
        with mock.patch.object(fc_hc, "prompt", return_value=(None, "agent_blocked")), mock.patch.object(fc_fleet.fc_compactlog, "typed") as typed:
            self.assertEqual(fc_fleet.prompt("shop-001", "go"), (None, "agent_blocked"))
            with self.assertRaises(RuntimeError) as e:
                fc_fleet.prompt("shop-001", "go", check=True)
        self.assertEqual((str(e.exception), typed.called), ("agent prompt shop-001: agent_blocked", False))

    def test_keys_read_and_wait(self):
        with mock.patch.object(fc_hc, "send_keys", return_value=True) as k, mock.patch.object(fc_hc, "read", return_value="x") as r, mock.patch.object(fc_hc, "wait", return_value=None) as w:
            self.assertEqual((fc_fleet.send_keys("a", "esc"), fc_fleet.read("a", lines=5, source="visible"), fc_fleet.wait("a", timeout=9, until="blocked")), (True, "x", None))
        k.assert_called_once_with("a", "esc")
        r.assert_called_once_with("a", lines=5, source="visible", fmt="text")
        w.assert_called_once_with("a", timeout=9, until="blocked")

    def test_the_space_is_made_once_and_remembered(self):
        with mock.patch.object(fc_hc, "space", return_value="sp9") as s, mock.patch.object(fc_hc, "find_space", return_value=None):
            self.assertEqual((fc_fleet.space(self.proj), fc_fleet.space(self.proj), fc_fleet.project_space(self.proj)), ("sp9", "sp9", None))
        s.assert_called_once_with("shop", self.proj.resolve().parents[1])
        with mock.patch.object(fc_hc, "tabs", return_value={"shell": {"tab_id": "tb1", "label": "shell"}}):
            self.assertEqual(fc_fleet.project_tabs(self.proj, "sp9"), [{"tab_id": "tb1", "label": "shell"}])
            self.assertEqual(fc_fleet.tabs(None), {})

    def test_a_teammate_starts_as_the_program_in_a_tab_of_its_own_and_waits_for_its_first_idle(self):
        with mock.patch.object(fc_hc, "start", return_value={"tab": "tbN", "pane": "t9"}) as st, mock.patch.object(fc_hc, "wait") as w:
            made = fc_fleet.start_teammate(self.proj, "sp9", "architect", "/w/shop", {"COMPOSE_ROLE": "crew"}, "shop-architect", "claude", "claude", ["--model", "m"])
        self.assertEqual(made, {"tab": "tbN", "pane": "t9"})
        st.assert_called_once_with("sp9", "architect", "/w/shop", {"COMPOSE_ROLE": "crew"}, "shop-architect", ["claude", "--model", "m"], title="architect")
        w.assert_called_once_with("shop-architect", timeout=60)
        with mock.patch.object(fc_hc, "start", side_effect=RuntimeError("hc run: name_taken")):
            with self.assertRaises(fc_fleet.StartFailed) as e:
                fc_fleet.start_teammate(self.proj, "sp9", "architect", "/w", {}, "a", "claude", "claude", [])
        self.assertEqual((str(e.exception), e.exception.tab, e.exception.pane), ("hc run: name_taken", None, None))

    def test_an_implementer_replaces_its_old_agent_joins_the_implementers_tab_and_shows_its_model(self):
        with mock.patch.object(fc_hc, "agent", return_value={"pane_id": "t2"}), mock.patch.object(fc_hc, "close_pane") as cp, \
                mock.patch.object(fc_hc, "start", return_value={"tab": "tbI", "pane": "t9"}) as st, mock.patch.object(fc_hc, "wait"), mock.patch.object(fc_hc, "pane_label") as pl:
            made = fc_fleet.start_implementer(self.proj, "sp9", "/w/shop", {"K": "v"}, "shop-001", "001 Parse", ["--session-id", "sid"], "openrouter/z-ai/glm-5.3-flash@preset/fast")
        self.assertEqual(made, {"tab": "tbI", "pane": "t9", "sfile": None})
        cp.assert_called_once_with("t2")
        st.assert_called_once_with("sp9", "implementers", "/w/shop", {"K": "v"}, "shop-001", ["pi", "--session-id", "sid"], join=True, title="001 Parse")
        pl.assert_called_once_with("t9", tokens={"model": "fast"})  # the last path piece, as herdr's token shows it

    def test_the_pi_session_file_is_found_from_the_session_id_once_pi_wrote_it(self):
        sdir = Path(tempfile.mkdtemp())
        argv = ["--no-skills", "--session-id", "abc-123", "--session-dir", str(sdir), "--model", "m"]
        session = {"name": "shop-001", "sfile": None, "seen": 0}
        self.assertIsNone(fc_fleet.session_file(session, argv))
        (sdir / "2026-11-07T10-00-00-000Z_abc-123.jsonl").write_text("")
        self.assertEqual(fc_fleet.session_file(session, argv), str(sdir / "2026-11-07T10-00-00-000Z_abc-123.jsonl"))
        self.assertEqual(fc_fleet.session_file(session, []), str(sdir / "2026-11-07T10-00-00-000Z_abc-123.jsonl"))  # remembered

    def test_labels_states_and_the_verify_viewer(self):
        with mock.patch.object(fc_hc, "pane_label") as pl:
            fc_fleet.pane_label("t9", title="x", state_labels={"working": "attempt 2/3", "done": "attempt 2/3"}, tokens={"cost": "$0.01"}, clear_tokens=("load",))
            fc_fleet.label({"pane": "t9"}, "passed", 0.5, state="idle")
            fc_fleet.pane_label(None, title="nothing")
        self.assertEqual(pl.call_args_list, [mock.call("t9", title="x", state="attempt 2/3", tokens={"cost": "$0.01"}, clear=("load",)),
                                             mock.call("t9", title=None, state="passed", tokens={"cost": "$0.5000"}, clear=())])
        with mock.patch.object(fc_hc, "tabs", return_value={"verify 10:02": {"tab_id": "tbOld", "label": "verify 10:02"}, "shell": {"tab_id": "tb0", "label": "shell"}}), \
                mock.patch.object(fc_hc, "close_tab") as ct, mock.patch.object(fc_hc, "start", return_value={"tab": "tbV", "pane": "t7"}) as st:
            self.assertEqual(fc_fleet.verify_panes(self.proj, "sp9", Path("/w/shop"), ["checks", "review"]), {})
            self.assertEqual(fc_fleet.verify_panes(self.proj, None, Path("/w/shop"), ["checks"]), {})
        ct.assert_called_once_with("tbOld")
        st.assert_called_once_with("sp9", "verify", Path("/w/shop"), {}, "shop-verify", ["compose", "stages", "--repo", "/w/shop", "--project", "shop"], title="verify")

    def test_closing_focus_and_liveness(self):
        with mock.patch.object(fc_hc, "close_pane") as cp, mock.patch.object(fc_hc, "close_tab") as ct, mock.patch.object(fc_hc, "tab_gone", return_value=True) as tg, \
                mock.patch.object(fc_hc, "close_space") as cs, mock.patch.object(fc_hc, "space_is", return_value=True), mock.patch.object(fc_hc, "focus") as f, \
                mock.patch.object(fc_hc, "live", return_value=False), mock.patch.object(fc_hc, "agent", return_value={"pane_id": "t3"}):
            fc_fleet.close_pane("t3"); fc_fleet.close_pane(None); fc_fleet.close_tab("tb1"); fc_fleet.close_tab(None)
            self.assertTrue(fc_fleet.tab_gone("tb1")); self.assertFalse(fc_fleet.tab_gone(None))
            fc_fleet.close_space("sp1"); self.assertTrue(fc_fleet.space_is("sp1", "shop")); fc_fleet.focus(space="sp1", tab="tb1", pane="t3")
            self.assertFalse(fc_fleet.manager_live("t1"))
            fc_fleet.close_task(self.proj, "001")
        self.assertEqual((cp.call_args_list, ct.call_args_list, tg.call_args_list, cs.call_args_list, f.call_args_list),
                         ([mock.call("t3"), mock.call("t3")], [mock.call("tb1")], [mock.call("tb1")], [mock.call("sp1")], [mock.call(space="sp1", tab="tb1", pane="t3")]))

    def test_the_panes_processes_from_the_backends_pids(self):
        with mock.patch.object(fc_hc, "pane_pids", return_value=[41, 42]), mock.patch.object(fc_fleet.fc_proc, "nice_floor", side_effect=lambda pid: 0 if pid == 42 else None), \
                mock.patch.object(fc_hc, "agent", return_value=None), mock.patch.object(fc_hc, "agents", return_value=[]):
            self.assertEqual((fc_fleet.pane_processes("t9"), fc_fleet.agent_pids("t9"), fc_fleet.pane_launching("a", "t9")), ([{"pid": 41}, {"pid": 42}], [42], True))
        with mock.patch.object(fc_hc, "pane_pids", return_value=[]), mock.patch.object(fc_hc, "agent", return_value=None), mock.patch.object(fc_hc, "agents", return_value=[]):
            self.assertFalse(fc_fleet.pane_launching("a", "t9"))

    def test_the_managers_pane_is_labelled_through_the_backend(self):
        labels = {"t1": "manager: cart"}
        with mock.patch.object(fc_hc, "manager_label", side_effect=lambda p: labels.get(p, "")), mock.patch.object(fc_hc, "set_manager_label", side_effect=labels.__setitem__), \
                mock.patch.object(fc_hc, "manager_panes", side_effect=lambda: dict(labels)):
            fc_fleet.name_manager(self.proj)
            self.assertEqual(labels["t1"], "manager: cart, shop")
            fc_fleet.unname_manager("cart")
            self.assertEqual(labels["t1"], "manager: shop")
            fc_fleet.unname_manager("shop")
            self.assertEqual(labels["t1"], None)
            labels["t1"] = "my own label"
            fc_fleet.name_manager(self.proj)
            self.assertEqual(labels["t1"], "my own label")
        with mock.patch.dict(os.environ, {"COMPOSE_ROLE": "orchestrator"}), mock.patch.object(fc_hc, "manager_label") as ml:
            fc_fleet.name_manager(self.proj)
        ml.assert_not_called()

    def test_the_claim_refuses_a_live_owner_names_the_host_and_moves_with_takeover(self):
        (self.proj / "results" / "crew").mkdir(parents=True)
        (self.proj / "results" / "crew" / "shop-x.json").write_text(json.dumps({"name": "shop-x", "started_by": "t5"}))
        (self.proj / "results" / "manager.json").write_text(json.dumps({"session": "t5", "since": "10:00"}))
        with mock.patch.object(fc_hc, "live", return_value=True):
            with self.assertRaises(SystemExit) as e:
                fc_fleet.manager_claim(self.proj)
            self.assertIn("another engineering manager session (the Herdr app pane t5", str(e.exception))
            self.assertEqual(fc_fleet.manager_claim(self.proj, takeover=True), {"from": "t5"})
        self.assertEqual(json.loads((self.proj / "results" / "crew" / "shop-x.json").read_text())["started_by"], "t1")
        self.assertEqual(json.loads((self.proj / "results" / "manager.json").read_text())["session"], "t1")
        self.assertIsNone(fc_fleet.manager_claim(self.proj))  # already the owner


class InHerdr(unittest.TestCase):
    """The herdr path is fc_herdr's own CLI calls, so a test that stands in for `herdr` sees what it always saw."""
    def test_a_prompt_with_check_names_herdrs_code_and_without_it_a_refusal(self):
        def herdr(*a, check=False, **kw):
            if check:
                raise RuntimeError("herdr agent prompt: agent_blocked")
            return None
        with mock.patch.object(fc_herdr, "herdr", herdr):
            self.assertEqual(fc_fleet.prompt("a", "hi"), (None, "refused"))
            with self.assertRaises(RuntimeError) as e:
                fc_fleet.prompt("a", "hi", check=True)
        self.assertEqual(str(e.exception), "agent prompt a: agent_blocked")
        with mock.patch.object(fc_herdr, "herdr", lambda *a, **k: {"ok": 1}):
            self.assertEqual(fc_fleet.prompt("a", "hi"), ({"ok": 1}, None))
        with mock.patch.object(fc_herdr, "herdr_error", return_value=({}, "timeout")) as he:
            self.assertEqual(fc_fleet.prompt("a", "hi", wait=True, timeout=10), ({}, "timeout"))
        self.assertEqual(he.call_args.args, ("agent", "prompt", "a", "hi", "--wait", "--timeout", "10000"))

    def test_reads_labels_and_the_agent_record_are_herdrs_argv(self):
        calls = []
        with mock.patch.object(fc_herdr, "herdr_text", lambda *a, **k: calls.append(a) or "rows"), mock.patch.object(fc_herdr, "herdr", lambda *a, **k: calls.append(a) or {"agent": {"agent_status": "idle", "pane_id": "w1:p3"}}):
            self.assertEqual(fc_fleet.read("a", lines=12, source="visible", fmt="ansi"), "rows")
            fc_fleet.pane_label("w1:p3", title="t", state_labels={"working": "x", "done": "x"}, tokens={"cost": "$1"}, clear_tokens=("load",), source="compose-load")
            self.assertEqual(fc_fleet.agent("a"), {"agent_status": "idle", "pane_id": "w1:p3"})
            fc_fleet.close_task(Path("/x/.compose/shop"), "003")
        self.assertEqual(calls[:2], [("agent", "read", "a", "--source", "visible", "--format", "ansi", "--lines", "12"),
                                     ("pane", "report-metadata", "w1:p3", "--source", "compose-load", "--title", "t", "--state-label", "working=x", "--state-label", "done=x", "--token", "cost=$1", "--clear-token", "load")])
        self.assertEqual(calls[-1], ("pane", "close", "w1:p3"))
        with mock.patch.object(fc_herdr, "herdr", lambda *a, **k: {"agent_status": "done"}):
            self.assertIsNone(fc_fleet.agent("a"))  # only the `agent` record counts

    def test_the_space_cache_is_herdrs_and_attach_takes_the_first_host_installed(self):
        self.assertIs(fc_fleet.SPACES, fc_herdr.HERDR_SPACES)
        tty = mock.patch.multiple(fc_fleet.sys, stdin=SimpleNamespace(isatty=lambda: True), stdout=SimpleNamespace(isatty=lambda: True))
        env = mock.patch.dict(os.environ, {"HERDR_ENV": "", "HC_TERMINAL_ID": "", "REX_SESSION": "", "CI": "", "COMPOSE_NO_HERDR": ""})
        for have, target in ((["hc", "herdr", "rex"], (fc_hc, "attach")), (["herdr", "rex"], (fc_herdr, "herdr_attach")), (["rex"], (fc_rex, "attach"))):
            with tty, env, mock.patch.object(fc_host, "installed", return_value=have), mock.patch.object(*target) as at:
                fc_fleet.attach()
            at.assert_called_once_with()
        with tty, env, mock.patch.object(fc_host, "installed", return_value=[]):
            with self.assertRaises(SystemExit) as e:
                fc_fleet.attach()
        self.assertIn("herdr is not installed: curl -fsSL https://herdr.dev/install.sh | sh", str(e.exception))
        self.assertIn("or run compose setup", str(e.exception))
        with tty, mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t1"}), mock.patch.object(fc_host, "installed") as inst:
            fc_fleet.attach()  # inside a host already: nothing to open
        inst.assert_not_called()


class CrewPromptAndRead(unittest.TestCase):
    """`compose crew prompt` and `crew read`: one command for every host, so no session needs the host's own CLI."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"
        (self.proj / "results" / "crew").mkdir(parents=True)
        (self.proj / "results" / "crew" / "shop-orchestrator.json").write_text(json.dumps({"name": "shop-orchestrator", "agent": "shop-orchestrator-2", "role": "orchestrator"}))

    def crew(self, **kw):
        a = SimpleNamespace(**{"repo": ".", "project": "shop", "role": None, "agent": None, "text": None, "lines": None, **kw})
        out = io.StringIO()
        with mock.patch.object(fc_core, "project_dir", return_value=self.proj), contextlib.redirect_stdout(out):
            try:
                fc_lead.cmd_crew(a)
            except SystemExit as e:
                return out.getvalue(), str(e)
        return out.getvalue(), None

    def test_a_prompt_by_role_goes_to_the_registered_agent_and_by_agent_to_any(self):
        with mock.patch.object(fc_fleet, "prompt", return_value=({}, None)) as p:
            self.assertEqual(self.crew(action="prompt", role="orchestrator", text="carry on"), ("prompted shop-orchestrator-2\n", None))
            self.assertEqual(self.crew(action="prompt", agent="shop-001", text="stop"), ("prompted shop-001\n", None))
            self.assertEqual(self.crew(action="prompt", role="planner", text="x"), ("prompted shop-planner\n", None))  # no record: the name the role gives
        self.assertEqual([c.args for c in p.call_args_list], [("shop-orchestrator-2", "carry on"), ("shop-001", "stop"), ("shop-planner", "x")])

    def test_a_refusal_and_a_missing_target_or_text_exit_with_the_reason(self):
        with mock.patch.object(fc_fleet, "prompt", return_value=(None, "agent_blocked")):
            self.assertIn("did not take the prompt: agent_blocked (it waits at a dialog", self.crew(action="prompt", role="orchestrator", text="go")[1])
        self.assertIn("usage: compose crew prompt --project shop (--role <role> | --agent <name>)", self.crew(action="prompt", text="go")[1])
        self.assertIn("needs the text", self.crew(action="prompt", role="orchestrator")[1])
        with mock.patch("sys.stdin", io.StringIO("from stdin")), mock.patch.object(fc_fleet, "prompt", return_value=({}, None)) as p:
            self.crew(action="prompt", role="orchestrator", text="-")
        self.assertEqual(p.call_args.args, ("shop-orchestrator-2", "from stdin"))

    def test_keys_press_in_the_pane_and_need_at_least_one(self):
        with mock.patch.object(fc_fleet, "send_keys", return_value=True) as k:
            self.assertEqual(self.crew(action="keys", role="orchestrator", text="esc enter"), ("pressed esc enter in shop-orchestrator-2\n", None))
        k.assert_called_once_with("shop-orchestrator-2", "esc", "enter")
        self.assertIn("needs the keys", self.crew(action="keys", role="orchestrator")[1])
        with mock.patch.object(fc_fleet, "send_keys", return_value=False):
            self.assertIn("did not take the keys", self.crew(action="keys", agent="shop-001", text="esc")[1])

    def test_read_prints_the_pane_and_a_gone_agent_exits(self):
        with mock.patch.object(fc_fleet, "read", return_value="the screen") as r:
            self.assertEqual(self.crew(action="read", agent="shop-001", lines=20), ("the screen\n", None))
        r.assert_called_once_with("shop-001", lines=20)
        with mock.patch.object(fc_fleet, "read", return_value=""), mock.patch.object(fc_fleet, "agent", return_value=None):
            self.assertEqual(self.crew(action="read", role="orchestrator")[1], "no live agent shop-orchestrator-2")


if __name__ == "__main__":
    unittest.main()
