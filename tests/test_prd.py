import hermetic  # first: no test sees the real home
import io, json, os, tempfile, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from test_linear_cmd import fc, URL, KEY
os.environ["HERDR_ENV"] = ""

HEADINGS = ["## Problem", "## Who it is for", "## Goals", "## Non-goals", "## Anti-goals", "## The product at a glance", "## Open questions"]
FULL = "# PRD\n\n" + "\n\n".join(f"{h}\n\nAbout {h[3:].lower()}." for h in HEADINGS) + "\n"
TODAY = time.strftime("%Y-%m-%d")


def issue(ident, title, state="Todo", *labels, parent=None, description="", kind=None):
    return {"identifier": ident, "title": title, "description": description, "labels": {"nodes": [{"name": l} for l in labels]},
            "state": {"name": state, "type": kind or ("completed" if state == "Done" else "canceled" if state == "Canceled" else "unstarted")},
            "parent": {"identifier": parent} if parent else None}


class Fake(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        (self.repo / ".compose").mkdir()
        (self.repo / ".compose" / "linear.json").write_text(json.dumps({"project": URL, "team": "Engineering"}))
        self.calls, self.created, self.updated = [], [], []
        self.docs = [{"id": "d1", "title": "Product delivery plan", "url": "u/plan", "content": "PLAN"}]
        self.project = {"name": "Shop", "description": "Sell shoes online", "content": "We sell shoes to runners."}
        self.issues = [issue("NEB-1", "Checkout", "Todo", "Feature", description="Pay for the cart.\nCards first.\n\nMore later."),
                       issue("NEB-2", "Pay by card", "Todo", "billing", parent="NEB-1", description="Stripe card form."),
                       issue("NEB-3", "Write the card form", "Todo", "Task", parent="NEB-2"),
                       issue("NEB-4", "Sign in", "Done", "Feature"),
                       issue("NEB-5", "Refund a card payment", "Backlog", "billing"),
                       issue("NEB-6", "Old idea", "Canceled")]
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", KEY), (fc.fc_config, "WORKSPACE", "work"), (fc.fc_linear, "linear_gql", self.gql),
                                 (fc.fc_linear_cmd, "await_write", mock.Mock())):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def gql(self, query, variables):
        q = " ".join(query.split())
        self.calls.append((q, variables))
        if "projects(filter" in q:
            return {"projects": {"nodes": [{"id": "p1", "name": "Shop", "url": URL, "teams": {"nodes": [{"id": "t1", "name": "Engineering", "key": "ENG"}]}}]}}
        if "documents(first" in q:
            return {"project": {"documents": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                              "nodes": [{k: d[k] for k in ("id", "title", "url")} for d in self.docs]}}}
        if "document(id" in q:
            return {"document": dict(next(d for d in self.docs if d["id"] == variables["id"]))}
        if "issues(first" in q:
            return {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self.issues}}
        if "project(id" in q:
            return {"project": dict(self.project)}
        if "documentCreate" in q:
            self.created.append(variables)
            self.docs.append({"id": "dnew", "title": variables["title"], "url": "u/prd", "content": variables["content"]})
            return {"documentCreate": {"document": {"id": "dnew", "title": variables["title"], "url": "u/prd", "content": variables["content"]}}}
        if "documentUpdate" in q:
            self.updated.append(variables)
            next(d for d in self.docs if d["id"] == variables["id"])["content"] = variables["content"]
            return {"documentUpdate": {"success": True}}
        raise AssertionError(q)

    def mutations(self):
        return [q for q, _ in self.calls if q.startswith("mutation")]

    def run_verb(self, action, **kw):
        a = SimpleNamespace(**{**dict(repo=str(self.repo), action=action, file=None, quote=None), **kw})
        with redirect_stdout(io.StringIO()) as out:
            fc.fc_prd.cmd_prd(a)
        return out.getvalue()

    def refused(self, action, **kw):
        with self.assertRaises(SystemExit) as e:
            self.run_verb(action, **kw)
        return str(e.exception.code)

    def prd_file(self, text=FULL):
        path = self.repo / "prd.md"
        path.write_text(text)
        return str(path)


