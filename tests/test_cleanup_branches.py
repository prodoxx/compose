import hermetic  # first: no test sees the real home
import json, os, tempfile, unittest
from pathlib import Path
from cleanup_kit import fc
os.environ["HERDR_ENV"] = ""


class ProjectBranch(unittest.TestCase):
    """A project's branch is stored in its verify.json; without one it is compose/<name>."""
    def proj(self, name, cfg):
        p = Path(tempfile.mkdtemp()) / name; p.mkdir()
        if cfg is not None: (p / "verify.json").write_text(json.dumps(cfg))
        return p

    def test_a_stored_branch_is_the_project_branch(self):
        self.assertEqual(fc.fc_core.project_branch(self.proj("p", {"branch": "team/p"})), "team/p")

    def test_a_project_without_a_stored_branch_keeps_compose_slash_slug(self):
        for cfg in (None, {}, {"test": "true"}):
            with self.subTest(cfg=cfg):
                self.assertEqual(fc.fc_core.project_branch(self.proj("old", cfg)), "compose/old")
