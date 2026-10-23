import json, os, re, sys, time
import fc_core, fc_config, fc_tiers


# ---------- tasks ----------

# Linear is the record humans and agents share. `compose sync` reads the feature and its tasks from Linear with the workspace
# key, or they are piped in with --stdin, and it keeps them in the git-ignored results/tasks.json.
# A task is any issue under the feature (directly or under a story) whose description ends with this block:
#   ```compose
#   id: 001
#   files: server/graph/validate.go
#   protected: server/graph/validate_test.go
#   test: cd server && go test ./graph/ -run TestValidateAnswers
#   ```
# Everything above the block is the implementer's brief. `after` is Linear's own "blocked by" between the feature's tasks;
# any other blocker is `waits_on` while it is not finished, and holds the task back without stacking it.
TASK_BLOCK = re.compile(r"\n*```compose[ \t]*\n(.*?)\n```[ \t]*\n*", re.S)
SYNC_SHAPE = ('{"feature": {"id", "title", "description"}, "issues": [{"id", "title", "description", "status", "parentId", "labels", '
              '"blockedBy": ["<issue id>" or {"id", "status", "labels"}]}]}'
              ": every issue under the feature, its stories' children included")
WORKFLOWS = ("test-first", "implement-then-test", "fix-then-regression-test", "check-only")
DEPTHS = ("FAST", "STANDARD", "DEEP")

def route_text(desc):
    """The `## Route` section of an issue description, from its heading to the next `## ` heading or the compose fence; "" when there is none."""
    lines = desc.splitlines()
    if "## Route" not in lines:
        return ""
    start = lines.index("## Route")
    end = start + 1
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## ") or lines[i].startswith("```compose"):
            break
        if lines[i].strip():
            end = i + 1
    return "\n".join(lines[start:end])

def task_from_issue(issue):
    """A task meta (with its brief under "body") from a Linear issue whose description carries the compose block, else None."""
    desc = issue.get("description") or ""
    m = TASK_BLOCK.search(desc)
    if not m:
        return None
    route = route_text(desc)
    body_desc = re.sub(re.escape(route) + r"\s*", "", desc, 1) if route else desc
    meta = {k.strip(): v.strip() for k, v in (l.split(":", 1) for l in m.group(1).splitlines() if ":" in l)}
    if "id" not in meta or "test" not in meta:
        sys.exit(f"{issue['id']}: its compose block needs id and test")
    for key in ("delete", "files", "protected", "nodes"):
        meta[key] = [f.strip() for f in meta.get(key, "").split(",") if f.strip()]
    if "figma" in meta:  # a design task's own file, a key or a url; else the project's stored key
        meta["figma"] = fc_core.figma_key(meta["figma"]) or sys.exit(f"{issue['id']}: figma is not a Figma file url or key: {meta['figma']}")
    if "route" in meta and meta["route"] not in WORKFLOWS:
        sys.exit(f"{issue['id']}: route must be test-first, implement-then-test, fix-then-regression-test or check-only, not {meta['route']}")
    if "tier" in meta and meta["tier"] not in fc_tiers.PINS:
        sys.exit(f"{issue['id']}: tier must be cheap, standard, frontier or no-frontier, not {meta['tier']}")
    meta.setdefault("kind", "code")
    if "model" not in meta:
        meta["model_auto"] = True
    meta.setdefault("model", fc_config.DEFAULT_MODEL)
    meta.update({"title": issue.get("title", ""), "linear": issue["id"], "linear_state": issue.get("status", ""),
                 "blocked_by": [b if isinstance(b, dict) else {"id": b} for b in issue.get("blockedBy") or []],  # MCP gives {id, title}
                 "body": re.sub(r"(?m)^## Evidence[ \t]*$[\s\S]*?(?=\n## |\Z)", "", TASK_BLOCK.sub("\n", body_desc)).strip()})
    return meta

def hand_task(issue):
    """An issue labelled Task that carries no compose block: work done by hand (a lowfi flow in Frame0), never dispatched."""
    return not TASK_BLOCK.search(issue.get("description") or "") and any(l.lower() == "task" for l in issue.get("labels") or ())

