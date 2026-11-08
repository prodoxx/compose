import hermetic  # first: no test sees the real home
import os, tempfile, unittest
from pathlib import Path
from unittest import mock
from tests.test_trail_setup import fc
import fc_host, fc_core  # noqa: E402
os.environ["HERDR_ENV"] = ""

HERDR = {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/s/herdr.sock", "HERDR_PANE_ID": "w1:p2"}
HC = {"HC_TERMINAL_ID": "t7", "HC_TERMINAL_SOCKET": "/s/default.sock"}
REX = {"REX_SESSION": "session:01a", "REX_BLOCK": "block:02b"}
CLEARED = {k: "" for k in (*HERDR, *HC, *REX, "COMPOSE_NO_HERDR", "COMPOSE_HOST", "REX_BIN_DIR")}


class Kind(unittest.TestCase):
    """Which host a session runs in, from the variables each host sets in its terminals, and the CLI it needs."""
    def ask(self, env, which=("hc",), rex=True):
        with mock.patch.dict(os.environ, {**CLEARED, **env}), mock.patch.object(fc_host, "which", lambda n: f"/bin/{n}" if n in which else None), \
                mock.patch.object(fc_host, "rex_bin", return_value="/bin/rex" if rex else None):
            return fc_host.kind(), fc_host.session_id(), fc_core.session_id()

    def test_headless_outside_every_host(self):
        self.assertEqual(self.ask({}), (None, None, None))

    def test_each_host_from_its_own_variables(self):
        self.assertEqual(self.ask(HERDR), ("herdr", "w1:p2", "w1:p2"))
        self.assertEqual(self.ask(HC), ("hc", "t7", "t7"))
        self.assertEqual(self.ask(REX), ("rex", "block:02b", "block:02b"))

    def test_the_herdr_app_and_rex_need_their_cli(self):
        self.assertEqual(self.ask(HC, which=())[0], None)
        self.assertEqual(self.ask(REX, rex=False)[0], None)
        self.assertEqual(self.ask(HC, which=())[2], "t7")  # the session's identity needs no CLI

    def test_herdr_wins_over_a_host_it_runs_inside(self):
        self.assertEqual(self.ask({**HC, **HERDR})[0], "herdr")
        self.assertEqual(self.ask({**REX, **HC})[0], "hc")

    def test_no_herdr_makes_every_host_headless(self):
        for env in (HERDR, HC, REX):
            self.assertEqual(self.ask({**env, "COMPOSE_NO_HERDR": "1"}), (None, None, None), env)

    def test_compose_host_forces_one_and_an_unknown_one_is_headless(self):
        self.assertEqual(self.ask({"COMPOSE_HOST": "rex", **REX})[0], "rex")
        self.assertEqual(self.ask({"COMPOSE_HOST": "hc", **HC}, which=())[0], "hc")  # forced: the CLI check is skipped
        self.assertEqual(self.ask({"COMPOSE_HOST": "tmux", **HERDR})[0], None)

    def test_names_and_the_session_variable_per_host(self):
        self.assertEqual([fc_host.name(k) for k in (*fc_host.KINDS, None)], ["herdr", "the Herdr app", "Rex", "a terminal host"])
        self.assertEqual(fc_host.SESSION_VARS, {"herdr": "HERDR_PANE_ID", "hc": "HC_TERMINAL_ID", "rex": "REX_BLOCK"})


class Installed(unittest.TestCase):
    def have(self, which, rex=False):
        with mock.patch.object(fc_host, "which", lambda n: f"/bin/{n}" if n in which else None), mock.patch.object(fc_host, "rex_bin", return_value="/bin/rex" if rex else None):
            return fc_host.installed()

    def test_the_app_first_then_herdr_then_rex(self):
        self.assertEqual(self.have({"hc", "herdr-app", "herdr"}, rex=True), ["hc", "herdr", "rex"])
        self.assertEqual(self.have({"herdr"}), ["herdr"])
        self.assertEqual(self.have(set()), [])

    def test_the_app_counts_only_with_both_its_programs(self):
        self.assertEqual(self.have({"hc"}), [])
        self.assertEqual(self.have({"herdr-app"}), [])

    def test_rex_bin_prefers_the_dir_rex_sets_then_path(self):
        d = Path(tempfile.mkdtemp())
        (d / "rex").write_text("#!/bin/sh\n")
        os.chmod(d / "rex", 0o755)
        with mock.patch.dict(os.environ, {"REX_BIN_DIR": str(d)}), mock.patch.object(fc_host, "which", return_value="/usr/local/bin/rex"):
            self.assertEqual(fc_host.rex_bin(), str(d / "rex"))
        with mock.patch.dict(os.environ, {"REX_BIN_DIR": "/nowhere"}), mock.patch.object(fc_host, "which", return_value="/usr/local/bin/rex"):
            self.assertEqual(fc_host.rex_bin(), "/usr/local/bin/rex")
        with mock.patch.dict(os.environ, {"REX_BIN_DIR": ""}), mock.patch.object(fc_host, "which", return_value=None):
            self.assertIsNone(fc_host.rex_bin())


class PaneEnv(unittest.TestCase):
    def test_the_variables_a_new_pane_gets_in_order(self):
        with mock.patch.dict(os.environ, {"COMPOSE_CONFIG": "/s/config.json", "HOME": "/s/home", "PATH": "/c/bin", "CODEX_HOME": ""}):
            env = fc_host.pane_env()
        self.assertEqual(list(env.items())[:2], [("DISABLE_AUTO_UPDATE", "true"), ("DISABLE_UPDATE_PROMPT", "true")])
        self.assertEqual([k for k in env if k not in ("DISABLE_AUTO_UPDATE", "DISABLE_UPDATE_PROMPT")], ["COMPOSE_CONFIG", "HOME", "PATH"])
        self.assertEqual((env["COMPOSE_CONFIG"], env["HOME"], env["PATH"]), ("/s/config.json", "/s/home", "/c/bin"))

    def test_with_env_wraps_the_program_in_env_only_when_there_is_something_to_set(self):
        self.assertEqual(fc_host.with_env({"A": "1", "B": "x y"}, ["pi", "--model", "m"]), ["env", "A=1", "B=x y", "pi", "--model", "m"])
        self.assertEqual(fc_host.with_env({}, ["pi"]), ["pi"])


if __name__ == "__main__":
    unittest.main()


class SpaceNames(unittest.TestCase):
    def test_a_project_space_is_named_under_the_root_of_its_parent(self):
        self.assertEqual(fc_host.space_name("dtt", "cart"), "dtt › cart")
        self.assertEqual(fc_host.space_name("dtt › recommender", "cart"), "dtt › cart")  # started from another project's space
        self.assertEqual(fc_host.space_name(None, "cart"), "cart")
        self.assertEqual(fc_host.space_name("cart", "cart"), "cart")

    def test_whose_space_and_where_its_tree_ends(self):
        self.assertTrue(fc_host.is_space_of("dtt › cart", "cart"))
        self.assertTrue(fc_host.is_space_of("cart", "cart"))
        self.assertFalse(fc_host.is_space_of("dtt › supercart", "cart"))
        self.assertEqual(fc_host.after_group(["a", "dtt", "dtt › x", "b", "dtt › y"], "dtt › x"), 4)
        self.assertIsNone(fc_host.after_group(["a"], "dtt"))
