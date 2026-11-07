import base64, sys, time
import fc_core, fc_update

# ---------- `compose stages`: one pane that follows a verify, in a host with no pane per stage ----------
# herdr draws every verify stage on a pane of its own (fc_herdr.herdr_verify_panes). The Herdr app and Rex show programs, so there
# compose opens one pane in the project's `verify` tab running this command: it follows the verify's hold file (fc_update.hold_stage
# keeps each stage's state there), prints one line per stage change, and reports its own status over OSC 7501 as an agent would,
# so the host's agents list shows the verify working, blocked on a failed stage, or done.

POLL = 2  # seconds between reads of the hold file
WAIT_FOR_START = 120  # seconds a pane opened before the verify holds anything waits for its hold file

def report(state, message=""):
    """This pane's status for the host, over OSC 7501: state, app, and the message (base64, as the protocol asks)."""
    extra = (":kind=question" if state == "blocked" else "") + (":msg=" + base64.b64encode(message.encode()).decode() if message else "")
    sys.stdout.write(f"\x1b]7501;state={state}:app=compose{extra}\x1b\\")
    sys.stdout.flush()

def verify_of(proj, repo):
    """The hold file of the verify running on the project, None when none runs."""
    return next(iter(fc_update.live_on(proj, repo, ("verify",))), None)

def lines(states, shown):
    """One line per stage whose state or message changed since `shown` (updated in place)."""
    out = []
    for name, (state, message, at) in states.items():
        if shown.get(name) != (state, message):
            shown[name] = (state, message)
            out.append(f"{time.strftime('%H:%M:%S', time.localtime(at))}  {name:<24} {state:<8} {message}".rstrip())
    return out

def cmd_stages(a):
    """Follow the verify of the project named by --project from this pane until it ends; exit 1 when a stage failed."""
    proj = fc_core.project_dir(a)
    print(f"stages  following the verify of {proj.name}", flush=True)
    report("working")
    shown, told, seen_one, deadline = {}, None, False, time.time() + WAIT_FOR_START
    while True:
        held = verify_of(proj, a.repo)
        if held is None:
            if seen_one or time.time() > deadline:
                break
            time.sleep(POLL)
            continue
        seen_one = True
        states = {k: tuple(v) for k, v in (held.get("stage_states") or {}).items() if isinstance(v, list) and len(v) == 3}
        for line in lines(states, shown):
            print(line, flush=True)
        failed = [n for n, (s, m, _) in states.items() if s == "blocked"]
        now = ("blocked", f"{failed[-1]} failed") if failed else ("working", "")
        if now != told:
            report(*now)
            told = now
        time.sleep(POLL)
    failed = [n for n, (s, _) in shown.items() if s == "blocked"]
    if not seen_one:
        print(f"stages  no verify of {proj.name} started within {WAIT_FOR_START} s", flush=True)
        report("done")
        return
    print(f"stages  verify ended: {', '.join(failed)} failed" if failed else "stages  verify ended: every stage passed", flush=True)
    report("error" if failed else "done", f"{len(failed)} stage(s) failed" if failed else "")
    if failed:
        sys.exit(1)
