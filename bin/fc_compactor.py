import os, re, subprocess, sys, time
from pathlib import Path
import fc_compactlog, fc_config, fc_herdr, fc_manager, fc_transcript

# The detached process the Stop hook starts to compact an engineering manager or team lead session at an idle moment: it waits for the
# pane to be quiet, has the session write its handoff, then types `/compact` with a fixed focus. Every step lands in the compaction log
# and every exit path clears the pane's in-flight marker. It types only into a pane that is idle with an empty input line.

PROMPT_MARKS = ("❯", ">")
DIM = re.compile(r"\x1b\[2m.*?\x1b\[(?:0|22)m")  # Claude Code's suggested next prompt sits dim in the input box until the owner takes it
SGR = re.compile(r"\x1b\[[0-9;]*m")
POLL = 2.0
GATE_LIMIT = 1800.0  # how long it waits for a quiet pane before giving up
CEILING_HOLD = 5.0  # a ceiling compaction cannot wait for the idle window: the session is about to run out of room
SETTLE_LIMIT = 300.0
START_WAIT = 10.0  # how long the pane gets to start working on the handoff prompt before it counts as already done
REQUIET_LIMIT = 60.0
COMPACT_LIMIT = 300.0
MARKER_REFRESH = 60.0  # the in-flight marker expires after 900 seconds and the idle gate can wait 1800

_HANDOFF = "compose: before a compaction, bring your handoff at {path} up to date now: sections %s, under 3,000 words, then reply with one line and nothing else."
HANDOFF_PROMPTS = {
    "manager": _HANDOFF % "`## Teammates live`, `## Open questions`, `## Decisions`, `## Focus`",
    "lead": _HANDOFF % "`## Open threads`, `## Holds`, `## Pending go's`, `## Teammates live`, `## Next`",
}
HANDOFF_PROMPT = HANDOFF_PROMPTS["manager"]
FOCUS = ("/compact The handoff at {path} is authoritative: keep the product owner's standing instructions and decisions, what each teammate "
         "waits on, and the watch rules exact; drop tool output and file contents.")


# ---------- reading the pane ----------
def input_line(lines):
    """The text after the last line that starts with a prompt mark, stripped; None when no line does. Earlier prompt lines are echoed prompts."""
    for line in reversed(lines):
        for mark in PROMPT_MARKS:
            if line.startswith(mark):
                return line[len(mark):].strip()
    return None

def pane_status(pane):
    """The agent's herdr status, read from the `agent` record `herdr agent get` answers with; None when there is none."""
    got = fc_herdr.herdr("agent", "get", pane)
    agent = got.get("agent") if isinstance(got, dict) else None
    status = agent.get("agent_status") if isinstance(agent, dict) else None
    return status if isinstance(status, str) else None

def plain_lines(ansi_text):
    """The pane's rows as plain text, with dim text dropped first: a suggested prompt the owner has not taken is dim, typed text is not."""
    return [SGR.sub("", DIM.sub("", line)).rstrip("\r") for line in ansi_text.split("\n")]

def pane_input(pane):
    """What waits in the pane's input line (see `input_line`), read with styling so a dim suggested prompt does not count as typed text."""
    return input_line(plain_lines(fc_herdr.herdr_text("agent", "read", pane, "--source", "visible", "--format", "ansi", "--lines", "12")))

def pane_quiet(pane):
    """Whether the agent is idle or done and nothing waits in its input line."""
    if pane_status(pane) not in ("idle", "done"):
        return False
    return pane_input(pane) == ""

def wait_settled(pane, timeout=SETTLE_LIMIT, poll=POLL):
    """Wait for the pane to take a prompt and finish it: up to START_WAIT seconds for `working` or `blocked`, then until `idle` or `done`.
    False on timeout or when it ends up `blocked`."""
    end = time.monotonic() + timeout
    start_by = time.monotonic() + START_WAIT
    started = False
    while time.monotonic() < end:
        status = pane_status(pane)
        if status == "blocked":
            return False
        if status == "working":
            started = True
        elif status in ("idle", "done") and (started or time.monotonic() >= start_by):
            return True
        time.sleep(poll)
    return False


# ---------- the owner's own prompts ----------
def utc_second_before_now():
    """The UTC second before this one as `%Y-%m-%dT%H:%M:%SZ`: a transcript time with milliseconds in the same second sorts before that second's
    own whole-second text, so an owner prompt that arrives a second early aborts a compaction, which is the safe side."""
    return time.strftime(fc_compactlog.TIME_FORMAT, time.gmtime(time.time() - 1))

class Watched:
    """A transcript's rows, re-read only when the file's size or modification time changed since the last read (a failed stat re-reads)."""
    def __init__(self, path):
        self.path, self.sig, self._rows = Path(path), None, []

    def rows(self):
        try:
            st = os.stat(self.path)
            sig = (st.st_size, st.st_mtime_ns)
        except OSError:
            sig = None
        if sig is None or sig != self.sig:
            self._rows, self.sig = fc_transcript.records(self.path), sig
        return self._rows

def _watched(transcript):
    return transcript if isinstance(transcript, Watched) else Watched(transcript)

