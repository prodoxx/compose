import hermetic  # first: no test sees the real home
import copy, io, json, os, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from layers_kit import fc
os.environ["HERDR_ENV"] = ""

fc_states, fc_linear, fc_linearapi = fc.fc_linear.fc_states, fc.fc_linear, fc.fc_linear.fc_linearapi
# The Jobscan team's workflow as Linear returns it, and the workspace's names for compose's states.
JOBSCAN = [("Backlog", "backlog"), ("Planning", "unstarted"), ("Design in progress", "unstarted"), ("Ready for Dev", "unstarted"), ("Dev In Progress", "started"),
           ("PR In Review", "started"), ("Staged", "started"), ("Done", "completed"), ("Canceled", "canceled"), ("Duplicate", "canceled")]
NAMES = {"Todo": "Ready for Dev", "In Progress": "Dev In Progress", "In Review": "PR In Review", "Done": "Staged"}
PLAIN = [("Backlog", "backlog"), ("Todo", "unstarted"), ("In Progress", "started"), ("In Review", "started"), ("Done", "completed")]


def configured(case, names):
    p = mock.patch.object(fc.fc_config, "LINEAR_STATES", names); p.start(); case.addCleanup(p.stop)


class Linear:
    """Linear behind the HTTP call, holding a team's own state names: feature JOB-1 > story JOB-2 > tasks JOB-3, JOB-4."""
    def __init__(self, workflow=JOBSCAN, first="Ready for Dev"):
        self.states = [{"id": f"s-{n}", "name": n, "type": t, "position": k} for k, (n, t) in enumerate(workflow)]
        self.state = {i: first for i in ("JOB-1", "JOB-2", "JOB-3", "JOB-4")}
        self.parent = {"JOB-2": "JOB-1", "JOB-3": "JOB-2", "JOB-4": "JOB-2"}

    def node(self, i):
        return {"name": self.state[i], "type": next(s["type"] for s in self.states if s["name"] == self.state[i])}

    def __call__(self, query, variables, attempt=0):
        if "issueUpdate" in query:
            self.state[variables["id"]] = next(s["name"] for s in self.states if s["id"] == variables["s"])
            return {"issueUpdate": {"success": True}}
        if "commentCreate" in query:
            return {"commentCreate": {"success": True}}
        i = variables["id"]
        return {"issue": {"id": i, "identifier": i, "state": self.node(i), "team": {"id": "t", "states": {"nodes": copy.deepcopy(self.states)}},
                          "parent": {"identifier": self.parent[i]} if i in self.parent else None,
                          "children": {"nodes": [{"identifier": c, "state": self.node(c)} for c, p in self.parent.items() if p == i]}}}


class TeamNames(unittest.TestCase):
    """A workspace whose team names its states its own way: compose reads each one by its own name and moves issues to the team's."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp())
        (self.proj / "linear.json").write_text(json.dumps({"feature": "JOB-1"}))
        self.lin, self.said = Linear(), []
        configured(self, NAMES)
        for mod, name, value in ((fc.fc_config, "LINEAR_KEY", "k"), (fc_linearapi, "_linear_post", self.lin), (fc_linearapi.fc_pace, "wait", lambda *a, **k: None),
                                 (fc.fc_core, "say", self.said.append), (fc_linear, "refresh_ready", lambda *a, **k: None)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def states(self, *ids):
        return [self.lin.state[i] for i in ids]

    def test_a_build_walks_the_teams_own_states_from_ready_for_dev_to_staged(self):
        result = {"status": "passed", "branch": "b", "attempts": 1, "cost": 0.0, "model": "m", "log": []}
        fc_linear.linear_event(self.proj, {"linear": "JOB-3"}, "dispatched")
        self.assertEqual(self.states("JOB-3", "JOB-2", "JOB-1", "JOB-4"), ["Dev In Progress"] * 3 + ["Ready for Dev"])
        self.assertIn("linear JOB-3 -> Dev In Progress", self.said)  # the line names the state as the board shows it
        fc_linear.linear_event(self.proj, {"linear": "JOB-3"}, "passed", result)
        fc_linear.linear_event(self.proj, {"linear": "JOB-4"}, "dispatched")
        fc_linear.linear_event(self.proj, {"linear": "JOB-4"}, "passed", result)
        self.assertEqual(self.states("JOB-3", "JOB-4", "JOB-2", "JOB-1"), ["PR In Review"] * 3 + ["Dev In Progress"])  # the feature waits for verify
        fc_linear.linear_reconcile(self.proj, verified=True)
        self.assertEqual(self.lin.state["JOB-1"], "PR In Review")
        fc_linear.linear_merged("JOB-1", [{"linear": "JOB-3"}, {"linear": "JOB-4"}], True)
        self.assertEqual(self.states("JOB-3", "JOB-4", "JOB-2", "JOB-1"), ["Staged"] * 4)

    def test_an_issue_a_person_released_to_done_is_finished_and_is_not_moved_back_to_staged(self):
        self.lin.state.update({"JOB-3": "Done", "JOB-4": "Staged", "JOB-2": "PR In Review"})
        self.assertEqual(fc_linear.linear_issue("JOB-3")["state"], {"name": "Done", "type": "completed"})
        self.assertEqual(fc_linear.linear_issue("JOB-4")["state"], {"name": "Done", "type": "completed", "team_name": "Staged"})
        fc_linear.linear_merged("JOB-1", [{"linear": "JOB-3"}, {"linear": "JOB-4"}], False)
        self.assertEqual(self.states("JOB-3", "JOB-4", "JOB-2"), ["Done", "Staged", "Staged"])

    def test_a_state_the_workspace_does_not_name_keeps_its_own_name(self):
        self.lin.state["JOB-3"] = "Design in progress"
        self.assertEqual(fc_linear.linear_issue("JOB-3")["state"]["name"], "Design in progress")
        self.lin.state["JOB-3"] = "Duplicate"
        self.assertEqual(fc.fc_tasks.state_name(fc_linear.linear_issue("JOB-3")["state"]), "Canceled")

    def test_a_team_of_the_same_workspace_with_composes_own_names_is_read_and_moved_as_before(self):
        self.lin.__init__(PLAIN, "Todo")
        fc_linear.linear_event(self.proj, {"linear": "JOB-3"}, "dispatched")
        self.assertEqual(self.states("JOB-3", "JOB-2"), ["In Progress", "In Progress"])
        fc_linear.linear_merged("JOB-1", [{"linear": "JOB-3"}], False)
        self.assertEqual(self.lin.state["JOB-3"], "Done")

    def test_a_planner_moves_a_ready_for_dev_issue_to_the_teams_planning(self):
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertTrue(fc.fc_briefs.fc_planstate.linear_planning("JOB-2"))
        self.assertEqual((self.lin.state["JOB-2"], err.getvalue().strip()), ("Planning", "linear JOB-2 -> Planning"))

    def test_a_state_flag_takes_either_name_and_a_list_asks_for_both(self):
        team = fc_linear.linear_issue("JOB-1")["team"]
        for given in ("In Review", "pr in review"):
            a = SimpleNamespace(state=given, label=None)
            self.assertEqual(fc.fc_linear_cmd.state_and_labels({**team, "name": "Jobscan"}, a), {"stateId": "s-PR In Review"})
        self.assertEqual(fc_states.names("Staged"), ["Done", "Staged"])
        self.assertEqual(fc_states.names("in review"), ["in review", "PR In Review"])
        self.assertEqual(fc_states.names("Triage"), ["Triage"])


class Table(unittest.TestCase):
    def read(self, names, *states):
        configured(self, names)
        return fc_states.read({"team": {"states": {"nodes": [{"id": n, "name": n, "type": t} for n, t in states]}}})["team"]["states"]["nodes"]

    def test_with_no_names_configured_an_answer_is_untouched(self):
        configured(self, {})
        data = {"issue": {"state": {"name": "Ready for Dev", "type": "unstarted"}}}
        self.assertEqual(fc_states.read(copy.deepcopy(data)), data)
        self.assertEqual(fc_states.canonical("Ready for Dev"), "Ready for Dev")

    def test_a_list_reads_every_name_as_the_state_and_writes_the_first_the_team_has(self):
        names = {"done": ["Done", "Staged"], "Todo": ["Ready", "Ready for Dev"]}
        nodes = self.read(names, ("Staged", "started"), ("Done", "completed"), ("Ready for Dev", "unstarted"))
        self.assertEqual([(n["id"], n["name"], n["type"]) for n in nodes],
                         [("Staged", "Staged", "started"), ("Done", "Done", "completed"), ("Ready for Dev", "Todo", "unstarted")])
        self.assertEqual(fc_states.read({"state": {"name": "staged", "type": "started"}})["state"], {"name": "Done", "type": "completed", "team_name": "staged"})

    def test_the_state_compose_writes_is_the_only_one_found_under_its_name(self):
        nodes = self.read(NAMES, ("Staged", "started"), ("Done", "completed"))
        self.assertEqual([(n["id"], n["name"]) for n in nodes], [("Staged", "Done")])

    def test_a_sync_payload_piped_by_hand_takes_composes_names(self):
        configured(self, NAMES)
        proj = Path(tempfile.mkdtemp())
        (proj / "linear.json").write_text(json.dumps({"feature": "JOB-1"}))
        block = "```compose\nid: 001\nfiles: a.py\ntest: true\n```"
        with redirect_stdout(io.StringIO()):
            tasks = fc.fc_tasks.sync_tasks(proj, {"feature": {"id": "JOB-1", "title": "f", "description": ""}, "issues": [
                {"id": "JOB-3", "title": "t", "description": block, "status": "PR In Review", "blockedBy": [{"id": "JOB-9", "status": "Staged", "labels": []}]},
                {"id": "JOB-4", "title": "u", "description": block.replace("001", "002"), "status": "Ready for Dev", "blockedBy": [{"id": "JOB-8", "status": "Dev In Progress"}]}]})
        self.assertEqual([(t["linear_state"], t["waits_on"]) for t in tasks], [("In Review", []), ("Todo", ["JOB-8 (In Progress)"])])

    def test_a_wrong_table_stops_the_command_with_the_line_to_fix(self):
        for names, said in (({"Shipped": "Staged"}, "linear_states.Shipped: compose has no such state"), ({"Todo": 3}, "linear_states.Todo must be the team's name"),
                            ({"Todo": []}, "linear_states.Todo must be"), ({"Todo": "X", "Done": "x"}, "'x' already stands for Todo"),
                            ({"Todo": "Backlog"}, "'Backlog' already stands for Backlog"), (["Todo"], "linear_states must be an object")):
            configured(self, names)
            with self.assertRaises(SystemExit) as stop:
                fc_states.read({})
            self.assertIn(said, str(stop.exception))


if __name__ == "__main__":
    unittest.main()
