import hermetic  # first: no test sees the real home
import contextlib, io, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock
from layers_kit import fc
os.environ["HERDR_ENV"] = ""

fc_linear = fc.fc_linear
planstate = fc.fc_briefs.fc_planstate
STATES = [{"id": "s-b", "name": "Backlog", "type": "backlog", "position": 0}, {"id": "s-t", "name": "Todo", "type": "unstarted", "position": 1},
          {"id": "s-p", "name": "In Progress", "type": "started", "position": 2}, {"id": "s-r", "name": "In Review", "type": "started", "position": 1002},
          {"id": "s-d", "name": "Done", "type": "completed", "position": 3}, {"id": "s-c", "name": "Duplicate", "type": "canceled", "position": 5}]
PLANNING = {"id": "s-pl", "name": "Planning", "type": "unstarted", "position": 1.5}


class LinearPlanning(unittest.TestCase):
    """A planner starting on an issue moves it from Todo or Backlog to Planning, making the state in a team that has none."""
    def setUp(self):
        self.state, self.states, self.sent, self.fail = "Todo", [dict(s) for s in STATES], [], None
        def gql(query, variables):
            if query.lstrip().startswith("mutation"):
                if self.fail:
                    raise RuntimeError(self.fail)
                self.sent.append((query.split("{")[1].split("(")[0].strip(), variables))
                return {"workflowStateCreate": {"success": True, "workflowState": dict(PLANNING)}, "issueUpdate": {"success": True}}
            state = next(s for s in self.states if s["name"] == self.state)
            return {"issue": {"id": "i-1", "identifier": "NEB-7", "state": {"name": state["name"], "type": state["type"]},
                              "team": {"id": "t-1", "states": {"nodes": self.states}}, "children": {"nodes": []}}}
        p = mock.patch.object(fc_linear, "linear_gql", gql); p.start(); self.addCleanup(p.stop)

    def planning(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            moved = planstate.linear_planning("neb-7")
        self.assertEqual(out.getvalue(), "")  # the planner's brief owns stdout
        return moved, err.getvalue().strip()

    def test_a_todo_or_backlog_issue_moves_to_the_teams_planning_state(self):
        self.states.append(dict(PLANNING))
        for self.state in ("Todo", "Backlog"):
            self.sent = []
            self.assertEqual(self.planning(), (True, "linear NEB-7 -> Planning"))
            self.assertEqual(self.sent, [("issueUpdate", {"id": "i-1", "s": "s-pl"})])

    def test_a_team_with_no_planning_state_gets_one_between_todo_and_in_progress(self):
        self.assertEqual(self.planning(), (True, "linear NEB-7 -> Planning (the team had no such state: made it)"))
        self.assertEqual(self.sent, [("workflowStateCreate", {"in": {"name": "Planning", "type": "unstarted", "color": planstate.PLANNING_STATE["color"],
                                                                     "description": "Its plan is being written", "teamId": "t-1", "position": 1.5}}),
                                     ("issueUpdate", {"id": "i-1", "s": "s-pl"})])

    def test_the_new_state_goes_after_the_last_unstarted_one_when_no_started_state_follows_it(self):
        self.states = [s for s in self.states if s["type"] != "started"] + [{"id": "s-x", "name": "Doing", "type": "started", "position": 0.5}]
        self.planning()
        self.assertEqual(self.sent[0][1]["in"]["position"], 2)

    def test_an_issue_that_is_planning_started_or_finished_stays_where_it_is(self):
        self.states.append(dict(PLANNING))
        for self.state in ("Planning", "In Progress", "In Review", "Done", "Duplicate"):
            self.assertEqual(self.planning(), (False, ""))
        self.assertEqual(self.sent, [])

    def test_a_failed_write_is_one_line_on_stderr_and_never_raised(self):
        self.fail = "HTTP 403: not allowed to change the team's workflow"
        self.assertEqual(self.planning(), (False, "linear neb-7 not moved to Planning: HTTP 403: not allowed to change the team's workflow"))


class PlannerBrief(unittest.TestCase):
    """`compose planner` marks the issue it plans, and only a real one, with a key."""
    def run_planner(self, issue=None, key="k", link=True):
        proj = Path(tempfile.mkdtemp()) / "shop"; proj.mkdir()
        if link:
            (proj / "linear.json").write_text(json.dumps({"feature": "NEB-1"}))
        with mock.patch.object(fc.fc_core, "project_dir", return_value=proj), mock.patch.object(fc.fc_config, "LINEAR_KEY", key), \
                mock.patch.object(fc.fc_briefs, "_known", return_value=None), mock.patch.object(fc.fc_briefs, "_prd", return_value=None), \
                mock.patch.object(planstate, "linear_planning") as planning, contextlib.redirect_stdout(io.StringIO()):
            fc.fc_briefs.cmd_planner(type("A", (), {"extra": None, "issue": issue}))
        return [c.args[0] for c in planning.call_args_list]

    def test_the_planned_issue_is_marked_the_feature_by_default_or_the_story_named(self):
        self.assertEqual(self.run_planner(), ["NEB-1"])
        self.assertEqual(self.run_planner("NEB-7"), ["NEB-7"])

    def test_nothing_is_marked_without_a_key_or_without_an_issue(self):
        self.assertEqual(self.run_planner(key=None), [])
        self.assertEqual(self.run_planner(link=False), [])


class Open(unittest.TestCase):
    """An issue in Planning is as open as one in Todo: it can be judged ready and can start."""
    def test_planning_counts_as_open_wherever_todo_and_backlog_do(self):
        self.assertEqual(fc.fc_tasks.OPEN, ("todo", "backlog", "planning"))
        self.assertIs(fc.fc_factoryturn.OPEN, fc.fc_tasks.OPEN)
        issue = lambda state: {"identifier": "NEB-7", "state": {"name": state, "type": "unstarted"}, "labels": {"nodes": []}, "inverseRelations": {"nodes": []}, "children": {"nodes": []}}
        with mock.patch.object(fc_linear, "task_holds", return_value=[]):
            self.assertEqual([fc_linear.ready_ids([issue(s)]) for s in ("Planning", "Todo", "In Progress")], [["NEB-7"], ["NEB-7"], []])
        story = lambda state: {"id": "NEB-7", "url": "u", "title": "T", "state": state, "blockers": []}
        self.assertIn("NEB-7", fc_linear.fc_plan.start_today([{"stories": [story("Planning")]}]))
        self.assertEqual(fc_linear.fc_plan.start_today([{"stories": [story("In Progress")]}]), "Nothing can start now.")


if __name__ == "__main__":
    unittest.main()