class Status(Fake):
    def test_status_says_none_without_a_prd_and_names_it_when_there_is_one(self):
        self.assertEqual(self.run_verb("status"), "prd: none\n")
        self.docs.append({"id": "d2", "title": "PRD", "url": "u/prd", "content": FULL})
        self.assertEqual(self.run_verb("status"), "prd: PRD u/prd\n")

    def test_an_alternate_title_counts_as_the_prd(self):
        for title in ("prd", "PRD: Shop", "PRD - v2", "Prd shoes", "Shop product requirements", "Product Requirements Document"):
            self.assertTrue(fc.fc_prd.is_prd_title(title), title)
        for title in ("PRDX", "Approach", "Product delivery plan", "Night prep: questions for the next planning session", "requirements"):
            self.assertFalse(fc.fc_prd.is_prd_title(title), title)
        self.docs.append({"id": "d2", "title": "Shop product requirements", "url": "u/req", "content": "old"})
        self.assertEqual(self.run_verb("status"), "prd: Shop product requirements u/req\n")

    def test_a_document_titled_prd_wins_over_an_alternate_title(self):
        self.docs += [{"id": "d2", "title": "Product requirements (old)", "url": "u/old", "content": ""},
                      {"id": "d3", "title": "PRD", "url": "u/prd", "content": ""}]
        self.assertEqual(self.run_verb("status"), "prd: PRD u/prd\n")

    def test_every_verb_needs_the_linear_key(self):
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", ""):
            self.assertIn("needs a Linear key", self.refused("status"))


class Write(Fake):
    def test_write_refuses_without_the_product_owner_s_words(self):
        for quote in (None, "", "   "):
            self.assertIn('--quote "<the product owner', self.refused("write", file=self.prd_file(), quote=quote))
        self.assertEqual(self.mutations(), [])

    def test_write_refuses_without_a_quote_under_away_and_yolo_too(self):
        with mock.patch.object(fc.fc_tasks, "yolo_state", return_value={"mode": "yolo", "decisions": []}):
            self.assertIn("--quote", self.refused("write", file=self.prd_file()))
        self.assertEqual(self.mutations(), [])

    def test_write_refuses_a_file_missing_a_heading_and_names_it(self):
        text = FULL.replace("## Anti-goals\n", "## Antigoals\n").replace("## Open questions\n", "")
        why = self.refused("write", file=self.prd_file(text), quote="ship it")
        self.assertIn("## Anti-goals", why); self.assertIn("## Open questions", why); self.assertNotIn("## Goals", why.split(";")[0])
        self.assertEqual(self.mutations(), [])

    def test_write_creates_the_prd_marked_a_draft_with_the_quote_in_its_history(self):
        out = self.run_verb("write", file=self.prd_file(), quote="yes,  that is  the product")
        self.assertEqual(out.strip().splitlines()[-1], "prd written: u/prd")
        (made,) = self.created
        self.assertEqual((made["title"], made["projectId"]), ("PRD", "p1"))
        self.assertTrue(made["content"].startswith(fc.fc_prd.DRAFT + "\n"))
        for h in HEADINGS:
            self.assertIn(h + "\n", made["content"])
        self.assertIn(f'- {TODAY}: written; the product owner said: "yes, that is the product"', made["content"])
        self.assertGreater(fc.fc_prd.written_at(self.repo), 0)

    def test_write_updates_the_prd_under_its_alternate_title_and_keeps_its_history(self):
        old = fc.fc_prd.DRAFT + "\n\n" + FULL + "\n## History\n\n- 2026-01-01: written; the product owner said: \"first\"\n"
        self.docs.append({"id": "d2", "title": "Product requirements", "url": "u/req", "content": old})
        self.run_verb("write", file=self.prd_file(FULL.replace("About goals.", "Sell more shoes.")), quote="second")
        self.assertEqual(self.created, [])
        (write,) = self.updated
        self.assertEqual(write["id"], "d2")
        self.assertIn("Sell more shoes.", write["content"])
        self.assertEqual(write["content"].count(fc.fc_prd.DRAFT), 1)
        self.assertIn('- 2026-01-01: written; the product owner said: "first"\n- ' + TODAY + ': written; the product owner said: "second"', write["content"])


