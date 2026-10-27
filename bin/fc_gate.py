"""The pass gate of a task: what its branch may change (size, documents, files) and what must still pass after it.
Imports only lower modules."""
import contextlib, re, shutil, tempfile, threading
from pathlib import Path
import fc_core, fc_config, fc_proc, fc_tasks, fc_worktree

def branch_budget(wt, base, allowed=None, delete=()):
    """(lines changed vs base counting the working tree, new document files, files outside the task's `files:` list).
    All three are hard limits for an implementer: size, no documents, and scope. A file deleted whole counts one line, as
    does each `delete:` path; files under a `delete:` path count nothing and are never stray."""
    fc_core.sh(["git", "add", "-A"], wt, check=False)
    del_paths = {d.rstrip("/") for d in delete}
    gone = set(fc_core.sh(["git", "diff", "--cached", "--diff-filter=D", "--name-only", base], wt, check=False).stdout.split())
    lines = len(del_paths)
    for row in fc_core.sh(["git", "diff", "--cached", "--numstat", base], wt, check=False).stdout.splitlines():
        a, d, f = row.split("\t", 2)
        if any(f == p or f.startswith(p + "/") for p in del_paths):
            continue
        if f in gone:
            lines += 1
        elif not fc_worktree.GENERATED_RE.search(f):  # lockfiles are regenerated, nobody reads them
            lines += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    changed = fc_core.sh(["git", "diff", "--cached", "--name-only", base], wt, check=False).stdout.split()
    new_docs = [f for f in fc_core.sh(["git", "diff", "--cached", "--name-only", "--diff-filter=A", base], wt, check=False).stdout.split()
                if fc_config.DOC_RE.search(f)]
    stray = [f for f in changed if allowed is not None and f not in allowed and f not in new_docs
             and not any(f == p or f.startswith(p + "/") for p in del_paths)]
    return lines, new_docs, stray

FAILING_RE = re.compile(r"^\s*(?:(?:FAIL|ERROR): |FAILED (?!\()|not ok \d+ - |--- FAIL: )(\S.*?)(?: \(\d[\d.]*s\))?\s*$", re.M)  # unittest, pytest, node --test, go test; not unittest's "FAILED (failures=3)" summary
BASELINES, BASELINE_LOCK = {}, threading.Lock()

CHECKS_TIMEOUT = 900  # seconds a whole-suite check may run: a task's own `test_timeout` is sized for one test, and a suite cut off by it leaves no ids

def check_state(run, cmds, wt, limit):
    """What each of the project's checks does in this checkout: its exit code and the failing test ids it printed.
    `run` is fc_implement.timed. A check that timed out (124), or exited non-zero without printing one id, is "unknown": its ids are not a complete list."""
    state = {}
    for cmd in cmds:
        r = run(cmd, wt, max(limit, CHECKS_TIMEOUT), env=fc_core.clean_env())
        failing = sorted(set(FAILING_RE.findall(r.stdout + r.stderr)))
        state[cmd] = {"exit": r.returncode, "failing": failing, "unknown": r.returncode == 124 or (r.returncode != 0 and not failing)}
    return state

@contextlib.contextmanager
def base_tree(wt, base, delete):
    """A scratch clone of `wt` at `base` with the task's `delete`s applied: what the task started from, whatever it has edited since."""
    root = Path(tempfile.mkdtemp(prefix="compose-base-"))
    try:
        tree = root / "tree"
        fc_core.sh(["git", "clone", "--quiet", "--shared", "--no-hardlinks", str(wt), str(tree)], wt)
        fc_core.sh(["git", "checkout", "-q", base], tree)
        if delete:
            fc_core.sh(["git", "rm", "-r", "-q", "--"] + list(delete), tree)
        fc_worktree.ready_checkout(wt, tree)
        yield tree
    finally:
        shutil.rmtree(root, ignore_errors=True)

def baseline_state(run, cmds, wt, base, limit):
    """check_state on a task's base, in a scratch clone of it, once per base: tasks that start from the same commit share it.
    `base` is (commit, deletes). None when the project has no checks. A task asks for it only when a check fails after the task:
    a suite that passes needs no base run. A state with an unknown check is not kept: the next task on that base takes it again."""
    if not cmds:
        return None
    with BASELINE_LOCK:
        if (base, tuple(cmds)) not in BASELINES:
            with base_tree(wt, *base) as tree:
                state = check_state(run, cmds, tree, limit)
            if any(c["unknown"] for c in state.values()):
                return state
            BASELINES[base, tuple(cmds)] = state
        return BASELINES[base, tuple(cmds)]

IMPORT_RE = re.compile(r"^\s*(?:import|from|use|require|#include)\b|\b(?:require|import)\s*\(", re.M)

def touches(wt, base, changed=None):
    """A function: does the diff of `wt` against `base` touch the test file a failing id names, or a module that file imports?
    An id that names no file in the checkout touches nothing. `changed`: the changed files when they are not the diff of `wt`."""
    if changed is None:
        fc_core.sh(["git", "add", "-A"], wt, check=False)
        changed = fc_core.sh(["git", "diff", "--cached", "--name-only", base], wt, check=False).stdout.split()
    def test_file(tid):
        m = re.search(r"[\w./-]+\.(?:py|js|mjs|ts|tsx|jsx|go|rs|rb)\b", tid)
        if m:
            return m.group(0)
        m = re.search(r"\(([\w.]+)\)", tid)  # unittest: "name (pkg.mod.Class.name)"
        parts = m.group(1).split(".") if m else []
        return next(("/".join(parts[:i]) + ".py" for i in range(len(parts), 0, -1) if (wt / ("/".join(parts[:i]) + ".py")).is_file()), None)
    def check(tid):
        f = test_file(tid)
        if f is None or not (wt / f).is_file():
            return False
        if f in changed:
            return True
        lines = [l for l in (wt / f).read_text(errors="replace").splitlines() if IMPORT_RE.search(l)]
        return any(re.search(rf"\b{re.escape(c.rsplit('/', 1)[-1].rsplit('.', 1)[0])}\b", l) for c in changed for l in lines)
    return check

def regressions(before, after, touches=lambda tid: True):
    """(broke, unknown): what passed on the base and fails now (a failing test id the base run did not print, or a check that exited 0 and no longer does),
    and what is red on a base whose run is unknown. A check that was already red tells nothing about the task beyond its new failures.
    A check with no recorded base run is unknown. Where the base is unknown a failing id is a regression only when `touches(id)`;
    the rest are ids the base may have printed, which `settle` then learns from the base tree."""
    broke, unknown = [], []
    for cmd, now in after.items():
        was = before.get(cmd) or {"exit": None, "failing": [], "unknown": True}  # no recorded base run is unknown too
        if now["exit"] != 0:
            new = [t for t in now["failing"] if t not in was["failing"]]
            if was.get("unknown"):
                broke += [t for t in new if touches(t)]
                unknown += [t for t in new if not touches(t)]
            else:
                broke += new or ([f"`{cmd}` exits {now['exit']}"] if was["exit"] == 0 else [])
    return broke, unknown

def settle(run, wt, base, delete, limit, red, after, learned):
    """(broke, why): `red`, the ids failing after a task on a base whose run is unknown, checked against the base tree itself.
    Each check that prints one runs on a scratch clone of `wt` at `base` with the task's `delete`s applied, once per run in `learned`.
    An id that fails there too is base-red; any other is a regression. `why` names the ids and the reason when that base run is unknown again."""
    todo = {c: [t for t in s["failing"] if t in red] for c, s in after.items()}
    todo = {c: ids for c, ids in todo.items() if ids}
    missing = [c for c in todo if c not in learned]
    if missing:
        with base_tree(wt, base, delete) as tree:
            learned.update(check_state(run, missing, tree, limit))
    broke, why = [], []
    for cmd, ids in todo.items():
        was = learned[cmd]
        if was["unknown"]:
            why.append(f"{', '.join(ids)}: the base run of `{cmd}` " + ("timed out" if was["exit"] == 124 else f"exited {was['exit']} and printed no failing test"))
        else:
            broke += [t for t in ids if t not in was["failing"]]
    return broke, why

def advice(broke):
    """The next prompt's problem when a passing task broke something that passed before it."""
    return ("Your change breaks what passed before it. Code outside this task, other tasks' stubs included, must stay as it was; "
            "restore it. Now failing: " + "; ".join(broke[:10]))

TEST_FILE_RE = re.compile(r"(?:^|/)(?:tests?|__tests__|spec)/|(?:^|/)test_[^/]*$|_test\.\w+$|\.(?:test|spec)\.\w+$")

def new_tests_prove(run, repo, fixed, cmds, install=None):
    """Whether a fix has a test of its own that proves it: on a clone of `repo`'s HEAD the fix's test files alone fail a test id
    of theirs that `cmds` print, and with the rest of the fix that id passes. False when the fix has no test file or nothing but test files."""
    tests = [f for f in fixed if TEST_FILE_RE.search(f)]
    if not tests or len(tests) == len(fixed) or not cmds:
        return False
    root = Path(tempfile.mkdtemp(prefix="compose-fix-"))
    try:
        tree = root / "tree"
        fc_core.sh(["git", "clone", "--quiet", "--shared", "--no-hardlinks", str(repo), str(tree)], repo)
        fc_worktree.ready_checkout(repo, tree, install=install)
        def put(files):
            for f in files:
                if (repo / f).exists():
                    (tree / f).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(repo / f, tree / f)
                else:
                    (tree / f).unlink(missing_ok=True)
        put(tests)
        without = check_state(run, cmds, tree, CHECKS_TIMEOUT)
        mine = touches(tree, "HEAD", tests)
        theirs = {c: [t for t in s["failing"] if mine(t)] for c, s in without.items()}
        put(fixed)
        within = check_state(run, cmds, tree, CHECKS_TIMEOUT)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails_without_passes_with(theirs, within, cmds)

def fails_without_passes_with(theirs, within, cmds):
    """Whether a fix's own tests prove it: some check's run with the whole fix is known and no longer fails one of the test ids `theirs[cmd]` that failed with the fix's test files alone."""
    return any(not within[c]["unknown"] and any(t not in within[c]["failing"] for t in theirs[c]) for c in cmds)

def after_checks(checks, tasks, changed, own):
    """The commands `run` must pass after an attempt, in place of the project's whole check list: every check that is not a
    whole suite (a suite is verify's, never run's), then the `test:` of each code task whose files or protected paths a changed
    path is in, in task order. `own`, the task's own `test:`, is never in the list, and no command appears twice."""
    cmds = [c for c in checks if not fc_proc.suite(c) and c != own]
    cmds += [t["test"] for t in fc_tasks.code_tasks(tasks)
             if any(p in changed for p in t.get("files", []) + t.get("protected", [])) and t.get("test") and t["test"] != own]
    return list(dict.fromkeys(cmds))
