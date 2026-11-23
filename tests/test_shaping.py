"""Shaping (sections/0b.md) is superpowers:brainstorming run on Linear: the moves that make it a conversation stay in the section."""
import hermetic  # first: no test sees the real home
import os
import unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

ROOT = Path(__file__).resolve().parents[1]
SHAPING = (ROOT / "sections" / "0b.md").read_text()


class ShapingFollowsBrainstorming(unittest.TestCase):
    def test_it_runs_whenever_something_non_trivial_is_open_not_only_on_request(self):
        self.assertIn("**First: what do you actually know?**", SHAPING)
        self.assertIn("Something non-trivial is missing", SHAPING)
        self.assertNotIn("shape only when asked", SHAPING.lower())

    def test_the_first_message_plays_the_request_back_with_what_was_found(self):
        self.assertIn("Echo the request back in your own words", SHAPING)
        self.assertIn("Add what you found that bears on it", SHAPING)
        self.assertIn("Or if you'd rather I just write it up and start, say so.", SHAPING)

    def test_it_looks_things_up_itself_instead_of_asking_leave_to(self):
        self.assertIn("**Do recon.**", SHAPING)
        self.assertNotIn("go only when they say yes", SHAPING)

    def test_approaches_get_a_message_of_their_own_and_a_section_on_the_issue(self):
        self.assertIn("**Lay out the approaches.**", SHAPING)
        self.assertIn("put the ways of building it in a message of its own", SHAPING)
        self.assertIn("Constraints, Approaches, Decided by the product owner", SHAPING)

    def test_it_offers_a_spike_and_keeps_the_red_flags(self):
        self.assertIn("**Spike to feel things out.**", SHAPING)
        self.assertIn("**Red flags.**", SHAPING)

    def test_skill_and_docs_no_longer_say_shaping_runs_only_on_request(self):
        for f in ("SKILL.md", "README.md", "docs/how-it-works.md", "sections/0h.md", "commands/planner.md"):
            with self.subTest(f):
                text = (ROOT / f).read_text()
                self.assertNotIn("0b's triggers", text)
                self.assertNotIn("only when you ask", text)


if __name__ == "__main__":
    unittest.main()
