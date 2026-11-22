import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

NOT_FOUND = ("Node 948:9907 was not found in the Figma file. Likely causes: (1) The source URL was a /proto/, /figjam/, or other non-design link. "
             "(2) The node is inside a Figma branch and the file key is the main file's. (3) The link is stale or the node was deleted. "
             "Open the file in Figma, select the node, and copy a fresh link: " + "x" * 161)
INFO = ("[INFO] Retrieving raw Figma node: 948:9907 from AbCdEf123456 (depth: default)\n"
        "[INFO] Calling https://api.figma.com/v1/files/AbCdEf123456/nodes?ids=948:9907\n"
        "[INFO] Using Personal Access Token for authentication\n")


class FramelinkFailure(unittest.TestCase):
    """#484: a failed Figma node fetch said `n for authentication` (the end of a log line) and never the file it asked."""
    def setUp(self):
        self.env = Path(tempfile.mkdtemp()) / "fl.env"
        self.env.write_text("FIGMA_API_KEY=figd_secret_token\n")

    def refused(self, stderr, node="948:9907", key="AbCdEf123456", **kw):
        bad = SimpleNamespace(returncode=1, stdout="", stderr=stderr)
        with mock.patch.object(subprocess, "run", return_value=bad), self.assertRaises(RuntimeError) as e:
            fc.fc_framelink.framelink_fetch(key, node=node, env_file=self.env, **kw)
        return str(e.exception)

    def test_the_message_starts_at_the_error_not_in_the_middle_of_a_log_line(self):
        self.assertGreater(len(INFO + NOT_FOUND), 500)
        msg = self.refused(INFO + NOT_FOUND)
        self.assertNotIn("n for authentication", msg)
        self.assertNotIn("[INFO]", msg)
        self.assertIn("Node 948:9907 was not found in the Figma file. Likely causes: (1)", msg)
        self.assertIn(NOT_FOUND[-60:], msg)  # the whole error, to its last word

    def test_it_names_the_file_and_the_node_it_asked(self):
        msg = self.refused(INFO + NOT_FOUND)
        self.assertTrue(msg.startswith("framelink fetch failed for node 948:9907 in file AbCdEf123456 (exit 1): "), msg)
        self.assertTrue(self.refused("boom", node=None).startswith("framelink fetch failed for file AbCdEf123456 (exit 1): boom"))

    def test_an_authentication_failure_says_so_and_a_missing_node_never_does(self):
        for text in ("Fetch failed with status 403: Forbidden", "Fetch failed with status 401: Unauthorized", "Invalid token"):
            with self.subTest(text=text):
                msg = self.refused(INFO + text)
                self.assertIn("Figma refused the token (authentication)", msg)
                self.assertIn(text, msg)
        msg = self.refused(INFO + NOT_FOUND)
        self.assertNotIn("authentication", msg)  # the INFO line named the credential in use; it is no verdict

    def test_a_secret_in_the_error_is_still_hidden_and_a_long_error_is_cut_at_its_end(self):
        msg = self.refused(INFO + "token figd_secret_token rejected " + "y" * 5000)
        self.assertNotIn("figd_secret_token", msg)
        self.assertIn("token *** rejected", msg)
        self.assertTrue(msg.endswith("..."))
        self.assertLess(len(msg), 2500)

    def test_a_failure_with_only_log_lines_keeps_them_rather_than_say_nothing(self):
        self.assertIn("Using Personal Access Token", self.refused(INFO))


class CodeNodeSource(unittest.TestCase):
    """#484: a code task's `figma_nodes:` read from the project's stored file, not the task's, was never said."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1", "figma": "StoredKey1234"}))
        self.wt = Path(tempfile.mkdtemp()).resolve()
        (self.wt / ".git").mkdir()

    def stop(self, meta):
        err = RuntimeError("framelink fetch failed for node 948:9907 in file X (exit 1): not found")
        with mock.patch.object(fc.fc_framelink, "framelink_fetch", side_effect=err), self.assertRaises(RuntimeError) as e:
            fc.fc_figma.code_nodes(self.proj, self.wt, meta)
        return str(e.exception)

    def test_a_stop_says_the_file_came_from_the_projects_stored_file_when_the_task_has_no_figma_line(self):
        msg = self.stop({"id": "523", "figma_nodes": ["948-9907"]})
        self.assertIn("StoredKey1234", msg)
        self.assertIn("the project's stored Figma file (the task has no figma: line)", msg)
        self.assertIn("not found", msg)

    def test_a_stop_says_the_file_came_from_the_tasks_figma_line(self):
        msg = self.stop({"id": "523", "figma_nodes": ["948-9907"], "figma": "TaskKey123456"})
        self.assertIn("TaskKey123456", msg)
        self.assertIn("the task's figma: line", msg)
        self.assertNotIn("StoredKey1234", msg)

    def test_the_file_a_task_reads_is_one_answer_for_run_and_for_the_stop(self):
        self.assertEqual(fc.fc_framelink.node_file(self.proj, {"figma": "TaskKey123456"}), ("TaskKey123456", "the task's figma: line"))
        self.assertEqual(fc.fc_framelink.node_file(self.proj, {}), ("StoredKey1234", "the project's stored Figma file (the task has no figma: line)"))
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1"}))
        self.assertEqual(fc.fc_framelink.node_file(self.proj, {}), (None, None))

    def test_run_says_at_its_start_which_stored_file_a_task_with_figma_nodes_and_no_figma_line_reads(self):
        tasks = [{"id": "523", "figma_nodes": ["948-9907", "9-1"]}, {"id": "524", "figma_nodes": ["1-2"], "figma": "TaskKey123456"}, {"id": "525"}]
        self.assertEqual(fc.fc_framelink.stored_file_notes(self.proj, tasks),
                         ["523 figma  reads node 948-9907, 9-1 from file StoredKey1234, the project's stored Figma file (the task has no figma: line)"])
        (self.proj / "linear.json").write_text("{}")
        self.assertIn("has figma_nodes: and no Figma file", fc.fc_framelink.stored_file_notes(self.proj, tasks)[0])


if __name__ == "__main__":
    unittest.main()
