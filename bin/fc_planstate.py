"""The Linear state an issue takes while its plan is being written: Planning, made in a team that has none (docs/states.md)."""
import sys
import fc_linear, fc_tasks

PLANNING_STATE = {"name": fc_tasks.PLANNING, "type": "unstarted", "color": "#bb87fc", "description": "Its plan is being written"}

def planning_state(issue):
    """The Planning state of the issue's team, made when the team has none: unstarted, after the team's other unstarted states and before
    its first started one, so a board shows it between Todo and In Progress."""
    nodes = issue["team"]["states"]["nodes"]
    if found := [n for n in nodes if (n["name"] or "").lower() == fc_tasks.PLANNING.lower()]:
        return found[0]
    at = lambda kind: [n.get("position") or 0 for n in nodes if n["type"] == kind]
    last, started = max(at("unstarted") or at("backlog") or [0]), at("started")
    position = (last + min(started)) / 2 if started and min(started) > last else last + 1
    made = fc_linear.linear_gql("mutation($in: WorkflowStateCreateInput!) { workflowStateCreate(input: $in) { success workflowState { id name type position } } }",
                      {"in": {**PLANNING_STATE, "teamId": issue["team"]["id"], "position": position}})["workflowStateCreate"]["workflowState"]
    nodes.append(made)
    return made

def linear_planning(ident):
    """A planner starts on `ident`: move it from Todo or Backlog to Planning (planning_state), so the board shows its plan is being written.
    An issue anywhere else stays where it is; it leaves Planning by the state table, In Progress once a task of it is dispatched. Says what
    it did on stderr, for the planner's brief owns stdout, and never raises: a brief is not held up by a status. True when it moved."""
    try:
        issue = fc_linear.linear_issue(ident)
        if fc_tasks.state_name(issue["state"]).lower() not in ("todo", "backlog"):
            return False
        new = not any((n["name"] or "").lower() == fc_tasks.PLANNING.lower() for n in issue["team"]["states"]["nodes"])
        state = planning_state(issue)
        fc_linear.linear_gql("mutation($id: String!, $s: String!) { issueUpdate(id: $id, input: {stateId: $s}) { success } }", {"id": issue["id"], "s": state["id"]})
    except Exception as e:  # noqa: BLE001
        print(f"linear {ident} not moved to {fc_tasks.PLANNING}: {str(e)[:200]}", file=sys.stderr)
        return False
    print(f"linear {issue['identifier']} -> {fc_tasks.PLANNING}" + (" (the team had no such state: made it)" if new else ""), file=sys.stderr)
    return True
