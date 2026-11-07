"""Task retrospectives, and the report over the run history: what each missed task's attempts did and why they missed, in the
words the test reader left on the task's log. A retrospective is written when a task ends without passing (results/retro.jsonl),
goes to the task's Linear issue as its failure comment, and is what the planner reads before a re-split, the toolsmith reads for
the misses that point at compose rather than the code, and the team lead turns into a trail learning at the end of a feature.
`compose retro` prints them, drafts the learning, and reports first-try pass rates over every run of the repository."""
import json, sys, time
from collections import Counter, defaultdict
from pathlib import Path
import fc_core, fc_history, fc_tiers, fc_routecost

FILE = "retro.jsonl"
KINDS = ("code", "brief", "scope", "harness", "environment")
NEXT = {"code": "the implementer had the facts and still missed: re-split into smaller tasks that name the function and behavior each attempt missed (7), unless a WHY line shows a fact the brief lacked (then close the gap in the brief and run it once more).",
        "brief": "the brief contradicts the code: fix the brief inside a change of course, then run the task once more (7, 8).",
        "scope": "the test passed but the change broke a limit: cut the task so its named files hold the whole change, or raise the limit the brief rests on (2, 7).",
        "harness": "compose or a provider failed, not the code: resume or run again; a repeat is a tool bug for the toolsmith, never a re-split.",
        "environment": "the base or the chain is the problem: fix it (the test fails before any change, or the chain no longer rebases), then run again."}
PASSED = ("passed", "already_green", "unproven", "hand_done")


def kind_of(result):
    """Which of the five kinds a missed task is: the status says it for everything but `failed`, where the log does (an attempt log
    that never reached a test run is the harness's, one that did is the code's)."""
    s = result.get("status") or ""
    if s == "contradiction":
        return "brief"
    if s.startswith("rejected_"):
        return "scope"
    if s in ("payment_required", "interrupted"):
        return "harness"
    if s in ("broken_base", "unchained"):
        return "environment"
    return "code" if any("test_exit" in e for e in result.get("log") or []) else "harness"


def why_line(entry):
    """The reader's WHY line of an attempt's reflection, else its first line; "" without one."""
    lines = [l.strip() for l in (entry.get("summary") or "").splitlines() if l.strip()]
    return next((l for l in lines if l.upper().startswith("WHY:")), lines[0] if lines else "")


def attempt_lines(result):
    """One line per attempt of the task's log: what the CLI saw (RED or GREEN, the limits it broke, the refusals) and the reader's WHY."""
    out = []
    for e in result.get("log") or []:
        n = e.get("attempt")
        if n is None:
            continue
        if e.get("rate_limited"):
            out.append(f"attempt {n}: provider busy, no turn")
        elif e.get("payment_required"):
            out.append(f"attempt {n}: payment required ({e['payment_required'].get('provider', '?')})")
        elif e.get("contradiction"):
            out.append(f"attempt {n}: contradiction: {e['contradiction'][:200]}")
        elif e.get("error"):
            out.append(f"attempt {n}: {e['error'][:160]}")
        elif "test_exit" in e:
            parts = ["GREEN" if e["test_exit"] == 0 else f"RED (exit {e['test_exit']})"]
            if e.get("lines"):
                parts.append(f"{e['lines']} lines")
            if e.get("stray"):
                parts.append("outside files: " + ", ".join(e["stray"]))
            if e.get("new_docs"):
                parts.append("new docs: " + ", ".join(e["new_docs"]))
            if e.get("broke"):
                parts.append("broke: " + ", ".join(e["broke"])[:160])
            if e.get("tampered"):
                parts.append("tampered: " + ", ".join(e["tampered"]))
            if e.get("refused"):
                parts.append(f"{e['refused']} refused")
            why = why_line(e)
            out.append(f"attempt {n}: " + ", ".join(parts) + (f" · {why}" if why else ""))
    return out


def text(result, kind=None):
    """The retrospective as the issue's comment and the planner's reading: the outcome, each attempt, and what a miss of this kind calls for."""
    kind = kind or kind_of(result)
    lines = [f"Retrospective: {result.get('status')} after {result.get('attempts', 0)} attempt(s), a {kind} miss, model `{result.get('model', '')}`, ${result.get('cost') or 0:.4f}."]
    lines += [f"- {l}" for l in attempt_lines(result)] or ["- no attempt ran"]
    lines.append(f"Next: {NEXT[kind]}")
    return "\n".join(lines)


def task_facts(proj, meta):
    """The task's route (its workflow) and the tier of its model, for its history line: what `--history` groups by."""
    return {"route": fc_routecost.route(proj, meta)[0], "tier": fc_tiers.tier_of(meta.get("model") or "")}


def path(proj):
    return Path(proj) / "results" / FILE


def record(proj, meta, result):
    """Append the task's retrospective to results/retro.jsonl and return its text. A file that cannot be written never fails the run."""
    kind = kind_of(result)
    body = text(result, kind)
    line = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "task": result.get("id"), "title": meta.get("title") or result.get("title") or "", "status": result.get("status"),
            "kind": kind, "attempts": result.get("attempts", 0), "model": result.get("model", ""), "route": fc_routecost.route(proj, meta)[0],
            "files": list(meta.get("files") or []), "lines": attempt_lines(result), "text": body}
    try:
        path(proj).parent.mkdir(parents=True, exist_ok=True)
        with open(path(proj), "a") as f:
            f.write(fc_core.scrub(json.dumps(line)) + "\n")
    except OSError:
        pass
    return body


