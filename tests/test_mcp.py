import hermetic  # first: no test sees the real home
import json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import fc_mcp, fc_figmaauth, fc_config  # noqa: E402

P = ["claude_ai_Claude_Docs", "claude_ai_Google_Calendar", "claude_ai_Linear", "claude_ai_Notion", "frame0", "framelink-figma", "graft", "plugin_product-management_figma", "trail"]
LIST = """Checking MCP server health…

claude.ai Linear: https://mcp.linear.app/mcp (HTTP) - ✔ Connected
plugin:product-management:google calendar: https://x - Not configured
frame0: npx -y frame0-mcp-server - ✗ Failed to connect: spawn npx ENOENT
graft: graft mcp - ✔ Connected

MCP config diagnostics ⚠

[Conflicting scopes]
├ Server "frame0" is defined in multiple scopes: user (npx), local (npx --api-port=1).
"""


def pat(*names):
    return {"mcp__%s__*" % n for n in names}


class RoleMcp(unittest.TestCase):
    def test_installed_servers_reads_list_in_cwd(self):
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, LIST, "")) as run:
            got = fc_mcp.installed_servers("/some/worktree")
        self.assertEqual(got, ["claude_ai_Linear", "plugin_product-management_google_calendar", "frame0", "graft"])
        self.assertEqual(list(run.call_args.args[0]), ["claude", "mcp", "list"])
        self.assertEqual(str(run.call_args.kwargs["cwd"]), "/some/worktree")

    def test_installed_servers_empty_on_failure(self):
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, LIST, "")):
            self.assertEqual(fc_mcp.installed_servers("/w"), [])
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("claude")):
            self.assertEqual(fc_mcp.installed_servers("/w"), [])

    def test_disallowed(self):
        self.assertEqual(set(fc_mcp.disallowed("planner", P)), pat(*P))
        for r, s in [(r, s) for r in ("designer", "design-review", "design-003", "figma") for s in (P, P + ["claude_ai_Figma"])]:
            self.assertEqual(set(fc_mcp.disallowed(r, s)), pat(*P) - pat("framelink-figma"))
        for r in ("designer", "design-review", "design-003", "figma"):  # the Figma plugin's connector counts as the Figma connector; another plugin's Figma server does not
            self.assertEqual(set(fc_mcp.disallowed(r, P + ["plugin_figma_figma"])), pat(*P) - pat("framelink-figma"))
        for r in ("toolsmith", "implementer", "no-such-role", "orchestrator", "architect", "designer", "design-003"):  # graft, trail, and Linear are denied for every role
            self.assertLessEqual(pat("graft", "trail", "claude_ai_Linear"), set(fc_mcp.disallowed(r, P)), r)
        self.assertEqual(set(fc_mcp.disallowed("toolsmith", P)), pat(*P))
        self.assertIn("mcp__frame0__*", fc_mcp.disallowed("architect", P))  # a `frame0` server someone registered is denied like any other: compose frame0 is the way in
        self.assertEqual(fc_mcp.disallowed("planner", ["graft"]), ["mcp__graft__*"])  # nothing is denied that is not installed
        self.assertEqual(set(fc_mcp.disallowed("designer", ["LINEAR_x", "Graft2"])), pat("LINEAR_x", "Graft2"))

    def test_no_mcpjson_approval_is_left(self):
        self.assertFalse(hasattr(fc_mcp, "wired_mcpjson"))
        self.assertFalse(hasattr(fc_mcp, "GRAFT_MCPJSON"))


NEEDS = """Checking MCP server health…

claude.ai Linear: https://mcp.linear.app/mcp - ✔ Connected
plugin:figma:figma: https://mcp.figma.com/mcp (HTTP) - ! Needs authentication
framelink-figma: npx -y figma-developer-mcp --stdio - ✔ Connected
"""
PLUGIN_LINE = "plugin:figma:figma: https://mcp.figma.com/mcp (HTTP) - ! Needs authentication\n"
SIGNED_IN = NEEDS.replace("! Needs authentication", "✔ Connected")
CONNECTOR = NEEDS.replace(PLUGIN_LINE, "claude.ai Figma: https://mcp.figma.com/mcp - ✔ Connected\n")
NO_PLUGIN = NEEDS.replace(PLUGIN_LINE, "")