def owner_typed_since(transcript, pane, since_iso):
    """Whether the transcript (a path) holds a prompt stamped after `since_iso` that compose did not type into this pane."""
    return any(not fc_compactlog.was_typed(pane, p["text"], since_iso) for p in fc_transcript.owner_prompts(_watched(transcript).rows(), after=since_iso))

def trigger_for(mode):
    """The compaction log's name for a run: `compose` for a judged one, `ceiling` for one at the ceiling."""
    return "compose" if mode == "judged" else "ceiling"

def wait_quiet(pane, transcript, mode):
    """Wait until the pane has been quiet for the idle window with no owner prompt in it. False after GATE_LIMIT seconds.
    The pane's in-flight marker is refreshed every MARKER_REFRESH seconds, since the wait outlasts its expiry."""
    watched = _watched(transcript)
    hold = fc_config.COMPACT["idle_seconds"] if mode == "judged" else CEILING_HOLD
    give_up = time.monotonic() + GATE_LIMIT
    refreshed = time.monotonic()
    began = since = None
    while time.monotonic() < give_up:
        if time.monotonic() - refreshed >= MARKER_REFRESH:
            fc_compactlog.set_inflight(pane, "idle", os.getpid(), mode)
            refreshed = time.monotonic()
        if pane_quiet(pane):
            now = time.monotonic()
            if began is None:
                began, since = now, utc_second_before_now()
            if owner_typed_since(watched, pane, since):
                began = None  # the owner is at the keyboard: the window starts over
            elif now - began >= hold:
                return True
        else:
            began = None
        time.sleep(POLL)
    return False


# ---------- the run ----------
def step(pane, role, name, outcome=None, **fields):
    fc_compactlog.append("step", pane=pane, role=role, step=name, **({"outcome": outcome} if outcome else {}), **fields)

def finish(pane, role, name, outcome, **fields):
    """End the run on a step that did not lead to a compaction: log it and release the pane."""
    step(pane, role, name, outcome, **fields)
    fc_compactlog.clear_inflight(pane)

def compact(a):
    pane, role, transcript, mode, handoff = a.pane, a.role, Watched(a.transcript), a.mode, a.handoff
    pid = os.getpid()
    if role not in HANDOFF_PROMPTS:
        return step(pane, role, "idle", "unknown-role")  # no marker of ours was set: another compactor's may hold this pane
    fc_compactlog.set_inflight(pane, "idle", pid, mode)
    if not wait_quiet(pane, transcript, mode):
        return finish(pane, role, "idle", "gave-up")
    sent_after = utc_second_before_now()
    step(pane, role, "handoff", mode=mode)
    if not fc_manager.tell(pane, HANDOFF_PROMPTS[role].format(path=handoff)):
        return finish(pane, role, "handoff", "refused")
    fc_compactlog.set_inflight(pane, "handoff", pid, mode)
    if not wait_settled(pane):
        return finish(pane, role, "handoff", "not-settled")
    if owner_typed_since(transcript, pane, sent_after):
        fc_compactlog.append("compaction", pane=pane, role=role, trigger=trigger_for(mode), transcript=str(transcript.path), outcome="aborted-owner-typing")
        return fc_compactlog.clear_inflight(pane)
    give_up = time.monotonic() + REQUIET_LIMIT
    while not pane_quiet(pane):
        if time.monotonic() >= give_up:
            return finish(pane, role, "compact", "not-quiet")
        time.sleep(POLL)
    before = len(fc_transcript.compactions(transcript.rows()))
    step(pane, role, "compact", mode=mode)
    if not fc_manager.tell(pane, FOCUS.format(path=handoff)):
        return finish(pane, role, "compact", "refused")
    fc_compactlog.set_inflight(pane, "compact", pid, mode)
    give_up = time.monotonic() + COMPACT_LIMIT
    new = None
    while new is None:
        found = fc_transcript.compactions(transcript.rows())
        if len(found) > before:
            new = found[before]
        elif time.monotonic() >= give_up:
            break
        else:
            time.sleep(POLL)
    done = new is not None
    new = new or {}
    fc_compactlog.append("compaction", pane=pane, role=role, trigger=trigger_for(mode), transcript=str(transcript.path), pre=new.get("pre"),
                         post=new.get("post"), duration_ms=new.get("duration_ms"), outcome="done" if done else "timeout")
    if done:
        state = {k: v for k, v in fc_compactlog.pane_state(pane).items() if k != "retry_after"}
        if new.get("post") is not None:  # a boundary without postTokens leaves the old baseline in place
            state["baseline"] = new["post"]
        fc_compactlog.save_pane_state(pane, {**state, "failures": 0})
    fc_compactlog.clear_inflight(pane)

def cmd_compactor(a):
    """The detached process body. It never raises: whatever goes wrong is a log record, and the pane is released."""
    try:
        compact(a)
    except Exception as e:
        try:
            finish(a.pane, a.role, "error", "error", error=type(e).__name__)
        except Exception:
            pass

def start(pane, role, transcript, mode, handoff, repo):
    """Start the compactor detached from this process and its terminal; its pid."""
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve().with_name("compose.py")), "compactor", "--repo", repo, "--pane", pane,
                             "--role", role, "--transcript", str(transcript), "--mode", mode, "--handoff", str(handoff)],
                            start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid
