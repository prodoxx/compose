import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_waiting


class Store(unittest.TestCase):
    """The waiting list is one file per project: ids that are never reused, one open item per key, one line each."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / "hw"
        self.file = self.proj / "results" / "waiting.json"  # the results directory does not exist yet: the first item makes it

    def test_an_item_gets_the_next_id_and_one_line(self):
        a = fc_waiting.open_item(self.proj, "answer", "report:hw-orchestrator:t1", "which\n database?  " + "x" * 200, "SQLite", ids=["Q1"])
        b = fc_waiting.open_item(self.proj, "confirm", "decision:NEB-1:D7", "D7: dark theme")
        self.assertEqual((a["id"], b["id"]), ("W1", "W2"))
        self.assertEqual(len(a["line"]), 120)
        self.assertTrue(a["line"].startswith("which database? x"))
        self.assertTrue(a["line"].endswith("..."))
        self.assertEqual((a["stops"], b["stops"]), (True, False))
        self.assertEqual((a["recommendation"], a["ids"], a["seen"], a["closed"], a["closed_by"]), ("SQLite", ["Q1"], None, None, None))
        self.assertEqual(len(json.loads(self.file.read_text())["items"]), 2)

    def test_the_same_key_is_one_item(self):
        a = fc_waiting.open_item(self.proj, "go", "k", "wait for the go")
        b = fc_waiting.open_item(self.proj, "go", "k", "wait for the go again")
        self.assertEqual((a["id"], b["id"]), ("W1", "W1"))
        self.assertEqual(len(json.loads(self.file.read_text())["items"]), 1)

    def test_a_closed_item_leaves_the_open_list_and_its_id_is_not_reused(self):
        fc_waiting.open_item(self.proj, "go", "k", "wait for the go")
        self.assertTrue(fc_waiting.close_item(self.proj, "k", "settled"))
        self.assertFalse(fc_waiting.close_item(self.proj, "k", "settled"))
        self.assertEqual(fc_waiting.open_items(self.proj), [])
        closed = json.loads(self.file.read_text())["items"][0]
        self.assertEqual(closed["closed_by"], "settled")
        self.assertTrue(closed["closed"])
        self.assertEqual(fc_waiting.open_item(self.proj, "go", "k2", "again")["id"], "W2")

    def test_seen_keeps_it_open(self):
        fc_waiting.open_item(self.proj, "confirm", "k", "D1: a thing")
        self.assertTrue(fc_waiting.mark_seen(self.proj, "W1")["seen"])
        self.assertEqual([i["id"] for i in fc_waiting.open_items(self.proj)], ["W1"])
        self.assertIsNone(fc_waiting.mark_seen(self.proj, "W9"))

    def test_an_answered_question_closes_its_item_at_once(self):
        fc_waiting.open_item(self.proj, "answer", "report:a:t1", "which one?", ids=["Q1", "Q2"])
        fc_waiting.open_item(self.proj, "answer", "report:b:t2", "no ids here")
        self.assertEqual(fc_waiting.answered(self.proj, ["Q1"]), [])
        self.assertEqual(fc_waiting.open_items(self.proj)[0]["ids"], ["Q2"])
        self.assertEqual(fc_waiting.answered(self.proj, ["Q2"]), ["W1"])
        items = {i["id"]: i for i in json.loads(self.file.read_text())["items"]}
        self.assertEqual(items["W1"]["closed_by"], "decided")
        self.assertIsNone(items["W2"]["closed"])
        self.assertEqual(fc_waiting.answered(self.proj, ["Q1", "Q2"]), [])

    def test_an_unreadable_file_reads_as_empty_and_the_next_write_replaces_it(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text("{")
        self.assertEqual(fc_waiting.open_items(self.proj), [])
        self.assertEqual(self.file.read_text(), "{")
        self.assertEqual(fc_waiting.open_item(self.proj, "go", "k", "go?")["id"], "W1")
        self.assertEqual(json.loads(self.file.read_text())["items"][0]["id"], "W1")

    def test_a_file_that_is_not_a_store_reads_as_empty(self):
        self.file.parent.mkdir(parents=True)
        for bad in ('[]', '{"next": "3", "items": []}', '{"next": 3, "items": {}}', '{"next": 3, "items": [1]}', '{"next": 3, "items": [{"id": "W1"}]}'):
            self.file.write_text(bad)
            self.assertEqual(fc_waiting.load(self.proj), {"next": 1, "items": []}, bad)

    def test_stoppers_come_first_then_the_oldest(self):
        def open_at(stamp, *args):
            with mock.patch("time.strftime", lambda fmt: stamp):
                fc_waiting.open_item(self.proj, *args)
        open_at("2026-01-01 10:00:00", "confirm", "c1", "confirm late")    # W1, not a stopper, opened 10:00
        open_at("2026-01-01 09:00:00", "review", "r1", "review early")     # W2, not a stopper, 09:00
        open_at("2026-01-01 09:00:00", "go", "g1", "go early")             # W3, stopper, 09:00
        open_at("2026-01-01 09:00:00", "attend", "a1", "attend early")     # W4, stopper, 09:00
        open_at("2026-01-01 09:00:00", "answer", "q1", "answer")           # W5, stopper, 09:00
        self.assertEqual([i["id"] for i in fc_waiting.open_items(self.proj)], ["W3", "W4", "W5", "W2", "W1"])
        for n in range(6, 11):  # ids sort as numbers: W10 after W9 within one second
            open_at("2026-01-01 08:00:00", "go", f"n{n}", "go")
        self.assertEqual([i["id"] for i in fc_waiting.open_items(self.proj)][:6], ["W6", "W7", "W8", "W9", "W10", "W3"])


if __name__ == "__main__":
    unittest.main()
