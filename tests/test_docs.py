import hermetic  # first: no test sees the real home
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

ROOT = Path(__file__).resolve().parents[1]
UNDOCUMENTED = {"review", "cost", "models", "reenter"}  # subcommands docs/cli.md has no row for yet


def shipped():
    return [ROOT / "SKILL.md", ROOT / "README.md", *sorted((ROOT / "sections").glob("*.md")), *sorted((ROOT / "commands").glob("*.md")), *sorted((ROOT / "docs").glob("*.md"))]


class Docs(unittest.TestCase):
    def test_no_shipped_markdown_has_an_em_dash(self):
        self.assertEqual([str(p.relative_to(ROOT)) for p in shipped() if "—" in p.read_text()], [])

    def test_every_subcommand_has_a_cli_row(self):
        rows = [l.split("|")[1] for l in (ROOT / "docs" / "cli.md").read_text().splitlines() if l.startswith("| `")]
        out = subprocess.run([sys.executable, str(ROOT / "bin" / "compose"), "--help"], capture_output=True, text=True, check=True).stdout
        names = re.findall(r"(?m)^    ([a-z][a-z-]*)(?: |$)", out.split("<command>\n", 1)[1])
        self.assertGreater(len(names), 30)
        self.assertEqual([c for c in names if c not in UNDOCUMENTED and not any(re.search(r"[\\|` ]%s[\\|` ]" % re.escape(c), f" {r} ") for r in rows)], [])

    def test_relative_markdown_links_resolve(self):
        bad = []
        for p in shipped():
            for target in re.findall(r"\]\(([^)\s#<]+)(?:#[^)]*)?\)", p.read_text()):
                if not re.match(r"[a-z]+:", target) and not (p.parent / target).exists():
                    bad.append(f"{p.relative_to(ROOT)} -> {target}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
