import hermetic  # first: no test sees the real home
import json
import os
import unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

ROOT = Path(__file__).resolve().parents[1]

CONFIG = {"include": ["bin"], "typeCheckingMode": "strict", "pythonVersion": "3.9", "pythonPlatform": "Darwin"}


class PyrightConfig(unittest.TestCase):
    def test_config_is_exactly_the_strict_bin_config(self):
        path = ROOT / "pyrightconfig.json"
        self.assertTrue(path.exists(), "pyrightconfig.json must exist at the repo root")
        self.assertEqual(json.loads(path.read_text()), CONFIG)


if __name__ == "__main__":
    unittest.main()