def sync_tasks(proj, data):
    """Store the feature and its tasks, as the team lead read them from Linear, in results/tasks.json. Returns the tasks."""
    link = proj / "linear.json"
    feature = json.loads(link.read_text()).get("feature") if link.exists() else None
    if not feature:
        sys.exit(f'{link}: needs {{"feature": "<issue id, e.g. ENG-123>"}}, the Linear feature this project builds')
    if not isinstance(data, dict) or not isinstance(data.get("feature"), dict) or not isinstance(data.get("issues"), list):
        sys.exit(f"sync reads JSON on stdin shaped {SYNC_SHAPE}")
    if data["feature"].get("id") != feature:
        sys.exit(f"sync got feature {data['feature'].get('id')!r}, but {link} says {feature!r}")
    stories = set(json.loads(link.read_text()).get("stories") or [])
    if stories:  # a project scoped to stories sees those stories and their children, nothing else under the feature
        missing = [i["id"] for i in data["issues"] if i["id"] not in stories and "parentId" not in i]
        if missing:
            sys.exit(f"{link} scopes this project to stories, so sync needs each issue's parentId (the `parentId` field of each issue): "
                     f"missing on {', '.join(missing)}")
        data = {**data, "issues": [i for i in data["issues"] if i["id"] in stories or i.get("parentId") in stories]}
    # A canceled issue is no task even with its block still on: a re-plan or an undo canceled it, and it must not run or stack.
    tasks = [t for t in (task_from_issue(i) for i in data["issues"] if (i.get("status") or "").lower() not in CANCELED) if t]
    ids = [t["id"] for t in tasks]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        sys.exit(f"{feature}: task ids used twice: {', '.join(dupes)}")
    by_issue = {t["linear"]: t["id"] for t in tasks}
    states = {i["id"]: i.get("status") for i in data["issues"]}
    for t in tasks:  # a blocker outside this feature's tasks does not stack: it holds the task back until it is finished
        blockers = [dict(b, status=b.get("status") or states.get(b["id"])) for b in t.pop("blocked_by")]
        t["after"] = ",".join(sorted(by_issue[b["id"]] for b in blockers if b["id"] in by_issue))
        t["waits_on"] = [f"{b['id']} ({b['status'] or 'state unknown'})" for b in blockers
                         if b["id"] not in by_issue and not blocker_finished(b["status"], b.get("labels") or ())]
    tasks.sort(key=lambda t: t["id"])
    (proj / "results").mkdir(exist_ok=True)
    f = data["feature"]
    issues = {i["id"]: {"title": i.get("title", ""), "description": i.get("description") or "", "status": i.get("status") or "",
                        "blockedBy": sorted(b["id"] if isinstance(b, dict) else b for b in i.get("blockedBy") or []),
                        **({"parentId": i["parentId"]} if i.get("parentId") else {}), **({"labels": i["labels"]} if i.get("labels") else {})}
              for i in data["issues"]}  # whole, stories too: a change of course snapshots them and undo puts them back
    fc_core.atomic_write((proj / "results" / "tasks.json"), json.dumps({"synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "feature": {"id": f["id"], "title": f.get("title", ""), "description": f.get("description") or ""}, "tasks": tasks,
        "issues": issues}, indent=2))
    if any((proj / "tasks").glob("*.md")):
        fc_core.say(f"note: {proj / 'tasks'} still has task files; they are ignored, Linear is the record. Delete them.")
    return tasks


def linear_cache(proj):
    path = proj / "results" / "tasks.json"
    return json.loads(path.read_text()) if path.exists() else {"feature": {}, "tasks": []}

def load_tasks(proj, required=False, hint=""):
    """The tasks as the team lead last synced them from Linear. Commands that act on tasks require a sync."""
    if required and not (proj / "results" / "tasks.json").exists():
        sys.exit(f"no tasks synced for {proj.name}: read the feature and its tasks from Linear and pipe them to "
                 f"`compose sync --project {proj.name}` (JSON {SYNC_SHAPE}){hint}")
    return linear_cache(proj)["tasks"]

def blocker_finished(status, labels=()):
    """A blocker no longer holds back what it blocks: Done or Canceled, or In Review carrying the Design label."""
    s = (status or "").lower()
    return s in ("done", *CANCELED) or (s == "in review" and any(l.lower() == "design" for l in labels))

def waiting(tasks, released=()):
    """{task id: {"reason"}} for each task a blocker outside the feature's tasks holds back, as of the last sync. `released`: blocker
    ids git shows are in the trunk already (fc_blockers.released), which hold nothing."""
    out = {m["id"]: [w for w in m.get("waits_on") or [] if w.split()[0] not in released] for m in tasks}
    return {tid: {"reason": "waits on " + ", ".join(w)} for tid, w in out.items() if w}

def linear_done(meta):
    """Linear says the task is Done: its work is on trunk, whatever this project's own files say."""
    return (meta.get("linear_state") or "").lower() == "done"

def code_tasks(tasks):
    """The tasks that change code: a design task never clones, stacks, or is judged from files."""
    return [t for t in tasks if t.get("kind") != "design"]


FINISHED = ("passed", "hand_done")  # a layer whose branch is the record: a run keeps it unless told to --redo

def task_finished(proj, meta, redo=False, since=None):
    """Why a task needs no more work, or None: the one rule `run`, `resume`, and `testcrews` share. Done or Canceled on Linear,
    always; else, unless --redo, a passed result (its branch kept, or gone because it landed, which `status` reads as merged) or
    a hand_done one (finished by hand, wherever its commits are: it needs no branch and never moves Linear again).
    With --redo, `since` (epoch seconds) is when the redo run started: a result written after it is that run's own and stands,
    an older one describes a branch the redo set aside."""
    if meta.get("linear_state") in ("Done", "Canceled"):
        return f"its state is {meta['linear_state']}"
    f = proj / "results" / f"{meta['id']}.json"
    status = json.loads(f.read_text()).get("status") if f.exists() else None
    if status in FINISHED and (not redo or (since is not None and f.stat().st_mtime >= since)):
        return f"its result is {status}"
    return None


