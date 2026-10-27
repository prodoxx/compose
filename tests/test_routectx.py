import hermetic  # first: no test sees the real home
import hashlib, importlib.machinery, importlib.util, json, os, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_routectx  # noqa: E402

STORY = {"id": "ENG-9", "title": "Story", "description": "## Goal\ns\n\n## Acceptance criteria\n* greets\n* waves", "status": "Todo", "blockedBy": []}
BUG = "## Goal\nFix it.\n\n## Acceptance criteria\n* shows the name\n* keeps the case\n\n## Reproduction\nRun the driver.\ncase: greet-name\n\n## Notes\nx"


def issue(n, files, body="## Goal\nx", status="Todo", block="", **kv):
    desc = f"{body}\n\n```compose\nid: {n:03}\ntest: t\nfiles: {files}\n{block}```\n"
    return {"id": f"ENG-{n}", "title": f"T{n}", "description": desc, "status": status, "blockedBy": [], **kv}


FAKE_TRAIL = f"""#!{sys.executable}
import json, os, sys
out = json.load(open(os.environ["FAKE_TRAIL"])).get(" ".join(sys.argv[1:3]), 9)  # an unexpected call fails
if sys.argv[1] == "skeleton" and not os.path.exists(sys.argv[2]): out = 8  # run in the repo
sys.exit(out) if isinstance(out, int) else print(out)
"""


def ent(name, span):
    return {"name": name, "kind": "function", "span": span, "signature": f"def {name}()"}


def match(path, *hits):
    return {"symbol": {"name": "s", "path": path}, "hits": [{"path": h, "line": 1} for h in hits], "note": ""}


class Context(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.repo / "bin").mkdir()
        (self.repo / "bin" / "x.py").write_text("")
        (self.repo / "README.md").write_text("")
        (self.proj / "verify.json").write_text(json.dumps({"e2e": [{"name": "greet-name"}, {"name": "seen"}]}))
        cases = [{"name": "greet-name", "verdict": "feature_missing"}, {"name": "seen", "verdict": "passed"}, {"name": "ghost", "verdict": "feature_missing"}]
        (self.proj / "results" / "prove.json").write_text(json.dumps({"cases": cases}))

    def ctx(self, mine, *others):
        issues = {i["id"]: i for i in (STORY, mine, *others)}
        tasks = [fc.fc_tasks.task_from_issue(i) for i in (mine, *others)]
        return fc_routectx.route_context(self.repo, self.proj, tasks[0], tasks, issues)

    def test_whole_context(self):
        mine = issue(1, "bin/x.py, README.md, src/new.py", BUG, block="route: implement-then-test\n", parentId="ENG-9", labels=["bug"])
        others = [issue(2, "z.py, bin/x.py"), issue(3, "a.py, z.py", "## Goal\nx", "In Progress"), issue(4, "done.py", status="Done"),
                  issue(5, "gone.py", status="Canceled"), issue(6, "gone2.py", status="cancelled")]
        self.assertEqual(self.ctx(mine, *others), {
            "task": "T1", "criteria": ["shows the name", "keeps the case"],
            "files": [{"path": "bin/x.py", "top": "bin", "exists": True}, {"path": "README.md", "top": ".", "exists": True},
                      {"path": "src/new.py", "top": "src", "exists": False}],
            "other_files": ["a.py", "bin/x.py", "z.py"], "story": {"title": "Story", "criteria": ["greets", "waves"]},
            "bug": {"label": True, "documented": True, "confirmed": True}, "pin": "implement-then-test", "check": "t", "test": None,
            "signals": {"code_files": 2, "doc_files": 1, "new_files": ["src/new.py"], "existing_code_files": [{"path": "bin/x.py", "lines": 0}], "criteria": 2}})

    def test_signals_count_docs_apart_from_code_and_size_the_existing_code(self):
        (self.repo / "bin" / "x.py").write_text("a\nb\n\nc\n")
        got = self.ctx(issue(1, "bin/x.py, README.md, notes.txt, src/new.py, bin", "## Goal\nx\n\n## Acceptance criteria\n* one"))["signals"]
        self.assertEqual(got, {"code_files": 3, "doc_files": 2, "new_files": ["notes.txt", "src/new.py"], "criteria": 1,
                               "existing_code_files": [{"path": "bin/x.py", "lines": 4}]})

    def test_bug_and_story_holdouts(self):
        for body, bug in [("## Goal\nx", (False, False)), ("## Reproduction\n\n   \n## Notes\ncase: greet-name", (False, False)),
                          ("## Reproduction\ncase: seen", (True, False)), ("## Reproduction\ncase: ghost", (True, False))]:
            got = self.ctx(issue(1, "a.py", body, parentId="ENG-404", labels=["Feature"]))
            self.assertEqual(got["bug"], {"label": False, "documented": bug[0], "confirmed": bug[1]}, body)
            self.assertEqual((got["story"], got["pin"], got["other_files"]), (None, None, []))
        for name in ("results/prove.json", "verify.json"):
            (self.proj / name).unlink()
            self.assertEqual(self.ctx(issue(1, "a.py", BUG))["bug"], {"label": False, "documented": True, "confirmed": False}, name)


