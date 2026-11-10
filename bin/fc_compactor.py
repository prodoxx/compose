import os, re, subprocess, sys, time
from pathlib import Path
import fc_compactlog, fc_config, fc_herdr, fc_fleet, fc_manager, fc_transcript

# The detached process the Stop hook starts to compact an engineering manager or team lead session at an idle moment: it waits for the
# pane to be quiet, has the session write its handoff, types `/compact` with a fixed focus, and once the compaction landed types the prompt
# that resumes the session from its handoff (Claude Code starts no turn by itself after a typed `/compact`, and the re-entry note the
# SessionStart hook prints only rides along with the next prompt). Every step lands in the compaction log and every exit path clears the
# pane's in-flight marker. It types only into a pane that is idle with an empty input line.
# The idle window counts quiet seconds: a wake from a parked watch or a prompt compose typed makes the pane busy for a moment, which pauses
# the count, while a prompt from the product owner disarms the run so the next Stop judges again. A prompt the engineering manager types
# itself through the herdr CLI in its own Bash tool is not in compose's log, so it disarms a team lead's run too: the safe side.

PROMPT_MARKS = ("❯", ">")
DIM = re.compile(r"\x1b\[2m.*?\x1b\[(?:0|22)?m")  # Claude Code's suggested next prompt sits dim in the input box until the owner takes it
SGR = re.compile(r"\x1b\[[0-9;]*m")
POLL = 2.0
GATE_LIMIT = 1800.0  # how long it waits for the idle window to fill before giving up
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
_RESUME = ("compose: your context was compacted and this session continues. Read your handoff at {path} first, then run the `compose status` and "
           "`compose watch --now` lines %s, act on what they show, and end your turn; say nothing to the product owner unless something waits on them.")
RESUME = {"manager": _RESUME % "for each project you hold", "lead": _RESUME % "for your project"}


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
    agent = fc_fleet.agent(pane)
    status = agent.get("agent_status") if isinstance(agent, dict) else None
    return status if isinstance(status, str) else None

def plain_lines(ansi_text):
    """The pane's rows as plain text, with dim text dropped first: a suggested prompt the owner has not taken is dim, typed text is not."""
    return [SGR.sub("", DIM.sub("", line)).rstrip("\r") for line in ansi_text.split("\n")]

def pane_input(pane):
    """What waits in the pane's input line (see `input_line`), read with styling so a dim suggested prompt does not count as typed text."""
    return input_line(plain_lines(fc_fleet.read(pane, lines=12, source="visible", fmt="ansi")))

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
    """Wait until the pane has been quiet for the idle window in all: "quiet" then, "owner" as soon as the product owner prompts after the
    wait began, "gave-up" after GATE_LIMIT seconds. Only the time between two quiet polls counts, and a busy poll pauses the count, never
    resets it. The pane's in-flight marker is refreshed every MARKER_REFRESH seconds, since the wait outlasts its expiry."""
    watched = _watched(transcript)
    hold = fc_config.COMPACT["idle_seconds"] if mode == "judged" else CEILING_HOLD
    give_up = time.monotonic() + GATE_LIMIT
    refreshed = time.monotonic()
    since, quiet, last = utc_second_before_now(), 0.0, None
    while time.monotonic() < give_up:
        if time.monotonic() - refreshed >= MARKER_REFRESH:
            fc_compactlog.set_inflight(pane, "idle", os.getpid(), mode)
            refreshed = time.monotonic()
        if owner_typed_since(watched, pane, since):
            return "owner"
        if pane_quiet(pane):
            now = time.monotonic()
            quiet += now - last if last is not None else 0.0
            last = now
            if quiet >= hold:
                return "quiet"
        else:
            last = None  # busy for a moment: the count pauses
        time.sleep(POLL)
    return "gave-up"


