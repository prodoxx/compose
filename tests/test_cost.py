import hermetic  # first: no test sees the real home
import contextlib, hashlib, importlib.machinery, importlib.util, io, json, os, sys, tempfile, time, unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import fc_cost, fc_sessions  # noqa: E402

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(BIN / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

START = "2026-10-17 09:05:00"


def ts(delta=0):
    t = time.mktime(time.strptime(START, "%Y-%m-%d %H:%M:%S")) + delta
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user(text, delta=0):
    return {"type": "user", "timestamp": ts(delta), "message": {"role": "user", "content": text}}


def asst(mid, model="claude-sonnet-5-5", **u):
    return {"type": "assistant", "timestamp": ts(5), "message": {"id": mid, "model": model, "usage": u}}


def write(path, lines, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines))
    if mtime:
        os.utime(path, (mtime, mtime))


class SessionUsage(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        p = mock.patch("pathlib.Path.home", return_value=self.home)
        p.start()
        self.addCleanup(p.stop)

    def test_usage_counts_each_id_once_and_skips_bad_lines(self):
        big = dict(input_tokens=2, cache_creation_input_tokens=28939, cache_read_input_tokens=7634, output_tokens=4)
        log = self.home / "s.jsonl"
        write(log, [user("hi"), asst("m1", **big), {"type": "summary"}, asst("m1", **big),
                    asst("m2", "claude-opus-5-5", input_tokens=10, output_tokens=7), '{"type": "assistant", "mess'])
        self.assertEqual(fc_sessions.usage(str(log)), {"model": "claude-sonnet-5-5", "turns": 2, "input": 12,
                                                       "cache_created": 28939, "cache_read": 7634, "output": 11})
        write(log, [user("hi"), "not json"])
        self.assertEqual(fc_sessions.usage(log), {"model": None, "turns": 0, "input": 0, "cache_created": 0, "cache_read": 0, "output": 0})

    def test_find_session_by_brief_and_start(self):
        cwd = "/w/.t/x"
        self.assertIsNone(fc_sessions.find_session(cwd, START, "/p/a.brief.md"))
        d, now = fc_sessions.log_dir(cwd), time.time()
        write(d / "a_old.jsonl", [user("Your brief is the file /p/a.brief.md.", -121)], now - 35)
        write(d / "a.jsonl", [user("Your brief is the file /p/a.brief.md.", -60), asst("m1")], now - 40)
        write(d / "b.jsonl", [user([{"type": "text", "text": "Your brief is the file /p/b.brief.md."}], 30)], now - 30)
        write(d / "later.jsonl", [user("other", 40), user("/p/a.brief.md", 41)], now - 20)
        self.assertEqual(fc_sessions.find_session(cwd, START, "/p/a.brief.md"), d / "a.jsonl")
        self.assertEqual(fc_sessions.find_session(Path(cwd), START, Path("/p/b.brief.md")), d / "b.jsonl")
        self.assertIsNone(fc_sessions.find_session(cwd, START, "/p/c.brief.md"))
        write(d / "a2.jsonl", [user("Your brief is the file /p/a.brief.md.", 50)], now - 10)
        self.assertEqual(fc_sessions.find_session(cwd, START, "/p/a.brief.md"), d / "a2.jsonl")

    def test_usage_reads_only_what_a_log_gained_since(self):
        """A live session's log grows every turn and reaches tens of MB; `status` must not parse it whole on every call."""
        log = self.home / "d.jsonl"
        write(log, [asst("m1", input_tokens=1, output_tokens=2), asst("m2", input_tokens=3, output_tokens=4), asst("m3", input_tokens=5, output_tokens=6)], 1_000_000)
        with mock.patch.object(fc_sessions.json, "loads", wraps=json.loads) as loads:
            self.assertEqual(fc_sessions.usage(log)["turns"], 3)
            n = loads.call_count
            with log.open("a") as f:
                f.write(json.dumps(asst("m4", input_tokens=10, output_tokens=20)) + "\n")
            self.assertEqual(fc_sessions.usage(log), {"model": "claude-sonnet-5-5", "turns": 4, "input": 19, "cache_created": 0, "cache_read": 0, "output": 32})
            self.assertEqual(loads.call_count, n + 1, "the whole log was parsed again for one new line")

    def test_usage_waits_for_a_line_still_being_written(self):
        log = self.home / "e.jsonl"
        write(log, [asst("m1", input_tokens=1, output_tokens=2)], 1_000_000)
        self.assertEqual(fc_sessions.usage(log)["turns"], 1)
        half = json.dumps(asst("m2", input_tokens=3, output_tokens=4))
        with log.open("a") as f:
            f.write(half[:20])
        self.assertEqual(fc_sessions.usage(log)["turns"], 1)
        with log.open("a") as f:
            f.write(half[20:] + "\n")
        self.assertEqual(fc_sessions.usage(log), {"model": "claude-sonnet-5-5", "turns": 2, "input": 4, "cache_created": 0, "cache_read": 0, "output": 6})

    def test_usage_starts_over_when_a_log_was_rewritten_shorter(self):
        log = self.home / "f.jsonl"
        write(log, [asst("m1", input_tokens=1, output_tokens=2), asst("m2", input_tokens=3, output_tokens=4)], 1_000_000)
        self.assertEqual(fc_sessions.usage(log)["turns"], 2)
        write(log, [asst("m9", input_tokens=7, output_tokens=8)], 1_000_001)
        self.assertEqual(fc_sessions.usage(log), {"model": "claude-sonnet-5-5", "turns": 1, "input": 7, "cache_created": 0, "cache_read": 0, "output": 8})

    def test_find_session_reads_the_log_directory_again_only_when_it_changes(self):
        cwd, now = "/w/.t/y", time.time()
        d = fc_sessions.log_dir(cwd)
        write(d / "a.jsonl", [user("Your brief is the file /p/a.brief.md.", -60), asst("m1")], now - 40)
        with mock.patch.object(fc_sessions, "_first_user", wraps=fc_sessions._first_user) as reads:
            self.assertEqual(fc_sessions.find_session(cwd, START, "/p/a.brief.md"), d / "a.jsonl")
            n = reads.call_count
            self.assertGreater(n, 0)
            self.assertEqual(fc_sessions.find_session(cwd, START, "/p/a.brief.md"), d / "a.jsonl")
            self.assertEqual(reads.call_count, n, "the directory did not change, yet every log was opened again")
            time.sleep(0.01)
            write(d / "a2.jsonl", [user("Your brief is the file /p/a.brief.md.", 50)], now - 10)
            self.assertEqual(fc_sessions.find_session(cwd, START, "/p/a.brief.md"), d / "a2.jsonl")

    def test_usage_parses_again_only_when_mtime_or_size_changes(self):
        log = self.home / "c.jsonl"
        write(log, [asst("m1", input_tokens=1, output_tokens=2)], 1_000_000)
        want = {"model": "claude-sonnet-5-5", "turns": 1, "input": 1, "cache_created": 0, "cache_read": 0, "output": 2}
        with mock.patch.object(fc_sessions.json, "loads", wraps=json.loads) as loads:
            self.assertEqual(fc_sessions.usage(str(log)), want)
            n = loads.call_count
            self.assertEqual(fc_sessions.usage(log), want)
            self.assertEqual(loads.call_count, n)
            with log.open("a") as f:
                f.write(json.dumps(asst("m2", input_tokens=10, output_tokens=20)) + "\n")
            self.assertEqual(fc_sessions.usage(log), dict(want, turns=2, input=11, output=22))
            self.assertGreater(loads.call_count, n)
            write(log, [asst("m1", input_tokens=7, output_tokens=2)], 1_000_100)
            self.assertEqual(fc_sessions.usage(log), dict(want, input=7))


class SessionDiskCache(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        p = mock.patch("pathlib.Path.home", return_value=self.home)
        p.start()
        self.addCleanup(p.stop)
        self.log = self.home / "s.jsonl"
        self.saved = self.home / ".compose" / "usage" / (hashlib.sha1(str(self.log).encode()).hexdigest() + ".json")

    def read(self):
        """usage() as a new process calls it: nothing in memory. Returns (result, opens of the log, message lines parsed)."""
        fc_sessions._cache.clear()
        opened, parsed, real_open, real_loads = [], [], open, json.loads
        spy_open = lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1]
        spy_loads = lambda s, *a, **k: (parsed.append("message" in str(s)), real_loads(s, *a, **k))[1]
        with mock.patch("builtins.open", spy_open), mock.patch("io.open", spy_open), mock.patch.object(fc_sessions.json, "loads", spy_loads):
            result = fc_sessions.usage(self.log)
        return result, opened.count(str(self.log)), sum(parsed)

    def two(self):
        write(self.log, [asst("m1", input_tokens=1, output_tokens=2), asst("m2", input_tokens=3, output_tokens=4)], 1_000_000)

    def test_unchanged_log_is_not_opened_by_a_new_process(self):
        self.two()
        want, opens, _ = self.read()
        self.assertEqual(opens, 1)
        saved = json.loads(self.saved.read_text())
        self.assertEqual(sorted(saved), ["model", "offset", "result", "seen", "stat", "sums", "turns"])
        self.assertEqual((saved["seen"], saved["offset"], saved["result"]), (["m1", "m2"], self.log.stat().st_size, want))
        self.assertEqual([x.name for x in self.saved.parent.iterdir()], [self.saved.name])
        before = self.saved.read_bytes()
        self.assertEqual(self.read(), (want, 0, 0))
        self.assertEqual(self.saved.read_bytes(), before)

    def test_grown_log_is_read_from_the_saved_offset_and_matches_a_fresh_read(self):
        self.two()
        self.read()
        m3, m2, m4 = (json.dumps(asst(i, input_tokens=5, output_tokens=6)) for i in ("m3", "m2", "m4"))
        with self.log.open("a") as f:
            f.write(m3 + "\n" + m2 + "\n" + m4[:20])
        result, opens, parsed = self.read()
        self.assertEqual((result["turns"], opens, parsed), (3, 1, 2))
        with self.log.open("a") as f:
            f.write(m4[20:] + "\n")
        result, opens, parsed = self.read()
        self.assertEqual((result["turns"], opens, parsed), (4, 1, 1))
        self.saved.unlink()
        self.assertEqual(self.read()[0], result)

    def test_a_message_without_an_id_is_saved_beside_the_ones_with_one(self):
        write(self.log, [asst(None, input_tokens=1, output_tokens=2), asst("m1", input_tokens=3, output_tokens=4)], 1_000_000)
        want, opens, _ = self.read()
        self.assertEqual((want["turns"], opens), (2, 1))
        self.assertEqual(sorted(json.loads(self.saved.read_text())["seen"], key=str), [None, "m1"])  # the save did not raise on None beside a str
        self.assertEqual(self.read(), (want, 0, 0))

    def test_log_rewritten_shorter_is_read_from_the_start(self):
        self.two()
        self.read()
        write(self.log, [asst("m9", input_tokens=7, output_tokens=8)], 1_000_001)
        self.assertEqual(self.read(), ({"model": "claude-sonnet-5-5", "turns": 1, "input": 7, "cache_created": 0, "cache_read": 0, "output": 8}, 1, 1))
        self.assertEqual(json.loads(self.saved.read_text())["offset"], self.log.stat().st_size)

    def test_broken_cache_file_is_ignored(self):
        self.two()
        want = self.read()[0]
        self.assertTrue(self.saved.exists())
        self.saved.write_text('{"offset": 99, "seen": [')
        self.assertEqual(self.read(), (want, 1, 2))

    def test_short_or_misshapen_cache_entry_falls_back_to_a_fresh_read(self):
        self.two()
        want = self.read()[0]
        good = json.loads(self.saved.read_text())
        for bad in ({}, {k: v for k, v in good.items() if k != "result"}, dict(good, stat=5), dict(good, seen=3), [1]):
            self.saved.write_text(json.dumps(bad))
            self.assertEqual(self.read(), (want, 1, 2))

    def test_the_save_goes_through_a_temp_name_of_its_own_and_a_failed_save_costs_only_the_cache(self):
        self.two()
        real, seen = os.replace, []
        def replace(src, dst):
            seen.append(Path(src).name)
            real(src, dst)
        with mock.patch("os.replace", replace):
            want = self.read()[0]
        self.assertEqual((len(seen), seen[0] != self.saved.name + ".tmp", seen[0].startswith(self.saved.name), [x.name for x in self.saved.parent.iterdir()]), (1, True, True, [self.saved.name]))
        self.saved.unlink()
        with mock.patch("os.replace", side_effect=FileNotFoundError):
            self.assertEqual(self.read()[0], want)


def project():
    proj = Path(tempfile.mkdtemp())
    crew = proj / "results" / "crew"
    crew.mkdir(parents=True)
    for name, role, model, at in [("a-late", "planner", "claude:claude-opus-5-5", "2026-10-17 09:00:00"),
                                  ("z-early", "implementer", "pi:openai/gpt-6-astra", "2026-10-17 08:00:00")]:
        (crew / (name + ".json")).write_text(json.dumps({"name": name, "role": role, "model": model, "cwd": str(proj), "started_at": at}))
        (crew / (name + ".brief.md")).write_text("brief")
    (crew / "stray.json").write_text(json.dumps({"x": 1}))
    run = {"pi:openrouter/x": {"calls": 3, "tokens_in": 1200, "tokens_out": 300, "cost": 0.01, "priced": True},
           "typesafe": {"calls": 1, "tokens_in": 50, "tokens_out": 7, "cost": 0.0, "priced": False}, "total": 0.01}
    (proj / "results" / "run-cost.json").write_text(json.dumps(run))
    return proj


class CostRows(unittest.TestCase):
    def setUp(self):
        self.proj = project()
        log = self.proj / "a.jsonl"
        found = lambda cwd, at, brief: log if Path(brief).name == "a-late.brief.md" else None  # noqa: E731
        use = {"model": "claude-opus-5-5", "turns": 4, "input": 10, "cache_created": 200, "cache_read": 3000, "output": 40}
        self.find = mock.patch.object(fc_sessions, "find_session", side_effect=found).start()
        mock.patch.object(fc_sessions, "usage", return_value=use).start()
        self.addCleanup(mock.patch.stopall)
        self.rows = fc_cost.rows(self.proj)

    def test_rows(self):
        r = self.rows
        self.assertEqual([x["name"] for x in r[:2]], ["z-early", "a-late"])
        self.assertEqual({k: r[0][k] for k in ("role", "turns", "input", "cache_created", "cache_read", "output", "note")},
                         {"role": "implementer", "turns": None, "input": None, "cache_created": None, "cache_read": None, "output": None, "note": "no session log"})
        self.assertEqual({k: r[1][k] for k in ("turns", "input", "cache_created", "cache_read", "output", "note")},
                         {"turns": 4, "input": 10, "cache_created": 200, "cache_read": 3000, "output": 40, "note": ""})
        brief = self.proj / "results" / "crew" / "a-late.brief.md"
        self.assertIn((str(self.proj), "2026-10-17 09:00:00", brief), [(str(c.args[0]), c.args[1], Path(c.args[2])) for c in self.find.call_args_list])
        run = {x["name"]: x for x in r[2:]}
        self.assertEqual(len(r), 4)
        self.assertEqual({k: run["pi:openrouter/x"][k] for k in ("role", "turns", "input", "cache_created", "cache_read", "output")},
                         {"role": "run", "turns": 3, "input": 1200, "cache_created": 0, "cache_read": 0, "output": 300})
        self.assertEqual(run["typesafe"]["output"], 7)

    def test_total(self):
        t = fc_cost.total(self.rows)
        self.assertEqual((t["input"], t["cache_created"], t["cache_read"], t["output"], t["teammates"]), (1260, 200, 3000, 347, 2))

    def test_render(self):
        lines = fc_cost.render(self.rows).splitlines()
        self.assertEqual(len(lines), len(self.rows) + 2)
        self.assertTrue(lines[-1].startswith("total"))
        col = lines[0].index("role")
        for row, line in zip(self.rows, lines[1:-1]):
            self.assertEqual(line.index(row["role"], len(row["name"])), col, line)
        self.assertIn(" - ", lines[1])

class CostCommand(unittest.TestCase):
    ROWS = [{"name": "shop-planner", "role": "planner", "model": "claude-opus-5-5", "turns": 3, "input": 11, "cache_created": 22, "cache_read": 33, "output": 44, "note": ""}]
    TOTAL = {"input": 11, "cache_created": 22, "cache_read": 33, "output": 44, "turns": 3, "teammates": 1}

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.proj = self.repo / ".compose" / "shop"
        (self.proj / "results" / "crew").mkdir(parents=True)

    def fake(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(fc_cost, "rows", side_effect=lambda proj, runs=(): [dict(self.ROWS[0], name=proj.name)]))
        stack.enter_context(mock.patch.object(fc_cost, "total", side_effect=lambda rows: dict(self.TOTAL, teammates=len(rows[0]["name"]))))
        stack.enter_context(mock.patch.object(fc_cost, "render", return_value="TABLE"))
        return stack

    def cost(self, as_json):
        out = io.StringIO()
        with self.fake(), mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), contextlib.redirect_stdout(out):
            fc.fc_status.cmd_cost(SimpleNamespace(repo=str(self.repo), project="shop", json=as_json))
        return out.getvalue()

    def test_json_has_rows_and_their_total(self):
        self.assertEqual(json.loads(self.cost(True)), {"rows": [dict(self.ROWS[0], name="shop")], "total": dict(self.TOTAL, teammates=4)})

    def test_main_with_no_sessions_prints_cost_none(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["compose", "cost", "--project", "shop", "--repo", str(self.repo)]), \
                mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), contextlib.redirect_stdout(out):
            fc.main()
        self.assertEqual(out.getvalue().strip(), "cost  none")

    def test_status_puts_each_projects_totals_under_its_header(self):
        other = self.repo / ".compose" / "shop22"
        (other / "results").mkdir(parents=True)
        with self.fake(), mock.patch.object(fc.fc_status, "board", return_value={"trunk": "main", "rows": [], "layers": {}, "stack": ""}):
            lines = fc.fc_status.render_status(self.repo, [self.proj, other]).splitlines()
        heads = [i for i, line in enumerate(lines) if line.startswith("project ")]
        self.assertEqual([lines[i + 1] for i in heads], ["  cost  11in  22cc  33cr  44out  (4 teammates)", "  cost  11in  22cc  33cr  44out  (6 teammates)"])
        self.assertEqual(sum(line.startswith("  cost  ") for line in lines), 2)


if __name__ == "__main__":
    unittest.main()
