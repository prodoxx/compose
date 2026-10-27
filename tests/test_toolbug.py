import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


class Text(unittest.TestCase):
    """scrub, environment, issue_text: what reaches GitHub."""

    def setUp(self):
        p = mock.patch.dict(os.environ, {"HOME": "/Users/alice9", "USER": "alice9", "LOGNAME": "alice9"}); p.start(); self.addCleanup(p.stop)

    def test_scrub_replaces_home_users_and_tokens_and_keeps_the_rest(self):
        keep = "shop-tests at 1a2b3c4 / " + "0123456789abcdef" * 2 + "01234567, alice9x, bob7s, ghp_short"
        tokens = ["ghp_" + "A1" * 18, "gho_" + "b" * 20, "ghu_" + "c" * 24, "ghs_" + "D" * 30, "ghr_" + "e5" * 15,
                  "github_pat_11AB_" + "x9" * 20, "sk-or-v1-" + "f0" * 32, "sk-ant-api03-" + "Ab_-" * 6, "lin_api_" + "Z" * 40]
        raw = "/Users/alice9/code/x by alice9 and bob7: " + " ".join(tokens) + " Authorization: Bearer eyJ.abc-1 " + keep
        want = "~/code/x by <user> and <user>: " + " ".join(["<redacted>"] * len(tokens)) + " Authorization: Bearer <redacted> " + keep
        self.assertEqual(fc.fc_toolbug.scrub(raw, login="bob7"), want)
        self.assertEqual(fc.fc_toolbug.scrub("bob7 alice9"), "bob7 <user>")
        self.assertEqual(fc.fc_toolbug.scrub("prodoxx filed prodoxx/compose#1", login="prodoxx"), "<user> filed prodoxx/compose#1")

    def test_environment_lists_the_commit_platform_and_first_version_lines(self):
        import platform
        d = Path(tempfile.mkdtemp()).resolve()
        for name, out in (("claude", "2.1.302 (Claude Code)"), ("herdr", "herdr 0.9.1\nbuilt today"), ("gh", "gh version 2.94.0 (2026-09-01)")):
            (d / name).write_text(f"#!/bin/sh\necho '{out}'\n"); (d / name).chmod(0o755)
        real = subprocess.run
        run = lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 0, "abc1234\n", "") if cmd[0] == "git" else real(cmd, *a, **kw)  # the commit is git's to name
        with mock.patch.dict(os.environ, {"PATH": f"{d}:/usr/bin:/bin"}), mock.patch("subprocess.run", run):
            env = fc.fc_toolbug.environment()
        commit = "abc1234"
        self.assertEqual(env.strip().splitlines(), [f"- compose: {commit}", f"- OS: {platform.platform()}", f"- Python: {platform.python_version()}",
                         "- claude: 2.1.302 (Claude Code)", "- herdr: herdr 0.9.1", "- trail: not installed", "- gh: gh version 2.94.0 (2026-09-01)"])
        self.assertNotIn("/", env)

    def test_issue_text_splits_the_title_fills_environment_and_root_cause_and_is_stable(self):
        tb, env = fc.fc_toolbug, "- compose: abc1234\n- OS: at /Users/alice9/x"
        with mock.patch.object(tb, "environment", return_value=env):
            title, body = tb.issue_text("# crew start: hangs for alice9\n## Summary\n\n It hangs. \n\n## Symptom\nstuck\n\n## Root cause\n\n## Reproduction\nrun it\n")
            self.assertEqual(title, "crew start: hangs for <user>")
            self.assertEqual(body, "## Summary\n\nIt hangs.\n\n## Environment\n\n- compose: abc1234\n- OS: at ~/x\n\n## Symptom\n\nstuck\n\n"
                                   "## Root cause\n\nnot found\n\n## Reproduction\n\nrun it\n")
            self.assertEqual(tb.issue_text("# " + title + "\n" + body), (title, body))
            # no Summary: Environment goes first; a missing Root cause goes before Reproduction, else last; login scrubbed
            self.assertEqual(tb.issue_text("# x: y\n## Symptom\nbob7 saw it\n## Reproduction\nrun\n", login="bob7")[1],
                             "## Environment\n\n- compose: abc1234\n- OS: at ~/x\n\n## Symptom\n\n<user> saw it\n\n## Root cause\n\nnot found\n\n## Reproduction\n\nrun\n")
            self.assertEqual(tb.issue_text("# x: y\n## Symptom\nz\n## Environment\nold /opt/stuff\n## Fix\npatch\n")[1],
                             "## Symptom\n\nz\n\n## Environment\n\n- compose: abc1234\n- OS: at ~/x\n\n## Fix\n\npatch\n\n## Root cause\n\nnot found\n")
            for bad in ("crew start: hangs\n## Summary\nx\n", "#crew: hangs\n"):
                with self.assertRaises(ValueError):
                    tb.issue_text(bad)


