import hermetic  # first: no test sees the real home
import io, json, os, shutil, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
from cleanup_kit import CLI, ENV, FakeLinear, fc, repo
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"  # these cover the plain-worktree path, not the treehouse pool
os.environ["HERDR_ENV"] = ""


def own_file_commit(run, cwd, base, name, msg):
    """A commit on `base` that adds one file of its own, so merging it into base changes base's tree."""
    blob = run("hash-object", "-w", "/dev/null", cwd=cwd).stdout.strip()
    entries = run("ls-tree", f"{base}^{{tree}}", cwd=cwd).stdout + f"100644 blob {blob}\t{name}\n"
    tree = subprocess.run(["git", "mktree"], cwd=cwd, input=entries, check=True, capture_output=True, text=True).stdout.strip()
    return run("commit-tree", tree, "-p", base, "-m", msg, cwd=cwd).stdout.strip()


class ProjectBranch(unittest.TestCase):
    """A project's branch is stored in its verify.json: init --branch names it, --after and clean follow it, init --branch again renames it."""
    def setUp(self):
        self.r, self.run = repo()
        self.env = {**os.environ, **ENV}

    def cli(self, *a):
        return subprocess.run([CLI, *a, "--repo", str(self.r)], check=True, capture_output=True, text=True, env=self.env)

    def branches(self):
        return self.run("branch", "--format=%(refname:short)").stdout.split()

    def test_custom_branch_is_created_stored_renamed_built_on_and_cleaned(self):
        self.cli("init", "--project", "p", "--branch", "js-1-p")
        wt = self.r.parent / "compose-p"
        self.assertIn("js-1-p", self.branches())
        self.assertNotIn("compose/p", self.branches())
        self.assertEqual(json.loads((wt / ".compose" / "p" / "verify.json").read_text())["branch"], "js-1-p")
        self.cli("init", "--project", "q", "--after", "p")  # resolves p's stored branch, not compose/p
        qcfg = json.loads((self.r.parent / "compose-q" / ".compose" / "q" / "verify.json").read_text())
        self.assertEqual((qcfg["review_base"], qcfg["after"]), ("js-1-p", "p"))
        self.cli("init", "--project", "p", "--branch", "js-2-p")  # an existing project moves to the new name
        self.assertEqual(self.run("branch", "--show-current", cwd=wt).stdout.strip(), "js-2-p")
        self.assertNotIn("js-1-p", self.branches())
        self.assertEqual(json.loads((self.r.parent / "compose-q" / ".compose" / "q" / "verify.json").read_text())["review_base"], "js-2-p")
        self.cli("clean", "--project", "q", "--delete-unmerged")
        self.cli("clean", "--project", "p", "--delete-unmerged")
        self.assertEqual(self.branches(), ["main"])

    def test_a_project_without_a_stored_branch_keeps_compose_slash_slug(self):
        self.cli("init", "--project", "old")
        f = self.r.parent / "compose-old" / ".compose" / "old" / "verify.json"
        f.write_text(json.dumps({k: v for k, v in json.loads(f.read_text()).items() if k != "branch"}))
        self.assertEqual(fc.fc_core.project_branch(f.parent), "compose/old")
        self.assertIn("compose/old", self.branches())

    def config(self, cfg):
        f = Path(self.env["COMPOSE_CONFIG"]); f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cfg))

    def test_init_names_and_stores_the_project_branch_from_branch_prefix(self):
        ws = {"branch_prefix": "top/", "workspace": "w", "workspaces": {"w": {"branch_prefix": "ws/"}}}
        for cfg, args, want in (({}, (), "compose/p"), ({"branch_prefix": ""}, (), "p"), (ws, (), "ws/p")):
            with self.subTest(cfg=cfg, args=args):
                self.r, self.run = repo()
                self.config(cfg)
                self.cli("init", "--project", "p", *args)
                wt = self.r.parent / "compose-p"
                self.assertEqual(self.run("branch", "--show-current", cwd=wt).stdout.strip(), want)
                self.assertEqual(json.loads((wt / ".compose" / "p" / "verify.json").read_text())["branch"], want)

    def test_after_a_project_with_no_record_builds_on_its_prefixed_branch(self):
        self.config({"branch_prefix": "team/"})
        self.run("branch", "team/x")
        self.cli("init", "--project", "q", "--after", "x")
        self.assertEqual(json.loads((self.r.parent / "compose-q" / ".compose" / "q" / "verify.json").read_text())["review_base"], "team/x")

    def test_clean_removes_only_its_own_merged_branches_then_drops_the_task_prefix(self):
        self.cli("init", "--project", "p")
        wt = self.r.parent / "compose-p"; f = wt / ".compose" / "p" / "verify.json"
        git = lambda *a: self.run("-c", "user.name=t", "-c", "user.email=t@t", *a, cwd=wt).stdout.strip()
        (wt / "f.txt").write_text("f\n"); git("add", "f.txt"); git("commit", "-qm", "f")  # unmerged project: its worktree stays
        for b in ("compose/p-001", "compose/p-tests-001", "compose/p-002-bench-x", "compose/p-layer-01", "compose/p-2", "compose/p-2-001", "compose/p-2-tests-001"):
            git("branch", b, "compose/p")
        git("branch", "compose/p-003", own_file_commit(self.run, wt, "main", "p003.txt", "unmerged"))
        f.write_text(json.dumps({**json.loads(f.read_text()), "task_prefix": "compose/p-"}))
        self.cli("clean", "--project", "p")
        self.assertEqual(sorted(b for b in self.branches() if b.startswith("compose/p-")), ["compose/p-003", "compose/p-2", "compose/p-2-001", "compose/p-2-tests-001"])
        self.assertEqual(json.loads(f.read_text())["task_prefix"], "compose/p-")
        git("branch", "-D", "compose/p-003")
        self.cli("clean", "--project", "p")
        self.assertNotIn("task_prefix", json.loads(f.read_text()))



