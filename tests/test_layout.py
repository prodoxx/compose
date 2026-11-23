import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, sys, unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_change, fc_layout, fc_plan, fc_tasks  # noqa: E402  # compose.py puts bin on sys.path

LOG = "---\n\n" + fc_plan.LOG_HEADING + "\n\n" + fc_layout.DISCLAIMER
FEATURE = ["Feature"]


class LaidOut(unittest.TestCase):
    def test_a_feature_keeps_what_a_person_reads_on_top_and_the_planning_under_the_line(self):
        body = ("Shoppers lose their cart when they sign in.\n\n## Intent\n\nKeep the cart.\n\n## Constraints\n\nOne table.\n\n"
                "## My calls\n\n- cookie first\n\n## Goals\n\n- nothing lost\n\n## Decisions so far\n\n- D1: keep it\n")
        self.assertEqual(fc_layout.laid_out(body, FEATURE),
                         "Shoppers lose their cart when they sign in.\n\n## Intent\n\nKeep the cart.\n\n## Goals\n\n- nothing lost\n\n" + LOG
                         + "\n\n## Constraints\n\nOne table.\n\n## My calls\n\n- cookie first\n\n## Decisions so far\n\n- D1: keep it\n")

    def test_the_approaches_a_shaping_weighed_go_under_the_line(self):
        body = "## Approaches\n\n- cookie (picked)\n- server cart\n\n## Intent\n\nKeep the cart.\n"
        self.assertEqual(fc_layout.laid_out(body, FEATURE),
                         "## Intent\n\nKeep the cart.\n\n" + LOG + "\n\n## Approaches\n\n- cookie (picked)\n- server cart\n")

    def test_laying_out_twice_changes_nothing(self):
        body = "Opening.\n\n## Acceptance criteria\n\n- it works\n\n## Notes\n\n### Codebase brief\n\nfacts\n\n## Evidence\n\n| a |\n"
        once = fc_layout.laid_out(body, ["Story"])
        self.assertIn(LOG + "\n\n## Notes\n\n### Codebase brief\n\nfacts\n\n## Evidence", once)
        self.assertEqual(fc_layout.laid_out(once, ["story"]), once)

    def test_a_ticket_with_no_planning_gets_no_line(self):
        body = "Opening.\n\n## Intent\n\nWhy.\n\n## Background\n\nA person wrote this.\n"
        self.assertEqual(fc_layout.laid_out(body, FEATURE), body)

    def test_a_section_written_to_the_end_goes_to_its_side_of_the_line(self):
        body = "Opening.\n\n" + LOG + "\n\n## Notes\n\nn\n\n## Goals\n\n- g\n\n## Research\n\nr\n"
        self.assertEqual(fc_layout.laid_out(body, ["Epic"]), "Opening.\n\n## Goals\n\n- g\n\n" + LOG + "\n\n## Notes\n\nn\n\n## Research\n\nr\n")

    def test_a_heading_counts_by_its_name_whatever_follows_it(self):
        body = "## Not yet specified (C1)\n\n- Q1: which?\n\n## decisions so far:\n\n- D1\n\n## Planning poker\n\nmine\n"
        self.assertEqual(fc_layout.laid_out(body, FEATURE),
                         "## Planning poker\n\nmine\n\n" + LOG + "\n\n## Not yet specified (C1)\n\n- Q1: which?\n\n## decisions so far:\n\n- D1\n")

    def test_a_ticket_with_only_planning_opens_with_the_log_and_no_rule(self):
        self.assertEqual(fc_layout.laid_out("## Decisions so far\n\n- D1\n", FEATURE), LOG.removeprefix("---\n\n") + "\n\n## Decisions so far\n\n- D1\n")

    def test_a_task_and_a_ticket_of_no_level_are_left_alone(self):
        task = "## Goal\n\ng\n\n## Constraints\n\nc\n\n```compose\nid: 001\ntest: t\n```\n"
        self.assertEqual(fc_layout.laid_out(task, ["Story"]), task)
        plain = "## Constraints\n\nc\n"
        self.assertEqual(fc_layout.laid_out(plain, ["Task"]), plain)
        self.assertEqual(fc_layout.laid_out(plain, ["bug"]), plain)
        self.assertNotEqual(fc_layout.laid_out(plain, None), plain)  # a caller that knows the ticket is a map passes no labels

    def test_the_note_is_written_once_and_what_a_person_added_under_it_stays(self):
        stored = "Opening.\n\n---\n\n" + fc_plan.LOG_HEADING + "\n\n" + fc_layout.DISCLAIMER.replace(", ", ",\n") + "\n\nAsk Sam first.\n\n## Notes\n\nn\n\n## My calls\n\n- c\n"
        out = fc_layout.laid_out(stored + "\n## Plan\n\np\n", FEATURE)
        self.assertEqual(out, "Opening.\n\n" + LOG + "\n\nAsk Sam first.\n\n## Notes\n\nn\n\n## My calls\n\n- c\n\n## Plan\n\np\n")

    def test_a_heading_inside_fenced_code_is_no_section(self):
        body = "## Intent\n\n```md\n## Notes\n```\n\n## Notes\n\nreal\n"
        self.assertEqual(fc_layout.laid_out(body, FEATURE), "## Intent\n\n```md\n## Notes\n```\n\n" + LOG + "\n\n## Notes\n\nreal\n")

    def test_a_body_linear_stores_alike_is_returned_as_it_was(self):
        stored = "Opening.\n\n\n---\n\n" + fc_plan.LOG_HEADING + "\n\n" + fc_layout.DISCLAIMER + "\n\n\n## Notes\n\n* n\n"
        self.assertIs(fc_layout.laid_out(stored, FEATURE), stored)