class Gh(unittest.TestCase):
    """gh_status, open_matches, post, record_issue against a fake gh on PATH."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        self.log = self.home / ".compose" / "tool-bugs.jsonl"
        for p in (mock.patch.object(fc.fc_crew, "TOOL_BUGS", self.log),
                  mock.patch.object(fc.fc_config, "CONFIG_PATH", self.home / ".compose" / "config.json")):
            p.start(); self.addCleanup(p.stop)

    def fake_gh(self, replies):
        d = Path(tempfile.mkdtemp()).resolve(); (d / "replies.json").write_text(json.dumps(replies))
        (d / "gh").write_text(f'''#!{sys.executable}
import json, sys
d = {str(d)!r}
open(d + "/calls.jsonl", "a").write(json.dumps(sys.argv[1:]) + "\\n")
code, out = json.load(open(d + "/replies.json")).get(" ".join(sys.argv[1:3]), [1, ""])
print(out, end="")
sys.exit(code)
''')
        (d / "gh").chmod(0o755)
        p = mock.patch.dict(os.environ, {"PATH": f"{d}:/usr/bin:/bin"}); p.start(); self.addCleanup(p.stop)
        return lambda: [json.loads(l) for l in (d / "calls.jsonl").read_text().splitlines()]

    def test_gh_status_is_no_gh_maintainer_or_reporter(self):
        with mock.patch.dict(os.environ, {"PATH": tempfile.mkdtemp()}): self.assertEqual(fc.fc_toolbug.gh_status(), ("no-gh", None))
        self.fake_gh({"api user": [1, "alice9\n"], "api repos/prodoxx/compose": [0, "true\n"]})
        self.assertEqual(fc.fc_toolbug.gh_status(), ("no-gh", None))
        for perm, state in (([0, "true\n"], "maintainer"), ([0, "false\n"], "reporter"), ([1, "true\n"], "reporter")):
            calls = self.fake_gh({"api user": [0, "alice9\n"], "api repos/prodoxx/compose": perm})
            self.assertEqual(fc.fc_toolbug.gh_status(), (state, "alice9"))
            self.assertEqual(calls(), [["api", "user", "--jq", ".login"], ["api", "repos/prodoxx/compose", "--jq", ".permissions.push"]])

    def test_open_matches_searches_the_symptom(self):
        hit = [{"number": 1, "title": "crew start: hangs", "url": "https://github.com/prodoxx/compose/issues/1"}]
        calls = self.fake_gh({"issue list": [0, json.dumps(hit)]})
        self.assertEqual(fc.fc_toolbug.open_matches("crew start: hangs: at trust"), hit)
        self.assertEqual(fc.fc_toolbug.open_matches("hangs at trust"), hit)
        args, tail = ["issue", "list", "--repo", "prodoxx/compose", "--state", "open", "--search"], ["--json", "number,title,url", "--limit", "5"]
        self.assertEqual(calls(), [args + ["hangs: at trust"] + tail, args + ["hangs at trust"] + tail])
        self.fake_gh({"issue list": [1, "[]"]})
        self.assertEqual(fc.fc_toolbug.open_matches("x: y"), [])

    def test_post_creates_or_comments_and_returns_the_url(self):
        calls = self.fake_gh({"issue create": [0, "Creating issue\nhttps://github.com/prodoxx/compose/issues/2\n\n"],
                              "issue comment": [0, "https://github.com/prodoxx/compose/issues/1#c-9\n"]})
        self.assertEqual(fc.fc_toolbug.post("crew: t", "## Summary\n"), "https://github.com/prodoxx/compose/issues/2")
        self.assertEqual(fc.fc_toolbug.post("crew: t", "b", comment=1), "https://github.com/prodoxx/compose/issues/1#c-9")
        self.assertEqual(calls(), [["issue", "create", "--repo", "prodoxx/compose", "--label", "bug", "--title", "crew: t", "--body", "## Summary\n"],
                                   ["issue", "comment", "1", "--repo", "prodoxx/compose", "--body", "b"]])
        self.fake_gh({"issue create": [1, "https://github.com/prodoxx/compose/issues/3\n"]})
        self.assertIsNone(fc.fc_toolbug.post("t", "b"))

    def test_record_issue_marks_only_the_projects_last_entry_with_that_at(self):
        url, at = "https://github.com/prodoxx/compose/issues/2", "2026-10-17 15:30:00"
        self.assertIsNone(fc.fc_toolbug.record_issue("shop", url, at)); self.assertFalse(self.log.exists())
        self.log.parent.mkdir(parents=True)
        lines = ['{"at": "2026-10-17 09:00:00", "project": "shop", "message": "old"}\n', '{"at":"2026-10-17 15:30:00","project":"shop","message":"a"}\n',
                 '{"at": "2026-10-17 15:30:00", "project": "blog",  "message": "x"}\n', '{"at":"2026-10-17 15:30:00","project":"shop","message":"b"}\n',
                 '{"at": "2026-10-17 18:00:00", "project": "shop", "message": "c"}\n', '{"at":"2026-10-17 18:00:00","project":"shop","issue":"https://kept/1"}\n']
        self.log.write_text("".join(lines))
        self.assertEqual(fc.fc_toolbug.record_issue("shop", url, at), url)
        got = self.log.read_text().splitlines(keepends=True)
        self.assertEqual(got[:3] + got[4:], lines[:3] + lines[4:])
        self.assertEqual(json.loads(got[3]), {"at": at, "project": "shop", "message": "b", "issue": url})
        recorded = self.log.read_text()
        # a linked entry keeps its link, even when an older entry with the same at has none; no match records nothing
        for project, at, want in (("shop", at, url), ("shop", "2026-10-17 18:00:00", "https://kept/1"), ("blog", "2026-10-17 09:00:00", None), ("cafe", at, None)):
            with self.subTest(project, at=at):
                self.assertEqual(fc.fc_toolbug.record_issue(project, "https://other/9", at), want)
                self.assertEqual(self.log.read_text(), recorded)

    def test_record_issue_marks_the_numbered_entry_among_several_with_the_same_at(self):
        at = "2026-10-17 15:30:00"
        self.log.parent.mkdir(parents=True)
        self.log.write_text("".join(json.dumps({"at": at, "project": "shop", "message": m}) + "\n" for m in "ab"))
        for n, url in ((2, "https://x/2"), (1, "https://x/1")):
            self.assertEqual(fc.fc_toolbug.record_issue("shop", url, f"{at}#{n}"), url)
        self.assertEqual([(e["message"], e["issue"]) for e in map(json.loads, self.log.read_text().splitlines())], [("a", "https://x/1"), ("b", "https://x/2")])
        self.assertEqual(fc.fc_toolbug.record_issue("shop", "https://x/9", f"{at}#2"), "https://x/2")
        self.assertIsNone(fc.fc_toolbug.record_issue("shop", "https://x/9", f"{at}#3"))

    def test_record_issue_skips_lines_that_are_not_json_objects(self):
        url, at = "https://github.com/prodoxx/compose/issues/2", "2026-10-17 15:30:00"
        bad = ['{"at": "2026-10-17 15:30:00", "project": "shop"\n', "\n", "   \n", '[{"at":"2026-10-17 15:30:00","project":"shop"}]\n',
               '"shop"\n', "42\n", "null\n", "not json at all\n"]
        self.log.parent.mkdir(parents=True)
        for case, after in (("only bad lines", '{"at"'), ("bad lines last, no newline", '[1, 2]')):
            with self.subTest(case):
                self.log.write_text("".join(bad) + after)
                self.assertIsNone(fc.fc_toolbug.record_issue("shop", url, at))
                self.assertEqual(self.log.read_text(), "".join(bad) + after)
        entry, kept = '{"at":"2026-10-17 15:30:00","project":"shop","message":"b"}\n', '{"at": "2026-10-17 18:00:00", "project": "shop", "issue": "https://kept/1"}\n'
        lines = bad[:4] + [entry, kept] + bad[4:] + ["{]"]
        self.log.write_text("".join(lines))
        self.assertEqual(fc.fc_toolbug.record_issue("shop", url, at), url)
        got = self.log.read_text().splitlines(keepends=True)
        self.assertEqual(got[:4] + got[5:], lines[:4] + lines[5:])
        self.assertEqual(json.loads(got[4]), {"at": at, "project": "shop", "message": "b", "issue": url})
        recorded = self.log.read_text()
        for project, at, want in (("shop", "2026-10-17 18:00:00", "https://kept/1"), ("shop", "2026-10-17 09:00:00", None), ("blog", at, None)):
            with self.subTest(project, at=at):
                self.assertEqual(fc.fc_toolbug.record_issue(project, "https://other/9", at), want)
                self.assertEqual(self.log.read_text(), recorded)

    def test_find_entry_matches_across_projects_and_within_two_seconds_and_refuses_several(self):
        def entry(project, at): return json.dumps({"at": at, "project": project, "message": "m"}) + "\n"
        find, at = fc.fc_toolbug.find_entry, "2026-10-21 19:26:41"
        self.assertEqual(find("shop", at), ("shop", at, None))  # no log at all: nothing matches
        self.log.parent.mkdir(parents=True)
        self.log.write_text(entry("shop", at) + entry("evidence", "2026-10-21 19:30:00") + entry("blog", "2026-10-21 19:40:00") + entry("cafe", "2026-10-21 19:40:00"))
        self.assertEqual(find("shop", at), ("shop", at, None))
        got = find("toolsmith", "2026-10-21 19:30:00")  # another project's entry at that exact time
        self.assertEqual(got[:2], ("evidence", "2026-10-21 19:30:00")); self.assertIn("evidence", got[2])
        for shown in ("2026-10-21 19:30:02", "2026-10-21 19:29:58", "2026-10-21 19:30:01"):  # watch can be off by a second
            self.assertEqual(find("toolsmith", shown)[:2], ("evidence", "2026-10-21 19:30:00"), shown)
        self.assertEqual(find("toolsmith", "2026-10-21 19:30:03"), ("toolsmith", "2026-10-21 19:30:03", None))
        for shown in ("2026-10-21 19:40:00", "2026-10-21 19:40:01"):  # two projects at one time: list both, pick none
            with self.assertRaises(ValueError) as cm: find("toolsmith", shown)
            self.assertIn("blog at 2026-10-21 19:40:00", str(cm.exception)); self.assertIn("cafe at 2026-10-21 19:40:00", str(cm.exception))
        self.log.write_text(entry("a", "2026-10-21 19:50:00") + entry("a", "2026-10-21 19:50:00") + entry("b", "2026-10-21 19:50:02"))
        with self.assertRaises(ValueError): find("c", "2026-10-21 19:50:01")
        with self.assertRaises(ValueError) as cm: find("a", "2026-10-21 19:50:00")  # two entries of one project at one time: name neither
        self.assertIn("2026-10-21 19:50:00#1", str(cm.exception)); self.assertIn("2026-10-21 19:50:00#2", str(cm.exception))
        self.assertEqual(find("a", "2026-10-21 19:50:00#2"), ("a", "2026-10-21 19:50:00#2", None))
        self.assertEqual(find("b", "2026-10-21 19:50:02#1"), ("b", "2026-10-21 19:50:02#1", None))  # a lone entry may be numbered too
        for shown in ("2026-10-21 19:50:00#3", "2026-10-21 19:50:00#0"):
            with self.assertRaises(ValueError) as cm: find("a", shown)
            self.assertIn("2 entries", str(cm.exception))
        self.assertEqual(find("c", "not a time"), ("c", "not a time", None))


class FileCommand(unittest.TestCase):
    """cmd_tool_bug: post on the maintainer's machine or with --approved, else save a draft; search before every create."""
    AT = "2026-10-17 15:30:00"
    RAW, URL = "# crew start: hangs at alice9 prompt\n## Summary\n\nIt hangs. token ghp_abcdefghijklmnopqrstuvwx\n", "https://github.com/prodoxx/compose/issues/2"

    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        self.drafts = self.home / ".compose" / "tool-bugs"
        for p in (mock.patch.object(fc.fc_crew, "TOOL_BUGS", self.home / ".compose" / "tool-bugs.jsonl"),
                  mock.patch.object(fc.fc_config, "CONFIG_PATH", self.home / ".compose" / "config.json"),
                  mock.patch.object(fc.fc_toolbug, "environment", return_value="- OS: test")):
            p.start(); self.addCleanup(p.stop)

    def run_cmd(self, state, raw=RAW, matches=(), url=URL, recorded=URL, **kw):
        (self.home / "b.md").write_text(raw)
        a = SimpleNamespace(**{**dict(repo=".", project="shop", action="file", body=str(self.home / "b.md"), comment=None, new=False, approved=False, entry=None), **kw})
        self.m = {n: mock.patch.object(fc.fc_toolbug, n, return_value=v).start() for n, v in dict(
            gh_status=(state, None if state == "no-gh" else "alice9"), open_matches=list(matches), post=url, record_issue=recorded).items()}
        self.addCleanup(mock.patch.stopall)
        status = 0
        with mock.patch("sys.stdout", io.StringIO()) as out:
            try: fc.fc_toolbug.cmd_tool_bug(a)
            except SystemExit as e: status = 1 if isinstance(e.code, str) else e.code
        return status, out.getvalue()

    def draft(self, out, login="alice9"):
        [path] = list(self.drafts.glob("*.md"))
        self.assertRegex(path.name, r"^\d{8}-\d{6}\.md$"); self.assertIn(str(path), out)
        title, body = fc.fc_toolbug.issue_text(self.RAW, login)
        self.assertEqual(path.read_text(), "# " + title + "\n\n" + body); self.assertNotIn("ghp_abc", path.read_text())
        return path

    def test_a_reporter_or_no_gh_saves_a_scrubbed_draft_and_posts_nothing(self):
        for state, kw in (("reporter", {}), ("no-gh", {}), ("no-gh", {"approved": True})):
            with self.subTest(state, **kw):
                for f in self.drafts.glob("*.md"): f.unlink()
                status, out = self.run_cmd(state, **kw)
                self.assertEqual(status, 0); self.m["post"].assert_not_called()
                path = self.draft(out, None if state == "no-gh" else "alice9")
                self.assertIn(f"--project shop --body {path} --approved", out); self.assertEqual("gh auth login" in out, state == "no-gh")
                self.assertNotIn("--entry", out)

    def test_the_draft_and_retry_commands_carry_the_entry_and_record_nothing(self):
        for state, url in (("reporter", self.URL), ("no-gh", self.URL), ("maintainer", None)):
            with self.subTest(state, url=url):
                for f in self.drafts.glob("*.md"): f.unlink()
                status, out = self.run_cmd(state, url=url, entry=self.AT)
                path = self.draft(out, None if state == "no-gh" else "alice9")
                self.assertEqual(status, 1 if url is None else 0); self.m["record_issue"].assert_not_called()
                [line] = [l for l in out.splitlines() if "--approved" in l]; self.assertNotIn("not recorded", out)
                for part in (f'--entry "{self.AT}"', "--project shop", f"--body {path}", "--approved"): self.assertIn(part, line)

    def test_matches_are_listed_and_exit_2_without_posting(self):
        status, out = self.run_cmd("maintainer", matches=[{"number": 7, "title": "crew start hangs", "url": "https://x/7"}], entry=self.AT)
        self.assertEqual(status, 2); self.m["post"].assert_not_called(); self.m["record_issue"].assert_not_called()
        self.assertNotIn("not recorded", out)
        self.assertIn("#7 crew start hangs https://x/7", out); self.assertIn("--comment", out); self.assertIn("--new", out)

    def test_post_prints_and_records_the_url(self):
        for state, kw, comment in (("maintainer", {}, None), ("maintainer", {"new": True}, None), ("reporter", {"approved": True, "comment": 3}, 3)):
            with self.subTest(**kw):
                status, out = self.run_cmd(state, matches=[{"number": 7, "title": "t", "url": "u"}] if kw else [], entry=self.AT, **kw)
                self.assertEqual((status, out.splitlines()), (0, [self.URL]))
                title, body = fc.fc_toolbug.issue_text(self.RAW, "alice9")
                self.assertEqual((self.m["post"].call_args.args[:2], self.m["post"].call_args.kwargs.get("comment")), ((title, body), comment))
                self.m["record_issue"].assert_called_once_with("shop", self.URL, self.AT)
                if comment: self.m["open_matches"].assert_not_called()

    def test_a_post_that_records_nothing_says_why_in_one_line(self):
        for entry, recorded, why in ((None, self.URL, "--entry"), (self.AT, None, self.AT), (self.AT, "https://kept/1", "https://kept/1")):
            with self.subTest(entry=entry, recorded=recorded):
                status, out = self.run_cmd("maintainer", entry=entry, recorded=recorded)
                self.assertEqual((status, out.splitlines()[0], len(out.splitlines())), (0, self.URL, 2))
                self.assertIn("not recorded", out.splitlines()[1]); self.assertIn(why, out.splitlines()[1])
                if entry is None: self.m["record_issue"].assert_not_called()
                else: self.m["record_issue"].assert_called_once_with("shop", self.URL, entry)

    def test_the_url_lands_on_another_projects_entry_a_second_off_and_ambiguity_posts_nothing(self):
        log = self.home / ".compose" / "tool-bugs.jsonl"; log.parent.mkdir(parents=True)
        log.write_text('{"at":"2026-10-21 19:26:40","project":"evidence","message":"m"}\n{"at":"2026-10-21 19:26:42","project":"other","message":"n"}\n')
        for n, v in dict(gh_status=("maintainer", "alice9"), open_matches=[], post=self.URL).items():
            p = mock.patch.object(fc.fc_toolbug, n, return_value=v); p.start(); self.addCleanup(p.stop)
        (self.home / "b.md").write_text(self.RAW)
        a = lambda at: SimpleNamespace(repo=".", project="toolsmith", action="file", body=str(self.home / "b.md"), comment=None, new=False, approved=False, entry=at)
        with mock.patch("sys.stdout", io.StringIO()) as out, self.assertRaises(SystemExit) as cm:
            fc.fc_toolbug.cmd_tool_bug(a("2026-10-21 19:26:41"))  # evidence and other are a second away each
        self.assertIn("other at 2026-10-21 19:26:42", cm.exception.code); self.assertNotIn(self.URL, out.getvalue())
        log.write_text('{"at":"2026-10-21 19:26:41","project":"evidence","message":"m"}\n')
        with mock.patch("sys.stdout", io.StringIO()) as out:
            fc.fc_toolbug.cmd_tool_bug(a("2026-10-21 19:26:42"))
        self.assertEqual(out.getvalue().splitlines()[0], self.URL); self.assertNotIn("not recorded", out.getvalue()); self.assertIn("evidence at 2026-10-21 19:26:41", out.getvalue())
        self.assertEqual(json.loads(log.read_text())["issue"], self.URL)

    def test_a_failed_post_saves_the_draft_and_exits_1(self):
        status, out = self.run_cmd("maintainer", url=None)
        self.assertEqual(status, 1); self.m["record_issue"].assert_not_called()
        self.assertIn(f"retry with: compose tool-bug file --project shop --body {self.draft(out)} --approved", out)

    def test_a_body_without_a_title_line_exits_1(self):
        self.assertEqual(self.run_cmd("maintainer", raw="no title\n")[0], 1); self.m["post"].assert_not_called()