def parse_criteria(text):
    """Criteria under '## Acceptance criteria' in a task or issue body: one per top-level bullet.
    Top level is the first bullet's indent (Linear stores "-" as "*" and nests with 2 or 4 spaces); deeper bullets and
    wrapped lines fold into their parent, sub-items joined with "; "."""
    m = re.search(r"## Acceptance criteria\n(.*?)(?:\n## |\Z)", text or "", re.S)
    out, base = [], None
    for line in (m.group(1) if m else "").splitlines():
        body = line.strip()
        if not body:
            continue
        flat = line.expandtabs(4)
        indent = len(flat) - len(flat.lstrip())
        if body[0] in "-*" and (base is None or indent <= base):
            base = indent if base is None else base
            out.append([body[1:].strip()])
        elif out and body[0] in "-*":
            out[-1].append(body[1:].strip())
        elif out:
            out[-1][-1] += " " + body
    def join(parts):
        s = parts[0]
        for p in parts[1:]:
            s += (" " if s.endswith(":") else "; ") + p
        return s
    return [join(parts) for parts in out]

def scenarios_block(text):
    m = re.search(r"^```scenarios *\n(.*?)^```$", text or "", re.S | re.M)
    return m.group(1).strip() if m else None

def parse_scenarios(text):
    block = scenarios_block(text)
    if block is None:
        return None
    out, key = [], None
    for line in block.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if m:
            key = m.group(1)
            if key == "id" or not out:
                out.append({})
            out[-1][key] = m.group(2).strip()
        elif out and key:
            out[-1][key] += " " + line.strip()
    return out

def criteria_from_tasks(proj):
    """(task id, criterion text) for every criterion in every task file."""
    return [(meta["id"], c) for meta in load_tasks(proj) for c in parse_criteria(meta["body"])]


_hold_warned = []

def held(proj):
    """{task id: {"reason", "at"}} for every held task. A missing file holds nothing; so does an unreadable one, said once, so a broken file never freezes a run."""
    path = proj / "results" / "hold.json"
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        if not _hold_warned:
            _hold_warned.append(1)
            fc_core.say(f"hold.json unreadable ({e}); nothing is held until it is fixed: {path}")
        return {}

def held_behind(pending, hold, failed):
    """The pending tasks a hold keeps back: each held one, and each that waits on those with no failed dependency of its own."""
    stuck = {p for p in pending if p in hold}
    while True:
        more = {p for p, d in pending.items() if p not in stuck and set(d) & stuck and not set(d) & failed}
        if not more:
            return stuck
        stuck |= more


def yolo_state(proj):
    """The posture file, or None when yolo is off. Unreadable counts as off, so notifications keep firing. A file from
    before the mode was recorded reads as away."""
    try:
        y = json.loads((proj / "results" / "yolo.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return {**y, "mode": y.get("mode") or "away"}

def yolo_by(y):
    """Who turned yolo on or last changed it, as this session sees it (yolo is per project, so another engineering manager session
    on it changes how this one answers): `this session`, `an engineering manager session in herdr pane w1:p2`, or `unknown` for
    a file from before it was recorded."""
    by = y.get("by") or {}
    if not by:
        return "unknown"
    if by.get("session") and by["session"] == fc_core.session_id():
        return "this session"
    who = {"manager": "an engineering manager session", "orchestrator": "the team lead"}.get(by.get("role"), f"a {by.get('role')} session")
    return who + (f" in herdr pane {by['session']}" if by.get("session") else " outside herdr")


CANCELED = ("canceled", "cancelled")


def run_marker(proj):
    f = proj / "results" / "run.json"
    try:
        return json.loads(f.read_text()) if f.exists() else None
    except json.JSONDecodeError:
        return None

def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False

def run_alive(proj):
    m = run_marker(proj)
    return bool(m and pid_alive(m.get("pid")))

def mark_interrupted(proj):
    """Ctrl-C stopped the run on purpose: keep its marker for `resume`, and say so in it."""
    m = run_marker(proj)
    if m:
        try:
            fc_core.atomic_write(proj / "results" / "run.json", json.dumps({**m, "interrupted": True}, indent=2))
        except OSError:
            pass  # the stop goes on; watch then reads the run as crashed

def run_ended(proj):
    """How a run whose process is gone ended: "interrupted" when Ctrl-C stopped it, "crashed" when it just died, None without a marker or while it lives."""
    m = run_marker(proj)
    if not m or pid_alive(m.get("pid")):
        return None
    return "interrupted" if m.get("interrupted") else "crashed"