class WithoutEmptyLog(unittest.TestCase):
    def test_a_log_that_holds_only_the_note_goes_and_one_that_holds_more_stays(self):
        self.assertEqual(fc_layout.without_empty_log("Opening.\n\n" + LOG + "\n"), "Opening.\n")
        for kept in ("Opening.\n\n" + LOG + "\n\n## Notes\n\nn\n", "Opening.\n\n" + LOG + "\n\nAsk Sam first.\n", "Opening.\n"):
            self.assertEqual(fc_layout.without_empty_log(kept), kept)


class ReadersStopAtTheLine(unittest.TestCase):
    BODY = "Opening.\n\n## Acceptance criteria\n\n- the cart is kept\n- nothing is lost\n\n" + LOG + "\n\n## Notes\n\nn\n"

    def test_the_last_criterion_does_not_take_the_rule(self):
        self.assertEqual(fc_tasks.parse_criteria(self.BODY), ["the cart is kept", "nothing is lost"])

    def test_a_section_above_the_line_ends_at_the_rule(self):
        lines = self.BODY.split("\n")
        start, end = fc_plan.section_span(lines, "## Acceptance criteria")
        self.assertNotIn("---", lines[start:end])
        self.assertEqual(fc_change.map_section(self.BODY, "Acceptance criteria"), "- the cart is kept\n- nothing is lost")

    def test_a_section_write_above_the_line_keeps_the_line(self):
        new = fc_plan.replace_section(self.BODY, "## Acceptance criteria", "- one")
        self.assertEqual(new, "Opening.\n\n## Acceptance criteria\n\n- one\n\n" + LOG + "\n\n## Notes\n\nn\n")

    def test_the_log_heading_holds_only_its_note(self):
        lines = self.BODY.split("\n")
        start, end = fc_plan.section_span(lines, fc_plan.LOG_HEADING)
        self.assertEqual("\n".join(lines[start:end]).strip(), fc_plan.LOG_HEADING + "\n\n" + fc_layout.DISCLAIMER)


if __name__ == "__main__":
    unittest.main()