class OwnWorktree(unittest.TestCase):
    """The toolsmith fixes on its own branch in its own worktree of the compose repo: never a main checkout, never the installed copy.
    Git's answers are faked: where each checkout lives (fc_core.checkout), its branch, its changes, its commits past origin."""
    URL, PR = "https://github.com/prodoxx/compose/issues/2", "https://github.com/prodoxx/compose/pull/5"
    AT = "2026-10-17 15:30:00"

    def commit(self, repo, name):
        self.subjects = [f"Fix {name}"]

    def where(self, path):
        here = Path(path).resolve()
        return next((self.sites[d] for d in (here, *here.parents) if d in self.sites), None)

    def sh(self, argv, cwd=None, *a, **kw):
        """Git as `pr` and `shared_checkout` ask it, answered by subcommand: the branch, the changes, the fetch, the log, the push."""
        sub, out, code = argv[1], "", 0
        if sub == "branch": out = self.branches.get(self.where(cwd)[0], "") + "\n"
        elif sub == "status": out = "M x\n" if self.dirty else ""
        elif sub == "fetch": code = self.fetch_code
        elif sub == "log": out = "\n".join(self.subjects)
        elif sub == "push": self.pushed.append(argv[-1])
        return subprocess.CompletedProcess(argv, code, out, "")

    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); self.root = root = Path(os.path.realpath(t.name))
        self.main, self.wt, self.other, self.home = root / "compose", root / "fix", root / "other", root / "home"
        self.src, self.side = self.home / ".compose" / "src", root / "side"
        for d in (self.main, self.wt, self.other, self.src, self.side, root / "elsewhere"): d.mkdir(parents=True)
        git_dir = lambda top, name: self.main / ".git" / "worktrees" / name if top in (self.wt, self.other) else top / ".git"
        self.sites = {d: fc.fc_core.Checkout((d, git_dir(d, d.name), self.main / ".git" if d in (self.main, self.wt, self.other) else d / ".git"))
                      for d in (self.main, self.wt, self.other, self.src, self.side)}
        self.branches = {self.main: "main", self.wt: "toolsmith-fix", self.other: "master", self.src: "feature", self.side: "side"}
        self.branches = {self.sites[d][0]: b for d, b in self.branches.items()}
        self.dirty, self.fetch_code, self.subjects, self.pushed = False, 0, [], []
        self.log = root / ".compose" / "tool-bugs.jsonl"; self.log.parent.mkdir()
        self.log.write_text(json.dumps({"at": self.AT, "project": "shop", "message": "boom", "issue": self.URL}) + "\n")
        for p in (mock.patch.dict(os.environ, {"HOME": str(self.home)}), mock.patch.object(fc.fc_crew, "CLI_REPO", self.main), mock.patch.object(fc.fc_core, "CLI_CHECKOUT", self.main),
                  mock.patch.object(fc.fc_crew, "TOOL_BUGS", self.log), mock.patch.object(fc.fc_config, "CONFIG_PATH", root / ".compose" / "config.json"),
                  mock.patch.object(fc.fc_core, "checkout", self.where), mock.patch.object(fc.fc_core, "sh", self.sh),
                  mock.patch.object(fc.fc_worktree, "default_branch", return_value="main")):
            p.start(); self.addCleanup(p.stop)
        self.gh_calls = []

    def gh(self, state="maintainer", code=0):
        """gh_status says `state`; `gh pr create` prints the PR URL (or fails with `code`)."""
        real = subprocess.run
        def run(cmd, *a, **kw):
            if cmd and cmd[0] == "gh":
                self.gh_calls.append(cmd)
                return subprocess.CompletedProcess(cmd, code, self.PR + "\n" if not code else "", "boom" if code else "")
            return real(cmd, *a, **kw)
        for p in (mock.patch.object(fc.fc_toolbug, "gh_status", return_value=(state, None if state == "no-gh" else "alice9")),
                  mock.patch("subprocess.run", run)):
            p.start(); self.addCleanup(p.stop)

    def pr(self, worktree=None, **kw):
        a = SimpleNamespace(**{**dict(project="shop", action="pr", worktree=str(worktree or self.wt), entry=None, title=None, issue=None), **kw})
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            try: fc.fc_toolbug.cmd_tool_bug(a); status = 0
            except SystemExit as e: status = 1 if isinstance(e.code, str) else e.code; out.write(str(e.code))
        return status, out.getvalue()

    def test_shared_checkout_names_the_installed_copy_main_checkouts_and_main_worktrees(self):
        sc = lambda p: fc.fc_worktree.shared_checkout(p, self.main)
        self.assertIn("a main checkout of the compose repo", sc(self.main))
        self.assertIsNone(sc(self.wt))  # its own worktree on its own branch
        (self.wt / "sub").mkdir(); self.assertIsNone(sc(self.wt / "sub"))
        self.assertIn("on master", sc(self.other))
        self.branches[self.wt] = ""; self.assertIn("on no branch", sc(self.wt))
        self.assertIsNone(sc(self.root / "elsewhere"))  # not the compose repo: none of this
        self.assertIsNone(sc(self.side))
        (self.src / "bin").mkdir()
        for p in (self.src, self.src / "bin"):
            self.assertIn("the installed copy of compose", sc(p))

    def test_the_edit_guard_lets_a_toolsmith_edit_only_its_own_worktree(self):
        gd = fc.fc_hooks.guard_decision
        why = gd("Edit", str(self.main / "bin" / "new" / "x.py"), "toolsmith")  # the directory does not exist yet
        self.assertIn("toolsmith", why); self.assertIn("own worktree", why)
        for tool in fc.fc_hooks.GUARDED_TOOLS:
            self.assertIsNone(gd(tool, str(self.wt / "x.py"), "toolsmith"), tool); self.assertIsNotNone(gd(tool, str(self.main / "x.py"), "toolsmith"), tool)
        self.assertIsNone(gd("Read", str(self.main / "a"), "toolsmith"))
        self.assertIsNone(gd("Write", str(self.root / "elsewhere" / "brief.md"), "toolsmith"))
        self.assertIsNone(gd("Edit", str(self.main / "a"), "crew"))  # other roles are not this guard's business
        self.assertIn("installed copy", gd("Write", str(self.src / "bin" / "compose.py"), "toolsmith"))

    def test_tool_bug_file_from_a_toolsmith_is_refused_in_a_shared_checkout_and_goes_through_in_its_own_worktree(self):
        (self.root / "b.md").write_text("# crew start: hangs\n## Summary\n\nIt hangs.\n")
        a = SimpleNamespace(project="shop", action="file", body=str(self.root / "b.md"), comment=None, new=False, approved=False, entry=None)
        self.gh("no-gh")
        mock.patch.object(fc.fc_toolbug, "environment", return_value="- OS: test").start(); self.addCleanup(mock.patch.stopall)
        for cwd, refused in ((self.main, True), (self.wt, False)):
            with self.subTest(cwd=cwd.name), mock.patch.dict(os.environ, {"COMPOSE_ROLE": "toolsmith"}), \
                    mock.patch.object(Path, "cwd", return_value=cwd), mock.patch("sys.stdout", io.StringIO()) as out:
                if refused:
                    with self.assertRaises(SystemExit) as cm: fc.fc_toolbug.cmd_tool_bug(a)
                    self.assertIn("never in a shared checkout", cm.exception.code)
                else:
                    fc.fc_toolbug.cmd_tool_bug(a); self.assertIn("saved", out.getvalue())
        with mock.patch.object(Path, "cwd", return_value=self.main), mock.patch("sys.stdout", io.StringIO()) as out:
            fc.fc_toolbug.cmd_tool_bug(a); self.assertIn("saved", out.getvalue())  # any other role files from anywhere

    def test_pr_pushes_the_branch_opens_the_pr_fixing_the_issue_and_records_it(self):
        self.commit(self.wt, "fix.py"); self.gh()
        status, out = self.pr()
        self.assertEqual((status, out.strip()), (0, self.PR))
        self.assertEqual(self.pushed, ["toolsmith-fix"])
        [call] = self.gh_calls
        self.assertEqual(call[:3], ["gh", "pr", "create"]); self.assertEqual(call[call.index("--repo") + 1], "prodoxx/compose")
        self.assertEqual(call[call.index("--head") + 1], "toolsmith-fix"); self.assertEqual(call[call.index("--base") + 1], "main")
        self.assertEqual(call[call.index("--title") + 1], "Fix fix.py"); self.assertIn("Fixes #2", call[call.index("--body") + 1])
        self.assertEqual(json.loads(self.log.read_text())["pr"], self.PR)
        status, out = self.pr(title="Better title"); self.assertEqual(status, 0)
        self.assertEqual(json.loads(self.log.read_text())["pr"], self.PR); self.assertEqual(self.gh_calls[-1][self.gh_calls[-1].index("--title") + 1], "Better title")

    def test_pr_fixes_the_issue_it_is_given_not_the_first_one_the_entry_links(self):
        self.commit(self.wt, "fix.py"); self.gh()
        status, out = self.pr(issue=9); self.assertEqual(status, 0)
        body = self.gh_calls[-1][self.gh_calls[-1].index("--body") + 1]
        self.assertIn("Fixes #9", body); self.assertNotIn("Fixes #2", body)
        self.assertEqual(json.loads(self.log.read_text())["issue"], self.URL)

    def test_pr_fixes_the_numbered_entrys_issue_when_several_share_one_time(self):
        self.commit(self.wt, "fix.py"); self.gh()
        other = {"at": self.AT, "project": "shop", "message": "second", "issue": "https://github.com/prodoxx/compose/issues/7"}
        self.log.write_text(self.log.read_text() + json.dumps(other) + "\n")
        status, out = self.pr(entry=self.AT); self.assertEqual(status, 1); self.assertIn(f"{self.AT}#1", out); self.assertEqual(self.gh_calls, [])
        status, out = self.pr(entry=f"{self.AT}#1"); self.assertEqual(status, 0)
        self.assertIn("Fixes #2", self.gh_calls[-1][self.gh_calls[-1].index("--body") + 1])
        first, second = map(json.loads, self.log.read_text().splitlines())
        self.assertEqual((first.get("pr"), second.get("pr")), (self.PR, None))
        status, out = self.pr(entry=f"{self.AT}#2"); self.assertEqual(status, 0)
        self.assertIn("Fixes #7", self.gh_calls[-1][self.gh_calls[-1].index("--body") + 1])
        self.assertEqual(json.loads(self.log.read_text().splitlines()[1])["pr"], self.PR)

    def test_pr_records_on_another_projects_entry_a_second_off_and_refuses_several_before_pushing(self):
        self.commit(self.wt, "fix.py"); self.gh()
        other = {"at": "2026-10-17 15:30:02", "project": "blog", "message": "x"}
        self.log.write_text(self.log.read_text() + json.dumps(other) + "\n")
        status, out = self.pr(project="toolsmith", entry="2026-10-17 15:30:01")  # shop and blog are a second away each
        self.assertEqual(status, 1); self.assertIn("shop at 2026-10-17 15:30:00", out); self.assertIn("blog at 2026-10-17 15:30:02", out)
        self.assertEqual(self.gh_calls, []); self.assertEqual(self.pushed, [])
        self.log.write_text(json.dumps({"at": "2026-10-17 15:30:00", "project": "evidence", "message": "m"}) + "\n")
        status, out = self.pr(project="toolsmith", entry="2026-10-17 15:30:01"); self.assertEqual(status, 0)
        self.assertEqual(out.splitlines()[0], self.PR); self.assertIn("evidence at 2026-10-17 15:30:00", out); self.assertNotIn("not recorded", out)
        self.assertEqual(json.loads(self.log.read_text())["pr"], self.PR)

    def test_pr_refuses_what_a_toolsmith_may_not_do_and_pushes_nothing(self):
        self.commit(self.wt, "fix.py"); self.gh()
        status, out = self.pr(self.main); self.assertEqual(status, 1); self.assertIn("a main checkout of the compose repo", out)
        status, out = self.pr(self.root / "elsewhere"); self.assertEqual(status, 1); self.assertIn("no worktree of the compose repo", out)
        self.dirty = True; status, out = self.pr(); self.assertEqual(status, 1); self.assertIn("uncommitted", out)
        self.dirty, self.subjects = False, []; status, out = self.pr(); self.assertEqual(status, 1); self.assertIn("no commit past origin/main", out)
        self.fetch_code = 1; self.commit(self.wt, "fix.py"); status, out = self.pr(); self.assertEqual(status, 1); self.assertIn("cannot reach", out)
        self.assertEqual(self.pushed, []); self.assertEqual(self.gh_calls, [])

    def test_pr_needs_a_maintainer_and_says_when_gh_cannot_open_it(self):
        self.commit(self.wt, "fix.py")
        for state in ("reporter", "no-gh"):
            self.gh(state); status, out = self.pr()
            self.assertEqual(status, 1); self.assertIn("tool-bug file", out); self.assertEqual(self.gh_calls, [])
        self.gh(code=1); status, out = self.pr()
        self.assertEqual(status, 1); self.assertIn("is pushed but gh could not open the PR", out); self.assertNotIn("pr", json.loads(self.log.read_text()))