class LocallyMergedLayers(unittest.TestCase):
    """main fast-forwarded to a layer built locally: the layer holds copies of the task commits, and no layers.json says whose."""
    def test_clean_counts_tasks_whose_patches_are_in_main_merged_and_moves_linear(self):
        r, run = repo()
        subprocess.run([CLI, "init", "--project", "p", "--repo", str(r)], check=True, capture_output=True, env={**os.environ, **ENV})
        wt = r.parent / "compose-p"; proj = wt / ".compose" / "p"
        def commit(f, cwd=wt):
            (cwd / f).parent.mkdir(exist_ok=True); (cwd / f).write_text(f"{f}\n")
            run("add", f, cwd=cwd); run("commit", "-qm", f, cwd=cwd)
            return run("rev-parse", "HEAD", cwd=cwd).stdout.strip()
        tests = [commit("tests/t001.txt"), commit("tests/t002.txt")]
        run("checkout", "-q", "-b", "fast/p/001", cwd=wt); one = commit("src001.txt")
        run("checkout", "-q", "-b", "fast/p/002", cwd=wt); two = commit("src002.txt")
        run("checkout", "-q", "compose/p", cwd=wt)
        run("checkout", "-q", "-b", "fast/p/layer-01")  # each task's test sits next to it, so every pick is a new commit
        for c in (tests[0], one, tests[1], two):
            run("cherry-pick", c)
        run("checkout", "-q", "main"); run("merge", "-q", "--ff-only", "fast/p/layer-01")
        tasks = [{"id": t, "title": t, "files": [], "protected": [], "linear": l, "after": ""} for t, l in (("001", "ENG-3"), ("002", "ENG-4"))]
        (proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        (proj / "linear.json").write_text('{"feature": "ENG-1"}')
        linear = FakeLinear(ENG_1=("In Review", None), ENG_2=("In Review", "ENG-1"), ENG_3=("In Review", "ENG-2"), ENG_4=("In Review", "ENG-2"))
        with mock.patch.dict(os.environ, ENV), mock.patch("sys.stdout", io.StringIO()), \
                mock.patch.object(fc.fc_linear, "linear_gql", linear), mock.patch.object(fc.fc_config, "LINEAR_KEY", "k"):
            fc.fc_clean.clean_project(r, "p", wt, proj, explicit=True)
        self.assertEqual(run("branch", "--format=%(refname:short)").stdout.split(), ["main"])
        self.assertFalse(wt.exists())
        self.assertEqual(linear.states("ENG-3", "ENG-4", "ENG-2", "ENG-1"), ["Done", "Done", "Done", "Done"])


class UnsavedClone(unittest.TestCase):
    def test_clean_names_a_clone_a_failed_pause_snapshot_kept_before_it_removes_it(self):
        r, run = repo()
        subprocess.run([CLI, "init", "--project", "p", "--repo", str(r)], check=True, capture_output=True, env={**os.environ, **ENV})
        wt = r.parent / "compose-p"; proj = wt / ".compose" / "p"
        aside = proj / "worktrees" / "001-unsaved-20261026-101010"
        aside.mkdir(parents=True); (aside / "a.txt").write_text("half\n")
        out = io.StringIO()
        with mock.patch.dict(os.environ, ENV), mock.patch("sys.stdout", out):
            fc.fc_clean.clean_project(r, "p", wt, proj, explicit=True)
        self.assertIn(f"clean  p: removing {aside}, the clone a failed pause snapshot kept with unsaved edits", out.getvalue())
        self.assertFalse(aside.exists())


class SquashMergedLayers(unittest.TestCase):
    """Layers are squash-merged by PR: task branches were never PRs and ancestry is rewritten, so the merged layer that held a task proves it."""
    @classmethod
    def setUpClass(cls):
        cls.r, run = repo()
        cls.git = staticmethod(run)  # not `run`: TestCase.run is the runner
        cls.root = cls.r.parent
        cls.template = Path(tempfile.mkdtemp()).resolve() / "template"
        cls.addClassCleanup(shutil.rmtree, cls.root, True)
        cls.addClassCleanup(shutil.rmtree, cls.template.parent, True)
        cls.bin = cls.root / "bin"; cls.bin.mkdir()
        (cls.bin / "gh").write_text('''#!/usr/bin/env python3
import json, sys
a = sys.argv[1:]
merged = open(__file__ + ".merged").read().split()
head = a[a.index("--head") + 1] if "--head" in a else ""
print(json.dumps([{"number": 7, "mergedAt": "2026-10-15T00:00:00Z"}] if head in merged else []))
''')
        (cls.bin / "gh").chmod(0o755)
        (cls.bin / "gh.merged").write_text("fast/p/layer-01\n")
        cls.env = {**os.environ, **ENV, "PATH": f"{cls.bin}:{os.environ['PATH']}"}
        remote = cls.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        run("remote", "add", "origin", str(remote)); run("push", "-q", "origin", "main")
        subprocess.run([CLI, "init", "--project", "p", "--repo", str(cls.r)], check=True, capture_output=True, env=cls.env)
        proj = cls.root / "compose-p" / ".compose" / "p"
        tip = run("rev-parse", "main").stdout.strip()
        for b in ("fast/p/001", "fast/p/002", "fast/p/layer-01", "fast/p/layer-02"):  # commits on top of main: never ancestors of it
            run("branch", b, own_file_commit(run, cls.r, tip, b.replace("/", "-") + ".txt", b))
        run("push", "-q", "origin", "fast/p/layer-01", "fast/p/layer-02")
        (proj / "layers.json").write_text(json.dumps({"fast/p/layer-01": ["001"], "fast/p/layer-02": ["002"]}))
        shutil.copytree(cls.root, cls.template, symlinks=True)

    def setUp(self):
        shutil.rmtree(self.root)
        shutil.copytree(self.template, self.root, symlinks=True)
        self.r, self.run, self.bin, self.env = self.root / "repo", self.git, type(self).bin, type(self).env
        self.proj = self.root / "compose-p" / ".compose" / "p"

    def test_clean_drops_tasks_of_a_squash_merged_layer_and_its_remote_branch_and_keeps_the_rest(self):
        out = subprocess.run([CLI, "clean", "--project", "p", "--repo", str(self.r)], capture_output=True, text=True, env=self.env).stdout
        left = self.run("branch", "--format=%(refname:short)").stdout.split()
        self.assertNotIn("fast/p/001", left); self.assertNotIn("fast/p/layer-01", left)
        self.assertIn("fast/p/002", left); self.assertIn("fast/p/layer-02", left)
        remote = self.run("ls-remote", "--heads", "origin").stdout
        self.assertNotIn("fast/p/layer-01", remote); self.assertIn("fast/p/layer-02", remote)
        self.assertIn("deleted origin/fast/p/layer-01", out)
        self.assertIn("kept unmerged: fast/p/002, fast/p/layer-02", out)
        (self.bin / "gh.merged").write_text("fast/p/layer-01\nfast/p/layer-02\n")  # the last layer merges: everything goes
        subprocess.run([CLI, "clean", "--project", "p", "--repo", str(self.r)], capture_output=True, text=True, env=self.env)
        self.assertEqual(self.run("branch", "--format=%(refname:short)").stdout.split(), ["main"])
        self.assertFalse((self.r.parent / "compose-p").exists())
        self.assertEqual(self.run("ls-remote", "--heads", "origin").stdout.count("fast/p/"), 0)

    def linear_project(self):
        """Tasks 001 and 002 with issues under story ENG-2 of feature ENG-1, and 003, canceled and re-split, whose branch sits on main."""
        tasks = [{"id": t, "title": t, "files": [], "protected": [], "linear": l, "after": ""} for t, l in (("001", "ENG-3"), ("002", "ENG-4"), ("003", "ENG-5"))]
        (self.proj / "results").mkdir(exist_ok=True)
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {}, "tasks": tasks}))
        (self.proj / "linear.json").write_text('{"feature": "ENG-1"}')
        self.run("branch", "fast/p/003", "main")

    def clean_here(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env), mock.patch("sys.stdout", out):
            fc.fc_clean.clean_project(self.r, "p", self.r.parent / "compose-p", self.proj, explicit=True)
        return out.getvalue()

    def test_clean_moves_the_tasks_it_proves_merged_to_done_and_rolls_the_story_and_the_feature_up(self):
        self.linear_project()
        linear = FakeLinear(ENG_1=("In Review", None), ENG_2=("In Review", "ENG-1"), ENG_3=("In Review", "ENG-2"), ENG_4=("In Review", "ENG-2"), ENG_5=("Canceled", "ENG-2"))
        with mock.patch.object(fc.fc_linear, "linear_gql", linear), mock.patch.object(fc.fc_config, "LINEAR_KEY", "k"):
            out = self.clean_here()  # layer-01 merged: 001 is Done, 003 stays Canceled, the story waits for 002, the feature for the project branch
            self.assertEqual(linear.states("ENG-3", "ENG-4", "ENG-5", "ENG-2", "ENG-1"), ["Done", "In Review", "Canceled", "In Review", "In Review"])
            self.assertIn("linear ENG-3 -> Done", out)
            (self.bin / "gh.merged").write_text("fast/p/layer-01\nfast/p/layer-02\n")
            self.clean_here()  # every layer and the project branch merged: the story and the feature are Done too
            self.assertEqual(linear.states("ENG-3", "ENG-4", "ENG-5", "ENG-2", "ENG-1"), ["Done", "Done", "Canceled", "Done", "Done"])
        self.assertFalse((self.r.parent / "compose-p").exists())

    def test_with_task_branches_gone_merged_layers_move_their_tasks_story_and_feature_to_done(self):
        self.linear_project()
        wt = self.r.parent / "compose-p"
        (wt / "stub.txt").write_text("groundwork\n")  # the project branch's own commit: only its layers prove it merged
        self.run("add", "stub.txt", cwd=wt); self.run("commit", "-qm", "groundwork", cwd=wt)
        self.run("branch", "-D", "fast/p/001", "fast/p/002")
        (self.bin / "gh.merged").write_text("fast/p/layer-01\nfast/p/layer-02\n")
        linear = FakeLinear(ENG_1=("In Review", None), ENG_2=("In Review", "ENG-1"), ENG_3=("In Review", "ENG-2"), ENG_4=("In Progress", "ENG-2"), ENG_5=("Canceled", "ENG-2"))
        with mock.patch.object(fc.fc_linear, "linear_gql", linear), mock.patch.object(fc.fc_config, "LINEAR_KEY", "k"):
            out = self.clean_here()
        self.assertIn("every layer built from it is merged", out)
        self.assertEqual(linear.states("ENG-3", "ENG-4", "ENG-5", "ENG-2", "ENG-1"), ["Done", "Done", "Canceled", "Done", "Done"])

    def merged_ids(self):
        with mock.patch.object(fc.fc_linear, "linear_merged") as lm:
            self.clean_here()
        _, metas, project_merged = lm.call_args.args
        return [m["id"] for m in metas], project_merged

    def test_a_task_held_by_any_layer_not_merged_is_not_counted(self):
        self.linear_project()
        self.run("branch", "-D", "fast/p/001", "fast/p/002")
        (self.proj / "layers.json").write_text(json.dumps({"fast/p/layer-01": ["001", "002"], "fast/p/layer-02": ["002"]}))
        ids, project_merged = self.merged_ids()  # only layer-01 is merged: 002 waits for layer-02
        self.assertEqual(sorted(ids), ["001", "003"])
        self.assertFalse(project_merged)



