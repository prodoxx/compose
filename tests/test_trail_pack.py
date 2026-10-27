import hermetic  # first: no test sees the real home
import os, stat, subprocess, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from tests.test_trail_setup import fc, repo_with, FakeTrail
os.environ["HERDR_ENV"] = ""


class TrailPack(FakeTrail):
    """A pack from trail wherever the graph sits (.trail/cache/ or an old graft/), without trail's notes; graft never runs."""
    def setUp(self):
        super().setUp()
        graft = self.log.parent / "graft"
        graft.write_text('#!/bin/sh\necho "$*" >> "$TRAIL_LOG.graft"\n'); graft.chmod(stat.S_IRWXU)
        self.addCleanup(lambda: self.assertFalse(Path(f"{self.log}.graft").exists(), "graft was started"))

    def repo_on(self, graph):
        repo, _ = repo_with({"server/main.go": "package main\n"})
        (repo / graph).mkdir(parents=True)
        (repo / graph / "INDEX.md").write_text("index")
        return repo

    def assert_pack(self, pack, query):
        self.assertEqual(pack, f'trail ask — "{query}"  (lexical)\n\n1. main · function  [symbol]\n   server/main.go:L1-L1')

    def test_a_graph_in_trail_cache_or_old_graft_gives_a_pack_without_banners(self):
        self.assert_pack(fc.fc_implement.fc_trailpack.trail_pack(self.repo_on(".trail/cache"), "main"), "main")
        self.assert_pack(fc.fc_implement.fc_trailpack.trail_pack(self.repo_on("graft"), "entry point"), "entry point")
        self.assertTrue(self.calls()[-1].startswith("ask entry point --source -n 6 --no-refresh "))

    def test_a_task_clone_gets_the_graph_rebuilt_in_trail_cache_and_keeps_it_out_of_git(self):
        for graph in (".trail/cache", "graft"):
            repo = self.repo_on(graph)
            clone = Path(tempfile.mkdtemp()) / "clone"
            subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
            fc.fc_worktree.ready_checkout(repo, clone)
            self.assert_pack(fc.fc_implement.fc_trailpack.trail_pack(clone, "entry point", repo=repo, limit=3), "entry point")
            self.assertTrue((clone / ".trail" / "cache" / "INDEX.md").exists())  # .trail/cache/ made before the build
            self.assertEqual((clone / "graft").exists(), graph == "graft")
            self.assertEqual(self.calls()[-2:], [f"build --no-gitignore --no-ignore {clone}", f"ask entry point --source -n 3 --no-refresh {clone}"])
            self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True).stdout, "")

    def test_no_graph_or_a_failing_ask_gives_no_pack(self):
        repo, _ = repo_with({"a.txt": "x\n"})
        self.assertEqual((fc.fc_implement.fc_trailpack.trail_pack(repo, "main", repo=repo), self.calls()), ("", []))
        failed = SimpleNamespace(returncode=1, stdout='trail ask — "main"  (lexical)\n')
        with mock.patch.object(fc.fc_core, "sh", return_value=failed):
            self.assertEqual(fc.fc_implement.fc_trailpack.trail_pack(self.repo_on("graft"), "main"), "")

class PackCap(FakeTrail):
    """Task 012 (NEB-950): trail_pack cuts its output at `cap`, 12000 by default."""

    def test_the_pack_is_cut_at_cap_characters_and_12000_by_default(self):
        repo, _ = repo_with({".trail/cache/INDEX.md": "index"})
        out = "".join(f"{i:05d} code line\n" for i in range(2000))  # 32,000 characters
        with mock.patch.object(fc.fc_core, "sh", return_value=SimpleNamespace(returncode=0, stdout=out)):
            for cap, want in [(500, 500), (15000, 15000), (None, 12000)]:
                pack = fc.fc_implement.fc_trailpack.trail_pack(repo, "q") if cap is None else fc.fc_implement.fc_trailpack.trail_pack(repo, "q", cap=cap)
                self.assertEqual(pack, out[:want])