class Cli(unittest.TestCase):
    """`compose tool-bug` and `compose toolsmith` through the real entry point."""

    def run_main(self, *args):
        with mock.patch.object(sys, "argv", ["compose", *args]), mock.patch.object(fc.fc_config, "apply_config", return_value="default"), \
                mock.patch.object(fc.fc_toolbug, "cmd_tool_bug") as bug, mock.patch.object(fc.fc_briefs, "cmd_toolsmith") as smith, \
                mock.patch("sys.stderr", io.StringIO()):
            fc.main()
        return bug, smith

    def test_tool_bug_passes_its_arguments(self):
        bug, smith = self.run_main("tool-bug", "file", "--project", "shop", "--body", "b.md", "--comment", "3")
        a = bug.call_args.args[0]
        self.assertEqual((a.action, a.project, a.body, a.comment, a.new, a.approved, a.entry), ("file", "shop", "b.md", 3, False, False, None))
        smith.assert_not_called()
        a = self.run_main("tool-bug", "file", "--project", "shop", "--body", "b.md", "--new", "--approved", "--entry", "2026-10-17 15:30:00")[0].call_args.args[0]
        self.assertEqual((a.comment, a.new, a.approved, a.entry), (None, True, True, "2026-10-17 15:30:00"))

    def test_tool_bug_usage_errors(self):
        self.run_main("tool-bug", "file", "--project", "shop", "--body", "b.md")[0].assert_called_once()
        for args in (["file", "--body", "b.md", "--comment", "3", "--new"], ["file"], ["post", "--body", "b.md"], ["file", "--body", "b.md", "--comment", "x"],
                     ["pr"], ["pr", "--body", "b.md"]):
            with self.subTest(args), self.assertRaises(SystemExit) as cm:
                self.run_main("tool-bug", "--project", "shop", *args)
            self.assertEqual(cm.exception.code, 2)