class FigmaSignIn(unittest.TestCase):
    """#493: a design builder whose Figma plugin server lost its sign-in has no write tool; it starts on the claude.ai connector or not at all."""

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp()) / "model-probe.json"
        for p in (mock.patch.object(fc_config, "PROBE_CACHE", self.cache), mock.patch.object(fc_figmaauth, "_ROUTE", {})):
            p.start()
            self.addCleanup(p.stop)

    def lists(self, plain, denied=None):
        """`claude mcp list` stubbed: `plain` as is, `denied` under `--settings` (the plugin server denied); records every call."""
        self.calls = []
        def run(cmd, **kw):
            self.calls.append(list(cmd))
            out = denied if "--settings" in cmd and denied is not None else plain
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return mock.patch("subprocess.run", side_effect=run)

    def test_states_say_which_servers_need_a_sign_in(self):
        lines = [l for l in NEEDS.splitlines() if ": " in l]
        self.assertEqual(fc_mcp.server_states(lines), {"claude.ai Linear": "connected", "plugin:figma:figma": "needs authentication", "framelink-figma": "connected"})

    def test_only_the_roles_that_write_figma_are_checked(self):
        for role, want in (("design-591", True), ("designer", True), ("design-review", False), ("figma", False), ("implementer", False), ("planner", False)):
            self.assertEqual(fc_mcp.writes_figma(role), want, role)

    def test_a_signed_in_plugin_server_starts_as_before_and_the_pass_is_cached(self):
        with self.lists(SIGNED_IN):
            self.assertEqual(fc_figmaauth.check("design-591", "/w"), ([], None))
            self.assertEqual(len(self.calls), 1)
            fc_figmaauth._ROUTE.clear()  # another process: the file answers
            self.assertEqual(fc_figmaauth.check("design-592", "/w"), ([], None))
        self.assertEqual(len(self.calls), 1)

    def test_a_signed_out_plugin_with_the_connector_signed_in_denies_the_plugin_server(self):
        with self.lists(NEEDS, denied=CONNECTOR):
            self.assertEqual(fc_figmaauth.check("design-591", "/w"), (["plugin:figma:figma"], None))
        self.assertIn("deniedMcpServers", self.calls[1][self.calls[1].index("--settings") + 1])
        self.assertEqual(self.calls[1][-2:], ["mcp", "list"])
        with self.lists(NEEDS, denied=CONNECTOR):  # the fallback is cached too, with its denial
            fc_figmaauth._ROUTE.clear()
            self.assertEqual(fc_figmaauth.check("design-591", "/w"), (["plugin:figma:figma"], None))
        self.assertEqual(self.calls, [])

    def test_a_signed_out_plugin_and_no_connector_refuses_with_the_way_to_sign_in_and_caches_nothing(self):
        with self.lists(NEEDS, denied=NO_PLUGIN):
            deny, why = fc_figmaauth.check("design-591", "/w")
        self.assertEqual(deny, [])
        self.assertIn("plugin:figma:figma needs a sign-in", why)
        self.assertIn("claude mcp list", why)
        self.assertIn("/mcp", why)
        self.assertFalse(self.cache.exists())
        with self.lists(SIGNED_IN):  # signed in since: the next start goes ahead
            fc_figmaauth._ROUTE.clear()
            self.assertEqual(fc_figmaauth.check("design-591", "/w"), ([], None))

    def test_a_check_that_cannot_run_never_blocks(self):
        for boom in (FileNotFoundError("claude"), subprocess.TimeoutExpired("claude", 1)):
            with mock.patch("subprocess.run", side_effect=boom):
                self.assertEqual(fc_figmaauth.check("design-591", "/w"), ([], None))
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertEqual(fc_figmaauth.check("design-591", "/w"), ([], None))
        with self.lists(NEEDS, denied=""):  # the second look prints nothing (the issue's own attempt): not proof of anything
            self.assertEqual(fc_figmaauth.check("design-591", "/w"), ([], None))
        self.assertFalse(self.cache.exists())

    def test_no_plugin_server_or_a_failing_one_is_not_this_problem(self):
        with self.lists(NO_PLUGIN):
            self.assertEqual(fc_figmaauth.check("design-591", "/w"), ([], None))
        with self.lists(NEEDS.replace("! Needs authentication", "✘ Failed to connect - HTTP 502")):
            self.assertEqual(fc_figmaauth.check("design-591", "/w"), ([], None))

    def test_a_role_that_does_not_write_runs_no_command(self):
        with self.lists(NEEDS) as run:
            self.assertEqual(fc_figmaauth.check("design-review", "/w"), ([], None))
            self.assertEqual(fc_figmaauth.check("planner", "/w"), ([], None))
        run.assert_not_called()

    def test_an_old_pass_is_asked_again(self):
        self.cache.write_text(json.dumps({"figma-signin": {"at": time.time() - fc_figmaauth.TTL - 1, "deny": []}}))
        with self.lists(NEEDS, denied=NO_PLUGIN):
            self.assertIsNotNone(fc_figmaauth.check("design-591", "/w")[1])


if __name__ == "__main__":
    unittest.main()