def read(proj):
    """Every retrospective of the project, oldest first; a line that does not parse is skipped."""
    try:
        raw = path(proj).read_text()
    except OSError:
        return []
    out = []
    for l in raw.splitlines():
        try:
            e = json.loads(l)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def harness_lines(proj):
    """The misses that point at compose or a provider rather than the code, one line each, for the toolsmith's brief."""
    return [f"{r['task']} {r['title']}: {r['status']} after {r['attempts']} attempt(s); {'; '.join(r['lines'][-2:]) or 'no attempt ran'}"
            for r in read(proj) if r.get("kind") == "harness"]


def learnings_draft(proj, retros=None):
    """A trail learning drafted from the project's retrospectives, in `trail note` shape, for the team lead to cut down to the facts about
    the repo and save; "" without any retrospective. A draft, never a learning: the team lead reads each line against its evidence first."""
    retros = read(proj) if retros is None else retros
    if not retros:
        return ""
    by_file = defaultdict(list)
    for r in retros:
        for f in r.get("files") or []:
            by_file[f] += [l for l in r.get("lines") or [] if "·" in l]
    tried = [f"- task {r['task']} {r['title']} ended {r['status']} ({r['kind']}, {r['attempts']} attempt(s)): " + ("; ".join(r["lines"]) or "no attempt ran") for r in retros]
    watch = [f"- {f}: " + " | ".join(dict.fromkeys(l.split("·", 1)[1].strip() for l in ls)) for f, ls in sorted(by_file.items()) if ls]
    return "\n".join([f"# What building {Path(proj).name} taught about this repo (draft from {len(retros)} retrospective(s); keep only facts about the repo)", "",
                      "## Decided", "- (what you settled while escalating these tasks, if anything)", "",
                      "## Tried and ruled out", *tried, "",
                      "## Watch out", *(watch or ["- (nothing the reader tied to a file)"])]) + "\n"


def rate(n, d):
    return f"{n} ({100 * n // d}%)" if d else "0"


def history_report(repo, project=None):
    """First-try and final pass rates over every task line of the repository's history (one project's with `project`), by model, by
    route and by outcome, then the verify runs. The numbers compose's defaults should answer to; a change to them stays the product owner's."""
    entries = fc_history.read(repo, project, last=0)
    tasks = [e for e in entries if e.get("kind") == "task"]
    if not tasks:
        return "retro  no task has run here yet"
    def block(name, key):
        groups = defaultdict(list)
        for e in tasks:
            groups[e.get(key) or "-"].append(e)
        lines = [f"by {name}:"]
        for k, es in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            ok = [e for e in es if e.get("outcome") in PASSED]
            first = [e for e in ok if e.get("attempts") == 1]
            lines.append(f"  {k:40.40} tasks {len(es):4}  passed {rate(len(ok), len(es)):>10}  first try {rate(len(first), len(es)):>10}  ${sum(e.get('cost') or 0 for e in es):.4f}")
        return lines
    ok = [e for e in tasks if e.get("outcome") in PASSED]
    first = [e for e in ok if e.get("attempts") == 1]
    out = [f"tasks {len(tasks)} · passed {rate(len(ok), len(tasks))} · first try {rate(len(first), len(tasks))} · ${sum(e.get('cost') or 0 for e in tasks):.4f}"]
    out += block("model", "model") + block("route", "route") + block("tier", "tier")
    out.append("by outcome: " + ", ".join(f"{k} {n}" for k, n in Counter(e.get("outcome") or "-" for e in tasks).most_common()))
    ends = [e for e in entries if e.get("kind") == "verify-end"]
    if ends:
        passes = [e for e in ends if e.get("outcome") == "pass"]
        out.append(f"verify: {len(ends)} run(s) · pass {rate(len(passes), len(ends))} · rounds mean {sum(e.get('rounds') or 0 for e in ends) / len(ends):.1f}")
    return "\n".join(out)


def cmd_retro(a):
    """`compose retro`: the project's retrospectives (`--task NNN` one in full), `--learnings` the trail learning drafted from them, or
    `--history` the pass rates over every run of the repository (every project's without --project)."""
    repo = Path(a.repo).resolve()
    if getattr(a, "history", False):
        print(history_report(repo, a.project))
        return
    proj = fc_core.project_dir(a)
    if getattr(a, "learnings", False):
        draft = learnings_draft(proj)
        print(draft.rstrip() if draft else "retro  none: no task of this project ended without passing")
        return
    retros = read(proj)
    if getattr(a, "task", None):
        hit = [r for r in retros if r.get("task") == a.task]
        if not hit:
            sys.exit(f"retro  none for task {a.task}")
        print(hit[-1]["text"])
        return
    if not retros:
        print("retro  none: no task of this project ended without passing")
        return
    for r in retros:
        print(f"{r['at']}  {r['task']}  {r['status']:12} {r['kind']:12} x{r['attempts']}  {r['title']}")
        for l in r["lines"]:
            print(f"    {l}")
