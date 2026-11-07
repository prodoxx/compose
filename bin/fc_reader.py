"""The test reader: after a RED attempt, a read-only model (models.test_reader, Claude Haiku by default) turns the test output and
the attempt's diff into the implementer's next brief, as a reflection in three labelled lines: DID (what the change did), WHY (each
failing case, expected versus got, file:line) and NEXT (the one change to try, a hypothesis). Every earlier reflection goes with it,
so a later attempt never tries what an earlier one ruled out. The test, never the reflection, decides whether an attempt passed."""
import fc_config, fc_claude, fc_harness, fc_proc

READER_TIMEOUT = 120  # seconds the test reader may take before the raw tail goes back alone
DIFF_CAP = 4000  # characters of the attempt's diff the reader sees: the test output, not the whole change, is what it reads first
BRIEF_CAP = 1500


def reflections(log):
    """The reader's reflections of a task's log, oldest first: every attempt's non-empty summary."""
    return [e["summary"] for e in log if e.get("summary")]


def _numbered(items):
    return "\n".join(f"{i}. " + r.strip().replace("\n", "\n   ") for i, r in enumerate(items, 1))


def retry_note(log):
    """What the retry prompt says about the attempts before the newest: each earlier reflection, numbered. The newest stands under
    "What failed" in the same prompt, so it is left out here; "" when there is none."""
    earlier = reflections(log)[:-1]
    if not earlier:
        return ""
    return "Earlier attempts and why each missed (the reader's hypotheses; the test decides):\n" + _numbered(earlier) + "\n\n"


def read_failure(wt, meta, output, log_path, diff="", earlier=()):
    """(brief, usage): the test reader's reflection on a failed test run, for the implementer's next attempt. The reader runs
    headless in the task's clone with read-only tools, so it can point at the file:line but never edit or run anything. ("", None)
    when no Claude reader is set, or it fails or times out: the raw tail always follows, so this only ever adds."""
    if not fc_config.TEST_READER or not (fc_claude.is_claude(fc_config.TEST_READER) or fc_harness.harness_of(fc_config.TEST_READER)):
        return "", None
    cut = diff[:DIFF_CAP] + (f"\n... (cut at {DIFF_CAP} characters)" if len(diff) > DIFF_CAP else "")
    prompt = (f"A test run failed in this repository after an implementer's attempt.\nTest command: {meta['test']}\nFull output: {log_path}\nOutput (tail):\n{output[-6000:]}\n\n"
              + (f"The attempt's change (diff):\n{cut}\n\n" if diff else "")
              + (f"Earlier attempts' reflections, oldest first:\n{_numbered(earlier)}\n\n" if earlier else "")
              + f"Files the fix may change: {', '.join(meta['files']) or 'any'}. Open the test file and those files with Read when the output "
              "and the diff alone do not show where the problem is; you cannot edit files or run commands. "
              "Reply with at most 8 short lines in three labelled parts and nothing else:\n"
              "DID: what the change did, in one line.\n"
              "WHY: each failing case, what it expected versus what it got, the file:line to look at, and why the change missed it.\n"
              "NEXT: the one change to try next, as a hypothesis in words, no code."
              + (" Never repeat a NEXT an earlier reflection already tried; say what the earlier ones ruled out." if earlier else ""))
    try:
        if fc_harness.harness_of(fc_config.TEST_READER):
            p = fc_proc.run(fc_harness.once_command(fc_config.TEST_READER, wt, prompt, read_only=True), cwd=wt, env=fc_claude.headless_env(), timeout=READER_TIMEOUT)
            _, reply, _, usage = fc_harness.once_result(fc_config.TEST_READER, p)
        else:
            _, reply, _, usage = fc_claude.claude_once(wt, prompt, timeout=READER_TIMEOUT, model=fc_claude.claude_id(fc_config.TEST_READER), tools="Read,Grep,Glob")
    except Exception:  # noqa: BLE001 - a timeout or a refused call leaves the raw tail
        return "", None
    return reply.strip()[:BRIEF_CAP], usage