class Seed(Fake):
    def test_seed_scans_the_project_and_closes_with_a_guess_and_writes_nothing(self):
        self.docs.append({"id": "d2", "title": "PRD", "url": "u/prd", "content": "> Draft\n\n## Problem\n\nRunners lack shoes."})
        (self.repo / "README.md").write_text("# Shop\n\nA shoe shop for runners.\n")
        (self.repo / "docs").mkdir(); (self.repo / "docs" / "prd-notes.md").write_text("Old PRD notes: runners first.\n")
        (self.repo / "DESIGN.md").write_text("Design: calm and quick.\n")
        out = self.run_verb("seed")
        for s in ("Shop", "Sell shoes online", "We sell shoes to runners.", "Runners lack shoes.", "- Product delivery plan", "- PRD",
                  "A shoe shop for runners.", "Old PRD notes: runners first.", "Design: calm and quick."):
            self.assertIn(s, out)
        self.assertIn("- NEB-1 (Todo; Feature) Checkout: Pay for the cart. Cards first.\n  - NEB-2 (Todo; billing) Pay by card: Stripe card form.\n", out)
        self.assertIn("- NEB-5 (Backlog; billing) Refund a card payment\n", out)
        self.assertNotIn("NEB-3", out); self.assertNotIn("Old idea", out)
        built = out.split("Built so far")[1].split("\n## ")[0]
        self.assertIn("- Sign in", built); self.assertNotIn("NEB-4", built)
        guess = out.split("\n## ")[-1]
        self.assertIn("guess", guess.splitlines()[0].lower())
        self.assertIn("NEB-1 Checkout", guess); self.assertIn("billing", guess)
        self.assertLess(out.index("Built so far"), out.index(guess.splitlines()[0]))
        self.assertEqual(self.mutations(), [])

    def test_seed_says_when_there_is_no_prd_yet(self):
        self.assertIn("No PRD yet", self.run_verb("seed"))

    def test_a_big_seed_is_trimmed_with_a_note_and_keeps_its_guess(self):
        self.issues = [issue(f"NEB-{n}", f"Ticket number {n} about shoes", "Todo", description="x" * 400) for n in range(10, 200)]
        out = self.run_verb("seed")
        self.assertLessEqual(len(out), fc.fc_prd.SEED_LIMIT + 200)
        self.assertRegex(out, r"trimmed: \d+ characters cut")
        self.assertIn("guess", out.split("\n## ")[-1].splitlines()[0].lower())

    def test_the_ticket_print_follows_ids_titles_and_states_only(self):
        first = fc.fc_prd.ticket_print(self.repo)
        self.issues[0]["description"] = "changed"
        self.assertEqual(fc.fc_prd.ticket_print(self.repo), first)
        self.issues[0]["state"]["name"] = "In Progress"
        self.assertNotEqual(fc.fc_prd.ticket_print(self.repo), first)


class Lookup(unittest.TestCase):
    """The factory's cached PRD lookup: at most every 5 minutes while missing, every 6 hours while found, always after a write."""
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        (self.repo / ".compose").mkdir()
        self.state, self.now = {"prd": {}}, time.time()
        self.find = mock.patch.object(fc.fc_prd, "find_prd", return_value=None).start()
        self.addCleanup(mock.patch.stopall)

    def test_a_missing_prd_is_looked_up_again_after_five_minutes(self):
        self.assertIs(fc.fc_prd.check(self.repo, self.state, self.now), False)
        self.assertIs(fc.fc_prd.check(self.repo, self.state, self.now + 299), False)
        self.assertEqual(self.find.call_count, 1)
        self.find.return_value = {"id": "d", "title": "PRD", "url": "u"}
        self.assertIs(fc.fc_prd.check(self.repo, self.state, self.now + 301), True)
        self.assertEqual(self.state["prd"]["url"], "u")

    def test_a_found_prd_is_looked_up_again_after_six_hours(self):
        self.find.return_value = {"id": "d", "title": "PRD", "url": "u"}
        fc.fc_prd.check(self.repo, self.state, self.now)
        self.find.return_value = None
        self.assertIs(fc.fc_prd.check(self.repo, self.state, self.now + 6 * 3600 - 1), True)
        self.assertIs(fc.fc_prd.check(self.repo, self.state, self.now + 6 * 3600 + 1), False)
        self.assertEqual(self.find.call_count, 2)

    def test_a_prd_write_makes_the_next_check_look_again(self):
        fc.fc_prd.check(self.repo, self.state, self.now - 10)
        fc.fc_prd.stamp(self.repo)
        self.find.return_value = {"id": "d", "title": "PRD", "url": "u"}
        self.assertIs(fc.fc_prd.check(self.repo, self.state, self.now + 1), True)

    def test_a_failed_lookup_changes_nothing_and_is_tried_again_next_turn(self):
        self.find.side_effect = RuntimeError("linear down")
        self.assertIsNone(fc.fc_prd.check(self.repo, self.state, self.now))
        self.assertEqual(self.state["prd"], {})
        self.find.side_effect = None
        fc.fc_prd.check(self.repo, self.state, self.now)
        before = dict(self.state["prd"])
        self.find.side_effect = SystemExit("no .compose/linear.json")
        self.assertIs(fc.fc_prd.check(self.repo, self.state, self.now + 400), False)
        self.assertEqual(self.state["prd"], before)
        self.find.side_effect = None
        fc.fc_prd.check(self.repo, self.state, self.now + 401)
        self.assertEqual(self.find.call_count, 4)

    def test_without_a_linear_key_the_lookup_fails_rather_than_saying_missing(self):
        mock.patch.stopall()  # the real lookup
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", ""):
            self.assertIsNone(fc.fc_prd.check(self.repo, {"prd": {}}, self.now))


if __name__ == "__main__":
    unittest.main()
