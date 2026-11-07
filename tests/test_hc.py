import hermetic  # first: no test sees the real home
import io, json, os, unittest
from types import SimpleNamespace
from unittest import mock
from tests.test_trail_setup import fc
import fc_hc, fc_host  # noqa: E402
os.environ["HERDR_ENV"] = ""

SPACE, TAB = "sp0123", "tb4567"
APP_LS = {"spaces": [{"id": SPACE, "name": "shop", "folder": "/w/shop", "tabs": [
    {"id": "tb0001", "name": "shell", "terminals": [{"terminal": "t1", "pane": "p1", "status": None, "app": None, "name": None}]},
    {"id": TAB, "name": "implementers", "terminals": [{"terminal": "t3", "pane": "p3", "status": "working", "app": "pi", "name": "shop-001"}]}]}], "recent": []}


class Host:
    """A fake `subprocess.run` for hc and herdr-app: `answers` maps an argv prefix to (stdout, returncode, stderr); the calls are kept."""
    def __init__(self, **answers):
        self.calls, self.answers = [], {}
        for key, value in answers.items():
            self.answers[tuple(key.split("__"))] = value

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        for key, value in self.answers.items():
            if tuple(argv[:len(key)]) == key:
                out, code, err = (value, 0, "") if isinstance(value, str) else value
                return SimpleNamespace(returncode=code, stdout=out, stderr=err)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    def argv(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def run_with(host):
    return mock.patch.object(fc_hc.subprocess, "run", host)


class Calls(unittest.TestCase):
    def test_json_goes_right_after_the_subcommand_never_to_the_program(self):
        host = Host(hc__run='{"address": "t9"}')
        with run_with(host):
            self.assertEqual(fc_hc.hc_err("run", "--bg", "--", "cat"), ({"address": "t9"}, None))
        self.assertEqual(host.calls, [["hc", "run", "--json", "--bg", "--", "cat"]])

    def test_an_error_is_its_kind_and_read_is_text(self):
        host = Host(hc__send=('', 9, '{"error": {"kind": "blocked", "message": "t3 is blocked"}}'), hc__read="line one\nline two\n")
        with run_with(host):
            self.assertEqual(fc_hc.hc_err("send", "t3", "x"), (None, "blocked"))
            self.assertEqual(fc_hc.hc_err("read", "t3", "--lines", "0", text=True), ("line one\nline two\n", None))
        self.assertEqual(host.calls[1], ["hc", "read", "t3", "--lines", "0"])

    def test_the_window_prints_a_pretty_document_and_an_error_is_none(self):
        host = Host(**{"herdr-app__ls": json.dumps(APP_LS, indent=2), "herdr-app__do__space.close": ('{\n  "error": "no space"\n}', 1, "")})
        with run_with(host):
            self.assertEqual([s["name"] for s in fc_hc.app_ls()], ["shop"])
            self.assertIsNone(fc_hc.app("space.close", id="x", confirmed=True))
        self.assertEqual(host.calls[1], ["herdr-app", "do", "space.close", "--id", "x", "--confirmed", "true"])

    def test_a_closed_window_answers_none(self):
        with run_with(Host(**{"herdr-app__ls": ("", 1, "no window")})):
            self.assertIsNone(fc_hc.app_ls())


class Agents(unittest.TestCase):
    LS = json.dumps({"terminals": [{"address": "t3", "agent": "pi", "name": "shop-001", "state": "live", "status": "working"},
                                   {"address": "t1", "agent": "zsh", "name": None, "state": "live", "status": None},
                                   {"address": "t5", "agent": "claude-code", "name": "shop-orchestrator", "state": "live", "status": "error"}]})

    def test_named_terminals_are_agents_placed_by_the_window(self):
        with run_with(Host(hc__ls=self.LS, **{"herdr-app__ls": json.dumps(APP_LS)})):
            got = fc_hc.agents()
        self.assertEqual(got, [{"name": "shop-001", "pane_id": "t3", "workspace_id": SPACE, "tab_id": TAB, "agent_status": "working"},
                               {"name": "shop-orchestrator", "pane_id": "t5", "workspace_id": None, "tab_id": None, "agent_status": "blocked"}])

    def test_hc_not_answering_is_none_not_nobody(self):
        with run_with(Host(hc__ls=("", 1, '{"error": {"kind": "unreachable"}}'))):
            self.assertIsNone(fc_hc.agents())

    def test_one_agent_with_its_pids_cwd_and_labels(self):
        get = json.dumps({"address": "t3", "name": "shop-001", "state": "live", "status": {"app": "pi", "state": "idle"}, "cwd": "/w/shop",
                          "foreground": {"pid": 412, "cwd": "/w/shop/sub"}, "labels": {"herdr.space": SPACE, "herdr.tab": TAB, "compose.state": "attempt 1/3"}})
        with run_with(Host(hc__get=get)):
            self.assertEqual(fc_hc.agent("shop-001"), {"name": "shop-001", "pane_id": "t3", "workspace_id": SPACE, "tab_id": TAB, "agent_status": "idle",
                                                       "pids": [412], "cwd": "/w/shop/sub", "labels": {"herdr.space": SPACE, "herdr.tab": TAB, "compose.state": "attempt 1/3"}})
        with run_with(Host(hc__get=json.dumps({"address": "t3", "state": "finished"}))):
            self.assertIsNone(fc_hc.agent("shop-001"))


class Prompt(unittest.TestCase):
    def test_errors_in_composes_words(self):
        for kind, want in (("blocked", "agent_blocked"), ("not_found", "agent_not_found"), ("finished", "finished"), ("spawn_failed", "spawn_failed")):
            with run_with(Host(hc__send=("", 1, json.dumps({"error": {"kind": kind}})))):
                self.assertEqual(fc_hc.prompt("a", "hi"), (None, want), kind)

    def test_a_wait_that_checks_in_is_a_timeout_and_one_that_never_started_is_a_stall(self):
        host = Host(hc__send=json.dumps({"started": True, "status": "idle", "turn": 2, "wait": {"reason": "timeout"}}))
        with run_with(host):
            self.assertEqual(fc_hc.prompt("a", "hi", wait=True, timeout=30)[1], "timeout")
        self.assertEqual(host.calls[0], ["hc", "send", "--json", "a", "hi", "--wait", "--timeout", "30"])
        with run_with(Host(hc__send=json.dumps({"reason": "not_started"}))):
            self.assertEqual(fc_hc.prompt("a", "hi", wait=True)[1], "agent_prompt_stalled")
        with run_with(Host(hc__send=json.dumps({"started": True, "status": "idle"}))):
            self.assertEqual(fc_hc.prompt("a", "hi", wait=True)[1], None)

    def test_keys_read_and_wait(self):
        host = Host(hc__read="screen\n", hc__wait=json.dumps({"reason": "status", "status": "idle"}))
        with run_with(host):
            self.assertTrue(fc_hc.send_keys("a", "esc"))
            self.assertEqual(fc_hc.read("a", lines=12, source="visible"), "screen\n")
            self.assertEqual(fc_hc.read("a", lines=12), "screen\n")
            self.assertIsNone(fc_hc.wait("a", timeout=20))
        self.assertEqual(host.calls, [["hc", "keys", "--json", "a", "esc"], ["hc", "read", "a", "--lines", "0"], ["hc", "read", "a", "--lines", "12"], ["hc", "wait", "--json", "a", "--timeout", "20"]])

    def test_a_wait_for_a_status_polls_the_terminal(self):
        states = iter(["working", "working", "blocked"])
        host = Host()
        host.answers[("hc", "get")] = None
        host.__class__ = type("H", (Host,), {"__call__": lambda self, argv, **kw: (self.calls.append(argv) or SimpleNamespace(returncode=0, stderr="",
            stdout=json.dumps({"address": "t1", "state": "live", "status": {"state": next(states)}})))})
        with run_with(host), mock.patch.object(fc_hc.time, "sleep"):
            self.assertIsNone(fc_hc.wait("a", timeout=30, until="blocked"))
        self.assertEqual(len(host.calls), 3)


class Start(unittest.TestCase):
    def start(self, join, placed=True, **answers):
        ls = json.dumps(APP_LS) if placed else json.dumps({"spaces": [{"id": SPACE, "name": "shop", "folder": "/w/shop", "tabs": []}]})
        host = Host(hc__run='{"address": "t9"}', **{"herdr-app__ls": ls, **answers})
        with run_with(host), mock.patch.object(fc_host, "pane_env", return_value={"HOME": "/s/home"}), mock.patch.object(fc_hc.time, "sleep"):
            made = fc_hc.start(SPACE, "implementers" if join else "architect", "/w/shop", {"COMPOSE_ROLE": "crew"}, "shop-001", ["pi", "--model", "m"], join=join, title="001 Parse")
        return host, made

    def test_a_tab_of_its_own_is_named_after_the_start_and_the_terminal_learns_its_tab(self):
        placed = json.dumps({"spaces": [{"id": SPACE, "name": "shop", "folder": "/w/shop", "tabs": [{"id": "tbNEW", "name": "3", "terminals": [{"terminal": "t9", "name": "shop-001"}]}]}]})
        host, made = self.start(join=False, **{"herdr-app__ls": placed})
        self.assertEqual(made, {"tab": "tbNEW", "pane": "t9"})
        self.assertEqual(host.argv("hc", "run")[0], ["hc", "run", "--json", "--bg", "--no-parent", "--no-notify", "--name", "shop-001", "--cwd", "/w/shop",
                                                      "--label", f"herdr.space={SPACE}", "--label", "compose.tab=architect", "--label", "compose.title=001 Parse",
                                                      "--", "env", "HOME=/s/home", "COMPOSE_ROLE=crew", "pi", "--model", "m"])
        self.assertIn(["herdr-app", "do", "tab.rename", "--id", "tbNEW", "--name", "architect"], host.calls)
        self.assertIn(["hc", "label", "--json", "t9", "herdr.tab=tbNEW"], host.calls)

    def test_joining_the_implementers_tab_passes_its_id_and_renames_nothing(self):
        placed = json.loads(json.dumps(APP_LS))
        placed["spaces"][0]["tabs"][1]["terminals"].append({"terminal": "t9", "name": "shop-002"})
        host, made = self.start(join=True, **{"herdr-app__ls": json.dumps(placed)})
        self.assertEqual(made, {"tab": TAB, "pane": "t9"})
        run = host.argv("hc", "run")[0]
        self.assertEqual(run[run.index("--label") : run.index("--")], ["--label", f"herdr.space={SPACE}", "--label", f"herdr.tab={TAB}", "--label", "compose.tab=implementers", "--label", "compose.title=001 Parse"])
        self.assertEqual(host.argv("herdr-app", "do", "tab.rename"), [])

    def test_without_a_window_the_terminal_runs_with_no_tab(self):
        host = Host(hc__run='{"address": "t9"}', **{"herdr-app__ls": ("", 1, "")})
        with run_with(host), mock.patch.object(fc_host, "pane_env", return_value={}), mock.patch.object(fc_hc.time, "sleep"):
            self.assertEqual(fc_hc.start(None, "architect", "/w", {}, "shop-a", ["claude"]), {"tab": None, "pane": "t9"})
        self.assertNotIn("--label", host.argv("hc", "run")[0][:8])

    def test_a_refusal_raises(self):
        with run_with(Host(hc__run=("", 1, '{"error": {"kind": "name_taken"}}'))), mock.patch.object(fc_host, "pane_env", return_value={}):
            with self.assertRaises(RuntimeError) as e:
                fc_hc.start(SPACE, "x", "/w", {}, "a", ["cat"])
        self.assertIn("name_taken", str(e.exception))


class Spaces(unittest.TestCase):
    def test_found_by_name_then_by_folder_then_made_with_a_shell_tab(self):
        with run_with(Host(**{"herdr-app__ls": json.dumps(APP_LS)})):
            self.assertEqual(fc_hc.space("shop", "/elsewhere"), SPACE)
            self.assertEqual(fc_hc.space("other", "/w/shop"), SPACE)
        host = Host(**{"herdr-app__ls": json.dumps({"spaces": []}), "herdr-app__do__space.new": json.dumps({"space": "spNEW", "tab": "tbNEW", "terminal": "t2"})})
        with run_with(host):
            self.assertEqual(fc_hc.space("cart", "/w/cart"), "spNEW")
        self.assertEqual(host.calls[1:], [["herdr-app", "do", "space.new", "--name", "cart", "--folder", "/w/cart"], ["herdr-app", "do", "tab.rename", "--id", "tbNEW", "--name", "shell"]])
        with run_with(Host(**{"herdr-app__ls": ("", 1, "")})):
            self.assertIsNone(fc_hc.space("cart", "/w/cart"))

    def test_tabs_gone_tabs_and_closing(self):
        host = Host(**{"herdr-app__ls": json.dumps(APP_LS)})
        with run_with(host):
            self.assertEqual(fc_hc.tabs(SPACE), {"shell": {"tab_id": "tb0001", "label": "shell"}, "implementers": {"tab_id": TAB, "label": "implementers"}})
            self.assertFalse(fc_hc.tab_gone(TAB))
            self.assertTrue(fc_hc.tab_gone("tbGONE"))
            self.assertTrue(fc_hc.space_is(SPACE, "shop"))
            fc_hc.close_pane("t3")
            fc_hc.close_tab(TAB)
            fc_hc.close_space(SPACE)
        self.assertEqual(host.calls[-4:], [["hc", "kill", "--json", "t3"], ["hc", "rm", "--json", "t3"], ["herdr-app", "do", "tab.close", "--id", TAB, "--confirmed", "true"],
                                           ["herdr-app", "do", "space.close", "--id", SPACE, "--confirmed", "true"]])
        with run_with(Host(**{"herdr-app__ls": ("", 1, "")})):
            self.assertFalse(fc_hc.tab_gone(TAB))  # a closed window cannot say

    def test_labels_focus_and_the_managers_label(self):
        host = Host(hc__get=json.dumps({"address": "t1", "state": "live", "labels": {"compose.manager": "manager: shop"}}),
                    hc__ls=json.dumps({"terminals": [{"address": "t1", "name": None, "state": "live"}]}))
        with run_with(host):
            fc_hc.pane_label("t3", title="001 Parse", state="attempt 2/3", tokens={"cost": "$0.01"}, clear=("load",))
            fc_hc.focus(pane="t3")
            fc_hc.focus(tab=TAB)
            self.assertEqual(fc_hc.manager_label("t1"), "manager: shop")
            fc_hc.set_manager_label("t1", None)
            self.assertEqual(fc_hc.manager_panes(), {"t1": "manager: shop"})
            self.assertTrue(fc_hc.live("t1"))
        self.assertEqual(host.calls[:3], [["hc", "label", "--json", "t3", "compose.title=001 Parse", "compose.state=attempt 2/3", "compose.cost=$0.01", "compose.load="],
                                          ["herdr-app", "do", "agent.focus", "--terminal", "t3"], ["herdr-app", "do", "tab.select", "--id", TAB]])
        self.assertIn(["hc", "label", "--json", "t1", "compose.manager="], host.calls)

    def test_attach_opens_the_app_and_says_what_to_do(self):
        host = Host()
        with run_with(host), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            fc_hc.attach()
        self.assertIn("open Herdr, run claude in a tab and type /compose", out.getvalue())


if __name__ == "__main__":
    unittest.main()
