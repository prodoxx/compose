"""What `compose report` records besides the reports themselves: the refusals (a report compose would not send, so the watch can say
a teammate tried and why it failed) and the test writer's auto-collect, which runs once the turn that reported `test ready` has ended."""
import json, re, shutil, subprocess, sys, time
import fc_core, fc_herdr, fc_fleet


def reports_dir(proj):
    d = proj / "results" / "crew"
    d.mkdir(parents=True, exist_ok=True)
    return d

def write_refusal(proj, name, state, message, why):
    """The record of a report compose did not send: the watch turns it into an `event crew=<name> report=refused` so the team lead
    learns a teammate tried, and why it failed, instead of waiting on a report that never arrived."""
    with open(reports_dir(proj) / f"{name}.refused", "a") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "state": state, "message": message, "why": why}) + "\n")

def refuse(proj, name, state, message, why):
    """Record the refusal, then exit with it: the reporter reads the reason on stderr, its watcher in the event."""
    write_refusal(proj, name, state, message, why)
    sys.exit(f"REPORT REFUSED ({name} {state}): {why}")

def crew_refusals(proj, name):
    f = reports_dir(proj) / f"{name}.refused"
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.exists() else []

def test_writer_id(proj, name):
    """The task id a `<slug>-tests-NNN` teammate writes the test for, else None."""
    m = re.search(r"-tests-(\d{3})$", name)
    return m.group(1) if m and name == fc_herdr.agent_name(proj.name, f"tests-{m.group(1)}") else None

def collect_after_turn(proj, name, tid):
    """A test writer's `test ready` is collected by compose itself once the turn that reported it has ended: a detached process (its
    own session, started from the writer's pane, whose hangup cannot reach it) waits for the agent to settle, then runs
    `testcrews --collect --only NNN`, which puts the test on the project branch, frees the writer's slot, writes its `done`, and
    wakes the team lead. A collect that fails reports `blocked` as the writer, with the log to read. Outside herdr there is no turn
    to wait for: the team lead runs the collect."""
    if not (reports_dir(proj) / f"{name}.json").exists() or not fc_fleet.inside():
        return
    wt, log = fc_herdr.project_worktree(proj), proj / "results" / "collect.log"
    compose = shutil.which("compose") or str(fc_core.CLI_CHECKOUT / "bin" / "compose")
    herdr = shutil.which("herdr") or "herdr"
    script = ('"$1" agent wait "$2" --timeout 900000 >/dev/null 2>&1; '
              'if ! "$0" testcrews --collect --only "$3" --repo "$4" --project "$5" >>"$6" 2>&1; then '
              '"$0" report --repo "$4" --project "$5" --as "$2" blocked "auto-collect of tests-$3 failed; read $6, fix, then compose testcrews --collect --only $3" >>"$6" 2>&1; fi')
    subprocess.Popen(["sh", "-c", script, compose, herdr, name, tid, str(wt), proj.name, str(log)],
                     start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"tests-{tid} is collected onto the project branch when this turn ends (log: {log})")