class CheckOnlyContext(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)

    def ctx(self, mine):
        meta = fc.fc_tasks.task_from_issue(mine)
        return fc_routectx.route_context(self.repo, self.proj, meta, [meta], {mine["id"]: mine})

    def whole(self, check, pin=None, test=None):
        return {"task": "T1", "criteria": [], "files": [{"path": "bin/a.py", "top": "bin", "exists": False}], "other_files": [],
                "story": None, "bug": {"label": False, "documented": False, "confirmed": False}, "pin": pin, "check": check, "test": test,
                "signals": {"code_files": 1, "doc_files": 0, "new_files": ["bin/a.py"], "existing_code_files": [], "criteria": 0}}

    def test_a_task_with_no_protected_test_carries_its_check(self):
        self.assertEqual(self.ctx(issue(1, "bin/a.py")), self.whole("t"))

    def test_a_task_with_a_protected_test_carries_no_check(self):
        self.assertEqual(self.ctx(issue(1, "bin/a.py", block="protected: tests/test_a.py\n")), self.whole(None, test={"protected": ["tests/test_a.py"], "command": "t"}))

    def test_holdouts(self):
        self.assertIsNone(self.ctx(issue(1, "bin/a.py", block="protected: tests/test_a.py, tests/test_b.py\n"))["check"])
        self.assertEqual(self.ctx(issue(1, "bin/a.py", block="route: check-only\n")), self.whole("t", "check-only"))
        self.assertEqual(self.ctx(issue(1, "bin/a.py", block="protected:\n"))["check"], "t")
        self.assertIsNone(self.ctx(issue(1, "bin/a.py", block="protected:\n"))["test"])


class Dependencies(unittest.TestCase):
    def setUp(self):
        self.repo, self.bin = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (self.repo / "bin").mkdir()
        for f in ("bin/a.py", "bin/b.py", "c.py"): (self.repo / f).write_text("x\n")
        (self.bin / "trail").write_text(FAKE_TRAIL)
        (self.bin / "trail").chmod(0o755)
        self.canned = {  # inner sits inside Outer; L22-L100 does not, though as text it would
            "skeleton bin/a.py": {"entries": [ent("Outer", "L1-L20"), ent("inner", "L3-L5"), ent("helper", "L22-L100")]},
            "callers Outer": {"matches": [match("bin/a.py", "z.py", "bin/b.py", "m.py", "z.py", "nope.py"), match("other.py", "o.py")]},
            "callers helper": {"matches": [match("bin/a.py", "h.py")]},
            "skeleton bin/b.py": {"entries": [ent("Bee", "L1-L2")]},
            "callers Bee": {"matches": [match("bin/b.py", *[f"c{i:02}.py" for i in range(24, -1, -1)], "c03.py")]},
            "skeleton c.py": {"entries": [ent("Cee", "L1-L1")]},
            "callers Cee": {"matches": [match("bin/a.py", "q.py")]}}

    def deps(self, over={}, files=("bin/a.py", "nope.py", "bin/b.py", "c.py"), path=None):
        canned = {k: v if isinstance(v, (int, str)) else json.dumps(v) for k, v in {**self.canned, **over}.items()}
        (self.bin / "canned.json").write_text(json.dumps(canned))
        env = {"FAKE_TRAIL": str(self.bin / "canned.json"), "PATH": path or f"{self.bin}{os.pathsep}{os.environ['PATH']}"}
        with mock.patch.dict(os.environ, env):
            return fc_routectx.dependency_context(self.repo, {"id": "001", "files": list(files)})

    def test_callers(self):
        self.assertEqual(self.deps(), {"callers": {"bin/a.py": ["h.py", "m.py", "z.py"], "bin/b.py": [f"c{i:02}.py" for i in range(20)], "c.py": []}})
        self.assertEqual(self.deps(files=["nope.py", "gone/x.py"]), {"callers": {}})

def sha16(blob_list, body="## Goal\nGrüße", files=("a.py", "src/b.py", "new.py"), policies=()):
    text = json.dumps([body, list(files), blob_list, list(policies)], sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Hash(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.blobs = {}  # path -> blob id at HEAD, what `git rev-parse HEAD:<path>` answers
        self.commit({"a.py": "a1", "src/b.py": "b1"})
        self.meta = {"id": "001", "title": "T", "body": "## Goal\nGrüße", "files": ["a.py", "src/b.py", "new.py"], "route": "test-first"}
        self.pols = [{"name": "Gate", "statement": "s", "globs": ["a.py"], "checks": ["c"]}]
        p = mock.patch.object(fc_routectx.subprocess, "run", side_effect=self.rev_parse); p.start(); self.addCleanup(p.stop)

    def rev_parse(self, cmd, **kw):
        blob = self.blobs.get(cmd[-1].removeprefix("HEAD:"))
        return SimpleNamespace(returncode=0 if blob else 128, stdout=(blob or "") + "\n")

    def commit(self, blobs): self.blobs.update(blobs)

    def h(self, meta=None, pols=None):
        return fc_routectx.context_hash(self.repo, meta or self.meta, self.pols if pols is None else pols)

    def test_changes_with_each_input(self):
        base = self.h()
        self.assertEqual(base, sha16(["a1", "b1", "-"], policies=self.pols))  # a file HEAD does not have counts as "-"
        self.assertEqual(self.h({**self.meta, "id": "002", "title": "U", "route": None}), base)  # only body and files count
        changed = [self.h({**self.meta, "body": "## Goal\nother"}), self.h({**self.meta, "files": ["a.py", "src/b.py"]}),
                   self.h({**self.meta, "files": ["src/b.py", "a.py", "new.py"]}), self.h(pols=[]), self.h(pols=[{**self.pols[0], "checks": ["d"]}])]
        self.commit({"a.py": "a2"})
        changed.append(self.h())
        self.commit({"a.py": "a1", "new.py": "n1"})  # only new.py differs from base now
        changed.append(self.h())
        self.assertNotIn(base, changed)
        self.assertEqual(len(set(changed)), len(changed))