# ---------- the run ----------
def step(pane, role, name, outcome=None, **fields):
    """Log one step of the run (idle, handoff, compact, error), with its outcome when the run ends on it."""
    fc_compactlog.append("step", pane=pane, role=role, step=name, **({"outcome": outcome} if outcome else {}), **fields)

def finish(pane, role, name, outcome, **fields):
    """End the run on a step that did not lead to a compaction: log it and release the pane."""
    step(pane, role, name, outcome, **fields)
    fc_compactlog.clear_inflight(pane)

def stopped(pane, role, mode, transcript, outcome):
    """End the run as a compaction that did not happen because the product owner spoke, and release the pane."""
    fc_compactlog.append("compaction", pane=pane, role=role, trigger=trigger_for(mode), transcript=str(transcript.path), outcome=outcome)
    fc_compactlog.clear_inflight(pane)

def compact(a):
    """The run: wait for the idle window, have the session write its handoff, wait for it to settle, then type `/compact` and log the boundary
    it lands as the compaction, with the pane's new baseline."""
    pane, role, transcript, mode, handoff = a.pane, a.role, Watched(a.transcript), a.mode, a.handoff
    pid = os.getpid()
    if role not in HANDOFF_PROMPTS:
        if (fc_compactlog.inflight(pane) or {}).get("pid") == pid:
            fc_compactlog.clear_inflight(pane)  # the marker the hook set for this run; another compactor's is left alone
        return step(pane, role, "idle", "unknown-role")
    fc_compactlog.set_inflight(pane, "idle", pid, mode)  # the hook set it with this pid: this refreshes it
    held = wait_quiet(pane, transcript, mode)
    if held == "owner":
        return stopped(pane, role, mode, transcript, "disarmed-owner-typing")
    if held != "quiet":
        return finish(pane, role, "idle", "gave-up")
    sent_after = utc_second_before_now()
    step(pane, role, "handoff", mode=mode)
    if not fc_manager.tell(pane, HANDOFF_PROMPTS[role].format(path=handoff)):
        return finish(pane, role, "handoff", "refused")
    fc_compactlog.set_inflight(pane, "handoff", pid, mode)
    if not wait_settled(pane):
        return finish(pane, role, "handoff", "not-settled")
    if owner_typed_since(transcript, pane, sent_after):
        return stopped(pane, role, mode, transcript, "aborted-owner-typing")
    before = len(fc_transcript.compactions(transcript.rows()))  # counted first: on a long transcript this read takes a second
    give_up = time.monotonic() + REQUIET_LIMIT
    while not pane_quiet(pane):
        if time.monotonic() >= give_up:
            return finish(pane, role, "compact", "not-quiet")
        time.sleep(POLL)
    step(pane, role, "compact", mode=mode)
    compact_at = utc_second_before_now()
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
        resume(pane, role, transcript, handoff, mode, pid, compact_at)
    fc_compactlog.clear_inflight(pane)

def resume(pane, role, transcript, handoff, mode, pid, compact_at):
    """Type the prompt that starts the session's first turn after the compaction, from its handoff. Not when the product owner prompted since
    `/compact` went in (their prompt carries the re-entry note already) or the pane does not go quiet in REQUIET_LIMIT seconds. The prompt is
    logged as typed before it lands, so the prompt hook never takes it for the product owner's; the step and its outcome are logged."""
    fc_compactlog.set_inflight(pane, "resume", pid, mode)
    give_up = time.monotonic() + REQUIET_LIMIT
    while not pane_quiet(pane):
        if time.monotonic() >= give_up:
            return step(pane, role, "resume", "not-quiet")
        time.sleep(POLL)
    if owner_typed_since(transcript, pane, compact_at):
        return step(pane, role, "resume", "owner-typing")
    text = RESUME[role].format(path=handoff)
    try:
        fc_compactlog.typed(pane, text)
    except OSError:
        pass
    step(pane, role, "resume", "sent" if fc_manager.tell(pane, text) else "refused", mode=mode)

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
