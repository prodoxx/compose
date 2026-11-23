"""A visual question page shows only what is still open: every brief and section that opens one says so."""
import hermetic  # first: no test sees the real home
import os
import sys
import unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import fc_briefs, fc_figma  # noqa: E402


class VisualPageShowsOnlyWhatIsOpen(unittest.TestCase):
    def test_the_shaping_page_holds_only_the_question_asked_now_and_is_written_from_empty(self):
        text = (ROOT / "sections" / "0b.md").read_text()
        self.assertIn("The page shows only the question being asked now", text)
        self.assertIn("no question already answered, no decision already settled", text)
        self.assertIn("Write the page again from an empty file for each visual question", text)

    def test_the_change_architect_page_drops_closed_questions(self):
        brief = " ".join(fc_briefs.CHANGE_BRIEF.split())
        self.assertIn("It holds only the visual questions still open", brief)
        self.assertIn("never an answered question, a settled decision, or a recap", brief)
        self.assertIn("written again with only the questions still open", brief)

    def test_the_designer_page_shows_only_open_questions(self):
        brief = " ".join(fc_figma.DESIGNER_BRIEF.split())
        self.assertIn("the page shows only the questions still open, never a settled one or a recap", brief)
        self.assertIn("that page shows only the questions still open", (ROOT / "sections" / "0d.md").read_text())


if __name__ == "__main__":
    unittest.main()
