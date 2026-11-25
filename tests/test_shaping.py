"""Shaping (sections/0b.md) is the superpowers:brainstorming skill itself: compose invokes it and lists only what it changes,
never a paraphrase of the skill that could drift from it."""
import hermetic  # first: no test sees the real home
import os
import sys
import unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import fc_steps  # noqa: E402
SHAPING = (ROOT / "sections" / "0b.md").read_text()


class ShapingIsTheBrainstormingSkill(unittest.TestCase):
    def test_it_invokes_the_skill_and_follows_it_as_written(self):
        self.assertIn("Shaping is the superpowers:brainstorming skill itself: invoke it", SHAPING)
        self.assertIn("Never paraphrase it here or anywhere in compose", SHAPING)
        self.assertIn("**What compose changes.**", SHAPING)

    def test_it_holds_no_paraphrase_of_the_skill(self):
        for heading in ("**Your first message.**", "**The conversation.**", "**Lay out the approaches.**", "**Play it back.**",
                        "**Size it.**", "**Red flags.**", "**Do recon.**", "Get them describing", "Sort what the builder check asks"):
            with self.subTest(heading):
                self.assertNotIn(heading, SHAPING)

    def test_after_approval_the_planner_starts_not_writing_plans(self):
        self.assertIn("never superpowers:writing-plans", SHAPING)

    def test_setup_requires_the_skill_for_claude_code(self):
        row = next(r for r in fc_steps.STEPS if r[0] == "superpowers")
        self.assertEqual((row[1], row[3]), (".claude/plugins/cache/*/superpowers/*/skills/brainstorming/SKILL.md", "required"))
        self.assertIn("superpowers", fc_steps.CLAUDE_ONLY)

    def test_a_teammate_s_questions_are_asked_through_the_skill_not_a_paraphrase_of_it(self):
        relay = (ROOT / "sections" / "0h.md").read_text()
        sys.path.insert(0, str(ROOT / "bin"))
        import fc_decide  # noqa: E402
        self.assertIn("invoke superpowers:brainstorming", relay)
        self.assertIn("superpowers:brainstorming", fc_decide.ASK_STYLE)
        for rule in ("only when they are stuck", "the recommendation first", "open first", "no options listed"):  # held back what the skill offers
            with self.subTest(rule):
                self.assertNotIn(rule, relay + fc_decide.ASK_STYLE)

    def test_skill_and_docs_no_longer_say_shaping_runs_only_on_request(self):
        for f in ("SKILL.md", "README.md", "docs/how-it-works.md", "sections/0h.md", "commands/planner.md"):
            with self.subTest(f):
                text = (ROOT / f).read_text()
                self.assertNotIn("0b's triggers", text)
                self.assertNotIn("only when you ask", text)


if __name__ == "__main__":
    unittest.main()