class ProjectBranchNamedFirst(unittest.TestCase):
    """The summary line of a removed project branch names the branch first, then why clean removed it, whatever the why."""
    def summary(self, branch, land, delete_unmerged=False):
        """Init project p on `branch` with one commit of its own, let `land` move main, clean, and return clean's lines and the worktree."""
        r, run = repo()
        subprocess.run([CLI, "init", "--project", "p", "--branch", branch, "--repo", str(r)], check=True, capture_output=True, env={**os.environ, **ENV})
        wt = r.parent / "compose-p"; proj = wt / ".compose" / "p"
        (wt / "a.txt").write_text("base\nx\n"); run("commit", "-qam", "groundwork", cwd=wt)
        land(r, run, proj)
        out = io.StringIO()
        with mock.patch.dict(os.environ, ENV), mock.patch("sys.stdout", out):
            fc.fc_clean.clean_project(r, "p", wt, proj, explicit=True, delete_unmerged=delete_unmerged)
        return out.getvalue().splitlines(), wt

    def same_change_with_more(self, r, run, proj):  # main holds the groundwork's change inside a bigger patch
        (r / "a.txt").write_text("base\nx\n"); (r / "b.txt").write_text("y\n"); run("add", "-A"); run("commit", "-qm", "more")

    def test_each_why_reads_after_the_branch_name(self):
        for branch, land, why, delete_unmerged in (
                ("compose/p", lambda r, run, proj: run("cherry-pick", "-x", "compose/p"), "contained in main by patch", False),
                ("compose/p", self.same_change_with_more, "merging it into main changes nothing", False),
                ("compose/p", lambda r, run, proj: None, "UNMERGED, removed on --delete-unmerged", True)):
            with self.subTest(why=why, branch=branch):
                lines, wt = self.summary(branch, land, delete_unmerged)
                want = f"clean  p: task branches all merged and removed; project branch {branch}: {why}; removed worktree {wt} and branch {branch}"
                self.assertIn(want, lines, lines)
                self.assertFalse(wt.exists())


if __name__ == "__main__":
    unittest.main()
