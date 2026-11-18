import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, sys, unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

stale = fc.fc_spec.fc_stale

# A story as one stood on Linear: created with a placeholder, then shaped, deep-dived, built and moved to In Review, and nobody
# came back to the lines that had stopped being true.
SHAPED = """Not shaped yet. Needs a deep dive.
As the team, we want an endpoint that tells the extension who is signed in.

## Decisions so far

* D1: The backend changes are made in the backend repo (product owner, 2026-11-10; said: "we make the changes there")
* ~~D5: Tokens expire after a day (compose ruling, unconfirmed, 2026-11-10)~~ (C4)
* D10: No token expiry and no prune in this story (C4; product owner, 2026-11-12; said: "remove it if we dont need it")

## Tech doc

[ENG-7: tech doc](https://linear.app/d/1)

## Acceptance criteria

1. A signed-in user gets 200.

## Open questions

* ENG-7's tech doc is not approved yet (readiness check, 2026-11-11)
"""


def issue(description=SHAPED, labels=("Story", "deep-dive"), status="In Review"):
    return {"id": "ENG-7", "description": description, "labels": list(labels), "status": status}


class Stale(unittest.TestCase):
    def test_a_shaped_ticket_that_still_says_not_shaped_yet_is_stale(self):
        gaps = stale.stale(issue())
        self.assertTrue(any('"Not shaped yet"' in g and "remove" in g for g in gaps), gaps)
        self.assertTrue(any('"Needs a deep dive"' in g and "`## Tech doc`" in g for g in gaps), gaps)
        # still waiting to be shaped: the label says so, and the line is true
        self.assertEqual(stale.stale(issue(labels=("Story", "needs-shaping"), status="Backlog")), [])
        # the label is gone and nothing is on it yet: nobody can tell from the ticket, so nothing is said
        bare = "Not shaped yet. Needs a deep dive.\nWhich endpoint?\n"
        self.assertEqual(stale.stale(issue(bare, status="Backlog")), [])
        # labels unknown (a feature read from a sync carries none): the line is judged by what the ticket holds
        self.assertTrue(any('"Not shaped yet"' in g for g in stale.stale({"id": "ENG-1", "description": "Not shaped yet.\n\n## Intent\n\nPay by card.\n"})))
        self.assertEqual(stale.stale({"id": "ENG-1", "description": "## Intent\n\nA story that is not shaped yet waits.\n"}), [])  # prose, not the placeholder line

    def test_removing_the_placeholder_keeps_everything_else(self):
        out = stale.shaped_text(SHAPED)
        self.assertTrue(out.startswith("As the team, we want an endpoint"), out[:60])
        self.assertEqual(out, SHAPED.split("\n", 1)[1])
        # no tech doc linked yet: the deep dive is still owed, so that sentence stays
        owed = "Not shaped yet. Needs a deep dive.\nOpen: which endpoint?\n\n## Acceptance criteria\n\n1. Works.\n"
        self.assertEqual(stale.shaped_text(owed), "Needs a deep dive.\nOpen: which endpoint?\n\n## Acceptance criteria\n\n1. Works.\n")
        self.assertEqual(stale.shaped_text("Not shaped yet: which endpoint is open.\n\n## Intent\n\nx\n"), "which endpoint is open.\n\n## Intent\n\nx\n")
        self.assertEqual(stale.shaped_text("## Intent\n\nx\n"), "## Intent\n\nx\n")

    def test_a_readiness_question_left_on_a_story_past_todo_is_stale(self):
        gaps = stale.stale(issue())
        self.assertTrue(any("readiness check" in g and "In Review" in g and "ENG-7's tech doc is not approved yet" in g for g in gaps), gaps)
        for status in ("Todo", "Backlog"):  # readiness still judges it and rewrites its own lines
            self.assertFalse(any("readiness check" in g for g in stale.stale(issue(status=status))), status)
        theirs = SHAPED.replace("* ENG-7's tech doc is not approved yet (readiness check, 2026-11-11)", "* Which card brands?")
        self.assertFalse(any("readiness check" in g for g in stale.stale(issue(theirs))))  # the product owner's own question is theirs to close

    def test_a_tech_doc_older_than_a_live_decision_on_its_issue_is_stale(self):
        doc = lambda at: [{"title": "ENG-7: tech doc", "url": "https://linear.app/d/1", "updatedAt": at}, {"title": "ENG-8: tech doc", "updatedAt": "2020-01-01T00:00:00.000Z"}]
        gap = stale.tech_doc_behind("ENG-7", SHAPED, doc("2026-11-11T09:00:00.000Z"))
        self.assertIn("last written 2026-11-11", gap); self.assertIn("before D10 (2026-11-12)", gap)
        self.assertNotIn("D5", gap); self.assertNotIn("D1,", gap)  # a struck line decides nothing, and D1 is older than the doc
        self.assertIn("compose linear doc update", gap); self.assertIn('"the tech doc on ENG-7 stands after D10"', gap)
        self.assertIsNone(stale.tech_doc_behind("ENG-7", SHAPED, doc("2026-11-12T18:00:00.000Z")))  # rewritten the day of the decision or later
        self.assertIsNone(stale.tech_doc_behind("ENG-7", SHAPED, []))  # no tech doc: another gate's gap
        self.assertIsNone(stale.tech_doc_behind("ENG-7", SHAPED, [{"title": "ENG-7: tech doc"}]))  # no time on it: nothing to compare
        # read again and found to stand, or approved after the decision: the record says so, and the doc is not behind
        stands = SHAPED.replace("\n## Tech doc", '* D12: the tech doc on ENG-7 stands after D10 (compose ruling, unconfirmed, 2026-11-13)\n\n## Tech doc')
        self.assertIsNone(stale.tech_doc_behind("ENG-7", stands, doc("2026-11-11T09:00:00.000Z")))
        later = stands.replace("\n## Tech doc", '* D13: Sessions last a week (product owner, 2026-11-14; said: "a week")\n\n## Tech doc')
        self.assertIn("before D13 (2026-11-14)", stale.tech_doc_behind("ENG-7", later, doc("2026-11-11T09:00:00.000Z")))

    def test_a_decision_on_an_issue_with_a_tech_doc_says_to_reread_it(self):
        note = stale.reread_note("ENG-7", SHAPED, ["D11: Sessions last a week"])
        self.assertIn("ENG-7 has a tech doc", note); self.assertIn("D11", note); self.assertIn("stands after D11", note)
        self.assertIsNone(stale.reread_note("ENG-7", SHAPED, ["D11: the tech doc on ENG-7 is approved"]))  # a line about the doc itself
        self.assertIsNone(stale.reread_note("ENG-7", SHAPED, []))
        self.assertIsNone(stale.reread_note("ENG-7", "## Intent\n\nx\n", ["D1: Sessions last a week"]))  # no tech doc


if __name__ == "__main__":
    unittest.main()
