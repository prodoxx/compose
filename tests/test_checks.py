import hermetic  # first: no test sees the real home
import datetime, os, platform, shutil, stat, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
import fc_checks as C
ORIGINAL_PATH = os.environ["PATH"]


class Checks(unittest.TestCase):
    """Each check reads a fake HOME and PATH holding fake tools; nothing else on the machine."""
    def setUp(self):
        self.bin, self.home = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        p = mock.patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.home)}); p.start(); self.addCleanup(p.stop)

    def fake(self, tool, body):
        (self.bin / tool).write_text(f"#!/bin/sh\n{body}\n")
        (self.bin / tool).chmod(stat.S_IRWXU)

    def test_link_state(self):
        (self.home / "real").write_text("x")
        (self.home / "hop").symlink_to(self.home / "real")
        (self.home / "via").symlink_to("hop")
        (self.home / "gone").symlink_to("nowhere/x")
        self.assertEqual(C.link_state(self.home / "via"), ("ok", str((self.home / "real").resolve())))
        self.assertEqual(C.link_state(self.home / "gone"), ("dangling", "nowhere/x"))
        self.assertEqual(C.link_state(self.home / "none"), ("missing", None))

    def test_path_and_tools(self):
        os.environ["PATH"] = f"{self.bin}:{self.home}/.local/binx"
        self.assertFalse(C.local_bin_on_path())
        os.environ["PATH"] = f"{self.bin}:{self.home}/.local/bin"
        self.assertTrue(C.local_bin_on_path())
        self.assertFalse(C.gh_authed())
        for code, want in ((1, False), (0, True)):
            self.fake("gh", f'[ "$*" = "auth status" ] || exit 9\nexit {code}')
            self.assertIs(C.gh_authed(), want)
        self.assertFalse(C.rerere_set())
        for out, want in (("false", False), ("true", True)):
            self.fake("git", f'[ "$*" = "config --global --get rerere.enabled" ] || exit 9\necho {out}')
            self.assertIs(C.rerere_set(), want)

    def test_jev_and_chromium(self):
        self.assertFalse(C.jev_synced(self.home))
        (self.home / "jev/.venv").mkdir(parents=True)
        self.assertTrue(C.jev_synced(self.home))
        cache, arch = self.home / "Library/Caches/ms-playwright", "arm64" if platform.machine() == "arm64" else "x64"
        self.assertFalse(C.chromium_installed())
        app = Path(f"chrome-mac-{arch}/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
        for v in ("chromium-999", "chromium-1200", "chromium_headless_shell-2000"):
            (cache / v / app.parent).mkdir(parents=True)
        (cache / "chromium-999" / app).write_text("")
        self.assertFalse(C.chromium_installed())
        (cache / "chromium-1200" / app).write_text("")
        self.assertTrue(C.chromium_installed())


class Machine(unittest.TestCase):
    """The machine checks behind doctor and the start of run and verify read fake pmset, ps and docker, a patched load average and core count."""
    def setUp(self):
        self.bin = Path(tempfile.mkdtemp())
        p = mock.patch.dict(os.environ, {"PATH": str(self.bin)}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("COMPOSE_NO_PREFLIGHT", None)  # hermetic sets it; the cleanup above restores it
        for p in (mock.patch("os.getloadavg", return_value=(1.0, 1.0, 1.0)), mock.patch("os.cpu_count", return_value=8)):
            p.start(); self.addCleanup(p.stop)

    fake = Checks.fake

    def docker(self, rows, ids="aaa bbb"):
        """A docker that lists `ids` as running or restarting and inspects them into `rows` (lines of name|status|restarts|started)."""
        echoes = "; ".join(f"echo '{r}'" for r in rows)
        self.fake("docker", f'case "$1" in ps) echo "{ids}";; inspect) {echoes};; esac')

    def ago(self, seconds):
        return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")

    def batt(self, power, pct):
        self.fake("pmset", f"echo \"Now drawing from '{power}'\"\necho \" -InternalBattery-0 (id=4653155)\t{pct}%; discharging; 3:12 remaining present: true\"")

    def test_battery_stop_stops_only_a_nearly_empty_battery_and_names_the_charge_and_the_override(self):
        self.assertIsNone(C.battery_stop())  # no pmset
        self.batt("Battery Power", 97)  # on battery with charge to spare: the work starts
        self.assertIsNone(C.battery_stop())
        self.batt("Battery Power", 11)
        self.assertIsNone(C.battery_stop())
        self.batt("Battery Power", 10)
        self.assertEqual(C.battery_stop(), "the Mac is on battery at 10%: it can die before the work ends; plug it in, "
                                           "or run again with --force to start anyway; `compose watch` says `event power=ac` once it is plugged in")
        self.assertIsNone(C.battery_stop(force=True))
        with mock.patch.dict(os.environ, {"COMPOSE_NO_PREFLIGHT": "1"}):
            self.assertIsNone(C.battery_stop())
        self.batt("AC Power", 5)  # charging at 5% is fine
        self.assertIsNone(C.battery_stop())
        self.fake("pmset", 'echo "Now drawing from \'Battery Power\'"')  # a charge that cannot be read is not known to be low
        self.assertIsNone(C.battery_stop())

    def test_a_battery_stop_is_announced_once_per_watcher_when_the_mac_is_plugged_in(self):
        held = Path(tempfile.mkdtemp()) / "live" / "battery-held"
        with mock.patch.object(C, "POWER_HELD", held):
            self.assertEqual(C.power_back(None), (None, None))  # nothing stopped
            self.batt("Battery Power", 8)
            self.assertIsNotNone(C.battery_stop())
            self.assertEqual(C.power_back(None), (None, None))  # still on battery
            self.batt("AC Power", 21)
            line, at = C.power_back(None)
            self.assertRegex(line, r"^event power=ac held=\d\d:\d\d:\d\d  \(the Mac is plugged in: start again, with no ask, whatever a low battery stopped\)$")
            self.assertEqual(C.power_back(at), (None, at))  # this watcher said it already
            self.assertIsNotNone(C.power_back(at - 1)[0])  # another watcher has not
            held.write_text('{"at": %f}' % (at - C.POWER_HELD_FOR - 5))
            self.assertEqual(C.power_back(None), (None, None))  # a stop from hours ago is no news
            self.fake("pmset", "exit 0")  # pmset says nothing: no claim the Mac is plugged in
            held.write_text('{"at": %f}' % at)
            self.assertEqual(C.power_back(None), (None, None))

    def test_busiest_process_is_the_first_row_of_ps(self):
        self.assertIsNone(C.busiest_process())
        self.fake("ps", 'echo " 174.2 /usr/local/bin/node"\necho "  3.0 /bin/zsh"')
        self.assertEqual(C.busiest_process(), (174.2, "node"))

    def test_a_crash_looping_container_is_named_and_a_calm_one_is_not(self):
        self.assertEqual(C.restarting_containers(), [])  # no docker installed
        self.docker([f"/api|restarting|7|{self.ago(5)}", f"/db|running|0|{self.ago(86400)}", f"/old|running|9|{self.ago(86400)}", f"/flap|running|4|{self.ago(30)}"])
        self.assertEqual(C.restarting_containers(), ["api", "flap"])
        self.fake("docker", "exit 1")  # installed but the daemon is not running
        self.assertEqual(C.restarting_containers(), [])
        self.fake("docker", "echo")  # running, nothing to inspect
        self.assertEqual(C.restarting_containers(), [])

    def test_warnings_are_one_line_each_with_the_fix_and_empty_on_a_calm_machine(self):
        self.assertEqual(C.machine_warnings(), [])
        self.fake("pmset", 'echo "Now drawing from \'Battery Power\'"')
        self.fake("ps", 'echo "174.2 /usr/local/bin/node"')
        self.docker([f"/api|restarting|7|{self.ago(5)}"])
        with mock.patch("os.getloadavg", return_value=(60.0, 51.0, 40.0)):
            battery, load, container = C.machine_warnings()
        self.assertIn("on battery: closing the lid will sleep it", battery)
        self.assertEqual(load, "WARNING: load average 51 on 8 cores: checks and e2e run slow and time out; busiest process is node at 174% CPU, stop it or wait")
        self.assertEqual(container, "WARNING: docker container api keeps restarting and burns CPU, so a verify measures the machine, not the code: docker stop api")
        with mock.patch("os.getloadavg", return_value=(16.0, 16.0, 16.0)):  # twice the cores is busy, not far above
            self.assertEqual(len(C.machine_warnings()), 2)


class Startup(unittest.TestCase):
    """The startup checklist: docker where the project's compose file needs it, the database targets the env files set, the lines a start prints."""
    def setUp(self):
        self.bin, self.wt = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()).resolve()
        p = mock.patch.dict(os.environ, {"PATH": str(self.bin)}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("COMPOSE_NO_AWAKE", None)
        os.environ["PATH"] += os.pathsep + str(Path(shutil.which("git", path=ORIGINAL_PATH)).parent)  # the real git, after the fake tools
        subprocess.run(["git", "init", "-q", str(self.wt)], check=True)
        (self.wt / ".gitignore").write_text(".env\n.env.*\n")

    fake = Checks.fake

    def test_a_database_url_gives_its_host_port_and_name_and_never_its_password(self):
        self.assertEqual(C.database_target("postgresql://dtt:s3cret@localhost:5433/dtt?schema=public"), "localhost:5433/dtt")
        self.assertEqual(C.database_target("postgres://u:p@db.example.com/website"), "db.example.com/website")
        self.assertEqual(C.database_target("mysql://root@127.0.0.1:3306/shop/"), "127.0.0.1:3306/shop")
        self.assertEqual(C.database_target("file:./dev.db"), "./dev.db")
        self.assertEqual(C.database_target("redis://:pw@cache:6379"), "cache:6379")
        self.assertIsNone(C.database_target("https://api.example.com"))
        self.assertIsNone(C.database_target("not a url"))

    def test_env_values_drop_export_quotes_and_trailing_comments(self):
        f = self.wt / ".env"
        f.write_text('export A="x y"\nB=\'q\'\n# comment\nC=plain # note\nbad line\nD=\n')
        self.assertEqual(C.env_values(f), {"A": "x y", "B": "q", "C": "plain", "D": ""})
        self.assertEqual(C.env_values(self.wt / "missing"), {})

    def test_database_targets_are_keyed_by_env_file_so_an_app_with_its_own_database_is_a_line_of_its_own(self):
        (self.wt / ".env").write_text("DATABASE_URL=postgresql://u:p@localhost:5432/website\nNEXT_PUBLIC_URL=https://x.test\n")
        (self.wt / "app").mkdir(); (self.wt / "app" / ".env.local").write_text("DATABASE_URL=postgresql://u:p@localhost:5433/dtt\n")
        (self.wt / ".env.example").write_text("DATABASE_URL=postgresql://user:pass@host/db\n")  # tracked, not ignored: no target
        subprocess.run(["git", "-C", str(self.wt), "add", "-f", ".gitignore", ".env.example"], check=True, capture_output=True)
        self.assertEqual(C.database_targets(self.wt), {".env DATABASE_URL": "localhost:5432/website", "app/.env.local DATABASE_URL": "localhost:5433/dtt"})

    def test_database_problems_name_a_changed_and_an_unconfirmed_target_with_how_to_confirm(self):
        now = {".env DATABASE_URL": "localhost:5433/dtt", "app/.env DATABASE_URL": "localhost:5433/app"}
        self.assertEqual(C.database_problems(now, now), [])
        changed, new = C.database_problems(now, {".env DATABASE_URL": "localhost:5432/website"})
        self.assertTrue(changed.startswith("database target changed: .env DATABASE_URL points at localhost:5433/dtt, the project started on localhost:5432/website; confirm"))
        self.assertIn('set `database` {".env DATABASE_URL": "localhost:5433/dtt"} in verify.json', changed)
        self.assertTrue(new.startswith("database target not confirmed: app/.env DATABASE_URL points at localhost:5433/app, which the project did not start with"))
        self.assertEqual(C.database_problems({}, {".env DATABASE_URL": "gone"}), [])  # a target that went away is no problem

    def docker(self, body):
        self.fake("docker", body)

    def test_docker_problems_only_where_a_compose_file_needs_it(self):
        self.docker("exit 1")
        self.assertEqual(C.docker_problems(self.wt), [])  # no compose file: docker is a bystander
        (self.wt / "compose.yaml").write_text("services: {}\n")
        self.assertEqual(C.docker_problems(self.wt), ["docker is not running and compose.yaml needs it: start Docker Desktop, bring the services up, and run again"])
        self.docker('case "$1" in info) exit 0;; ps) case "$*" in *unhealthy*) echo db;; *) echo db; echo api;; esac;; esac')
        self.assertEqual(C.docker_problems(self.wt), ["docker container db is unhealthy, so what runs against it fails for the wrong reason: docker logs db, then fix or restart it"])
        self.docker('case "$1" in info) exit 0;; ps) case "$*" in *unhealthy*) ;; *) echo db;; esac;; esac')
        self.assertEqual(C.docker_problems(self.wt), [])
        self.assertEqual(C.docker_line(self.wt), "docker: 1 container up, none unhealthy")
