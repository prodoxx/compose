"""The build loop's decision step: what is in flight, and which ready stories to start next."""
import hashlib, json, os, re, subprocess, sys, time, traceback
from pathlib import Path
import fc_change, fc_config, fc_core, fc_delivery, fc_herdr, fc_linear, fc_load, fc_plan, fc_prd, fc_readiness, fc_reconcile, fc_tasks
import fc_factorystate

JUDGE_LIMIT = 6
MOVE_UP_JUDGES = 2  # judges a turn spends on out-of-order tickets, beyond pick's
BACKOFF_TURNS = 3  # turns a story whose dispatch failed waits before it is tried again
PLANNING_HOURS = 24  # a planning conversation older than this no longer holds offers back
PRD_FIRST = ("event factory=prd-missing  (no PRD and no product delivery plan yet: plan the PRD with the product owner from compose prd seed "
             "(sections/0q.md), then the delivery plan from it; nothing is dispatched before they exist)")
PRD_OFFER = "event factory=prd-missing  (no PRD yet: offer to plan it with the product owner from compose prd seed (sections/0q.md); building goes on)"
OPEN = ("todo", "backlog")
FREED = ("in review", "done", "canceled")
SHAPING = "needs-shaping"

def story_of(plan, ident):
    """The plan's story with this id, or None."""
    for f in plan["features"].values():
        for s in f.get("stories") or []:
            if s["id"] == ident:
                return s
    return None

def classify(labels, r):
    """What a judged story is ready for: "build" (tasks cut and labeled Ready), "cut" (specced, no tasks yet), or None."""
    if not r["ready"] or SHAPING in labels:
        return None
    if r["has_tasks"]:
        return "build" if "Ready" in labels else None
    return "cut"

def in_flight(plan, state):
    """Ids of stories being built: In Progress in Linear, or dispatched here and not yet In Review, Done or Canceled."""
    live = set()
    for f in plan["features"].values():
        for s in f.get("stories") or []:
            if s["state"].lower() == "in progress" or (s["id"] in state["dispatched"] and s["state"].lower() not in FREED):
                live.add(s["id"])
    return sorted(live, key=fc_plan.issue_num)

def _why(missing, labels):
    if missing:
        return "; ".join(f"{m['item']}: {m['question']}" for m in missing)[:200]
    return ("labeled " + SHAPING) if SHAPING in labels else "not specced"

def backed_off(state, ident):
    """True while a story's failed dispatch keeps it out of the picks: through turn until_turn (state["turns"] counts the loop's turns)."""
    b = (state.get("backoff") or {}).get(ident)
    return isinstance(b, dict) and b.get("until_turn", 0) >= state.get("turns", 0)

def pick(entries, plan, state, free, judge):
    """Walk the entries in order and start up to free ready stories. judge(id, write=True) is the caller's readiness check; the caller binds the repo."""
    flying = set(in_flight(plan, state))
    out = {"picks": [], "ready_waiting": False, "offer": None, "failed": [], "specced": set()}
    candidate, judged = None, 0
    for e in entries:
        if judged >= JUDGE_LIMIT or (len(out["picks"]) == free and (out["ready_waiting"] or candidate)):
            break
        if e["id"] in flying or e["state"].lower() not in OPEN or e["waits_on"] or backed_off(state, e["id"]):
            continue
        if e["kind"] == "feature":
            candidate = candidate or {"id": e["id"], "layer": "stories", "why": "no stories yet"}
            continue
        labels = (story_of(plan, e["id"]) or {}).get("labels", [])
        judged += 1
        try:
            r = judge(e["id"], write=True)
        except (fc_readiness.NotAStory, RuntimeError, OSError) as ex:
            out["failed"].append((e["id"], str(ex)))
            continue
        if r.get("error"):
            out["failed"].append((e["id"], r["error"]))
            continue
        if r.get("specced"):
            out["specced"].add(e["id"])
        kind = classify(labels, r)
        if kind:
            if len(out["picks"]) < free:
                out["picks"].append((e, kind))
            else:
                out["ready_waiting"] = True
        elif r.get("specced") is False or SHAPING in labels:
            candidate = candidate or {"id": e["id"], "layer": "story", "why": _why(r.get("missing") or [], labels)}
    if not out["picks"] and not out["ready_waiting"]:
        out["offer"] = candidate
    return out

CLI_TIMEOUT = 1800  # crew start waits while the machine is loaded
LEAD_READS = ("SKILL.md, sections/0h.md, sections/0a.md, sections/0e.md, sections/0f.md, sections/0g.md, sections/4.md, sections/5.md, "
              "sections/6.md, sections/6a.md, sections/7.md, sections/8.md and ~/.claude/skills/herdr/SKILL.md")
HOW = {"build": "Its tasks are cut: run them through the 3b gate.",
       "cut": "It has no tasks yet: start the planner for this story only (0j, story), then run its tasks through the 3b gate."}

def lead_prompt(story, feature, title, kind):
    """The prompt that hands one story to a feature's team lead."""
    return (f"factory: build story {story} of feature {feature} ({title}). {HOW[kind]} Build only this story: verify and stack it "
            "as SKILL.md says, report done when it passes, and never start another story on your own.")

def lead_brief(slug, wt, feature, prompt):
    """The first brief of a new team lead: who it is, what to read, then the prompt."""
    return (f"You are the team lead of project {slug} in {wt}, building feature {feature} for the compose factory.\n"
            f"Read {LEAD_READS}, then continue from 0e.\n{prompt}\n")

def slug_for(title, taken):
    """A short kebab project name from a title: at most 4 words within 24 characters, numbered while taken."""
    words = [w for w in re.sub(r"[^a-z0-9]+", "-", title.lower()).split("-") if w][:4] or ["feature"]
    while len(words) > 1 and len("-".join(words)) > 24:
        words.pop()
    base = "-".join(words)[:24].strip("-")
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug

class _Taken:
    """Names `slug_for` must not make: the repo's projects, and any leftover branch or sibling worktree folder `init` would trip on."""
    def __init__(self, repo, names):
        self.repo, self.names = Path(repo), set(names)
        self.prefix = fc_config.settings(fc_config.read_config())[1].get("branch_prefix", "compose/")

    def __contains__(self, slug):
        return (slug in self.names or (self.repo.parent / f"compose-{slug}").exists()
                or fc_core.sh(["git", "rev-parse", "--verify", "-q", self.prefix + slug], self.repo, check=False).returncode == 0)

def _linear(proj):
    try:
        got = json.loads((Path(proj) / "linear.json").read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}

def project_for(repo, feature):
    """(slug, worktree, project dir) of the feature's compose project, or None."""
    for slug, (wt, proj) in sorted(fc_core.all_projects(Path(repo)).items()):
        if _linear(proj).get("feature") == feature:
            return slug, Path(wt), Path(proj)
    return None

def after_project(repo, plan, feature):
    """The project of the first blocker that is built (verified) but not merged, by id number, or None."""
    blockers = (plan["features"].get(feature) or {}).get("blockers") or []
    for b in sorted(blockers, key=lambda b: fc_plan.issue_num(b["id"])):
        if fc_plan.base_built(b["state"], b["labels"]) and not fc_tasks.blocker_finished(b["state"], b["labels"]):
            got = project_for(repo, b["id"])
            if got:
                return got[0]
    return None

def compose_cli(args, stdin=None, pane=None):
    """Run one compose command as the engineering manager, from its pane when given (the loop's own may be stale); its stdout, or RuntimeError with its stderr."""
    env = {k: v for k, v in os.environ.items() if k != "COMPOSE_ROLE"}
    if pane:
        env["HERDR_PANE_ID"] = pane
    try:
        p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "compose.py"), *map(str, args)], input=stdin,
                           stdin=None if stdin is not None else subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=CLI_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"compose {args[0]} did not finish in {CLI_TIMEOUT // 60} minutes")
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).strip() or f"compose {args[0]} exited {p.returncode}")
    return p.stdout

HERDR_DOWN = "herdr did not answer"

class HerdrDown(RuntimeError):
    """herdr's agent list failed: no lead can be told live from gone this turn."""

def leads_or_none(repo):
    """live_leads, or None when herdr's agent list call failed (a failure is not "no lead")."""
    got = fc_herdr.herdr("agent", "list")
    return None if not isinstance(got, dict) else fc_reconcile.leads_among(repo, got.get("agents") or [])

def prompt_lead(name, text):
    """Send a prompt to a live team lead; whether herdr took it."""
    return fc_herdr.herdr("agent", "prompt", name, text) is not None

def _add_story(proj, story):
    meta = _linear(proj)
    if isinstance(meta.get("stories"), list) and story not in meta["stories"]:
        meta["stories"].append(story)
        fc_core.atomic_write(proj / "linear.json", json.dumps(meta, indent=2) + "\n")

def _new_project(repo, plan, feature, title, story, pane=None):
    """The feature's project as (slug, worktree, project dir): an `init` that died before linking is taken up, else a new one is made."""
    projects = fc_core.all_projects(Path(repo))
    slug = slug_for(title, set())
    if slug in projects and not _linear(projects[slug][1]).get("feature"):
        wt, proj = projects[slug]
    else:
        slug = slug_for(title, _Taken(repo, projects))
        after = after_project(repo, plan, feature)
        compose_cli(["init", "--repo", str(repo), "--project", slug] + (["--after", after] if after else []), pane=pane)
        got = fc_core.all_projects(Path(repo)).get(slug)
        if not got:
            raise RuntimeError(f"compose init made no project {slug}")
        wt, proj = got
    fc_core.atomic_write(Path(proj) / "linear.json", json.dumps({"feature": feature, "stories": [story]}, indent=2) + "\n")
    return slug, Path(wt), Path(proj)

def dispatch(repo, plan, entry, kind, state, now):
    """Hand a ready story to its feature's team lead, making the project and starting the lead when missing. Returns the event line."""
    feature, story = entry["feature"], entry["id"]
    title = (plan["features"].get(feature) or {}).get("title") or feature
    leads = leads_or_none(repo)
    if leads is None:
        raise HerdrDown(f"{HERDR_DOWN}; the next turn tries again")
    slug, wt, proj = project_for(repo, feature) or _new_project(repo, plan, feature, title, story, state.get("manager_pane"))
    prompt = lead_prompt(story, feature, title, kind)
    lead = leads.get(slug)
    if lead:
        if not prompt_lead(lead, prompt):
            raise RuntimeError(f"could not prompt {lead}")
        _add_story(proj, story)
    else:
        _add_story(proj, story)  # a new lead reads its scope at start
        compose_cli(["crew", "start", "--repo", str(wt), "--project", slug, "--role", "orchestrator", "--title", "orchestrator", "--brief", "-"],
                    stdin=lead_brief(slug, wt, feature, prompt), pane=state.get("manager_pane"))
    state["dispatched"][story] = {"feature": feature, "project": slug, "kind": kind, "at": now}
    return (f"event factory=dispatched story={story} project={slug} kind={kind}  "
            f"(its team lead builds it; relay it: compose watch --project {slug} --from orchestrator)")

DECISION_ID = re.compile(r"^([A-Z]+)(\d+):")
TAG = re.compile(r'\s*\((?:product owner[^()"]*; said: ".*"|[^()]*)\)\s*$')  # the one tag saying who or when: '(product owner, <date>; said: "<their words>")', "(away, 2026-10-26)", "(C1)"
OPEN_QUESTIONS = re.compile(r"^#+[ \t]*Open questions[^\n]*$.*?(?=^#|\Z)", re.M | re.S)

def fingerprint(feature_desc, story_desc):
    """What a plan change would touch: the feature's live decision lines, and a hash of the story without its Open questions (the loop writes those)."""
    body = OPEN_QUESTIONS.sub("", story_desc or "").rstrip()
    return {"decisions": fc_change.decision_lines(feature_desc), "story": hashlib.sha256(body.encode()).hexdigest()}

def _decisions(lines):
    """({id: (sort key, text without tags)}, {text without tags of the lines with no id})."""
    ids, bare = {}, set()
    for line in lines:
        text = TAG.sub("", line).strip()
        m = DECISION_ID.match(text.lstrip("*_ "))
        if m:
            ids[m.group(1) + m.group(2)] = ((m.group(1), int(m.group(2))), text)
        else:
            bare.add(text)
    return ids, bare

def changed_refs(old, new, story):
    """What changed between two fingerprints: the ids of decisions removed, struck or reworded (an added one changes no course), in id order,
    then the story when its text changed or a decision line without an id went away or was reworded."""
    (old_ids, old_bare), (new_ids, new_bare) = _decisions(old.get("decisions") or []), _decisions(new.get("decisions") or [])
    ids = sorted((i for i, (_, text) in old_ids.items() if new_ids.get(i, (None, None))[1] != text), key=lambda i: old_ids[i][0])
    return ids + ([story] if old.get("story") != new.get("story") or old_bare - new_bare else [])

def hold_changed(repo, proj_dir, story, refs, why):
    """Hold the tasks that rest on what changed; returns the event line."""
    ids = sorted(fc_change.rests_on(proj_dir, refs))
    if not ids:
        return f'event factory=changed story={story} why="{why}"  (nothing running rests on it)'
    fc_change.hold_tasks(proj_dir, ids, f"factory: {why}")
    return (f'event factory=held story={story} tasks={",".join(ids)} why="{why}"  '
            "(re-plan it with the product owner: keep, part, or discard each held task with compose change, step 8)")

def offer_day(repo, state, offer, now, out):
    """What a day turn does when nothing can be started: offer the product owner the next ticket to shape."""
    state["offer"] = {"id": offer["id"], "layer": offer["layer"], "at": now}
    fc_factorystate.say_once(state, "offer", f'event factory=offer ticket={offer["id"]} layer={offer["layer"]} why="{offer["why"]}"  '
                             "(offer to plan it with the product owner (sections/0f.md); they may pick another ticket or decline)", out)

ORDER_FILTER = {}  # mode -> fn(plan, entries, state) -> entries: a mode's say over which ordered stories may start
IDLE = {"day": offer_day}
PRD_IDLE = {}  # mode -> fn(repo, state, now, out): a mode's use of a turn that waits on the PRD (night prep prepares it)

def _settle(repo, plan, state, out):
    """Drop the records of stories that left the build (verified, finished, out of the plan) or lost their team lead.
    When herdr does not answer, no lead counts as lost this turn."""
    leads = False  # not asked yet
    for story, rec in list(state["dispatched"].items()):
        found = story_of(plan, story)
        s = (found or {}).get("state", "").lower()
        if found is None:
            out.append(f"event factory=dropped story={story}  (it left the product delivery plan)")
        elif s in FREED:
            if s == "in review":
                out.append(f"event factory=verified story={story} project={rec.get('project')}  (verified: waiting for the product owner's look and merge)")
        else:
            leads = leads_or_none(repo) if leads is False else leads
            if leads is None or rec.get("project") in leads:
                continue
            if s in OPEN:
                out.append(f"event factory=lead-lost story={story} project={rec.get('project')}  (its team lead is gone: the story goes back in the queue)")
            else:
                out.append(f"event factory=lead-lost story={story} project={rec.get('project')} state={found['state'].replace(' ', '-')}  "
                           "(its team lead is gone and the story is not in Todo: start its lead again or move the story back to Todo)")
        del state["dispatched"][story]
        state["prints"].pop(story, None)

def _watch(repo, plan, entries, state, out):
    """Hold the tasks of an in-flight story whose text, decisions or place in the plan changed since the last look."""
    waits = {e["id"]: e["waits_on"] for e in entries}
    descriptions = {}
    for story in in_flight(plan, state):
        feature = next((fid for fid, f in plan["features"].items() if any(s["id"] == story for s in f.get("stories") or [])), None)
        got = project_for(repo, feature) if feature else None
        if not got:
            continue
        if feature not in descriptions:
            descriptions[feature] = fc_linear.linear_issue(feature).get("description") or ""
        new = fingerprint(descriptions[feature], fc_linear.linear_issue(story).get("description") or "")
        new["waits_on"] = list(waits.get(story) or [])
        old = state["prints"].get(story)
        refs = changed_refs(old, new, story) if old else []
        if refs:
            ids = [r for r in refs if r != story]
            if ids and story in refs:
                why = f"{', '.join(ids)} changed on {feature}; {story} changed"
            else:
                why = f"{', '.join(refs)} changed on {feature if ids else story}"
            out.append(hold_changed(repo, got[2], story, refs, why))
        if old and new["waits_on"] and not old.get("waits_on"):
            out.append(hold_changed(repo, got[2], story, [story], f"the product delivery plan now puts {feature} after {', '.join(new['waits_on'])}"))
        state["prints"][story] = new  # only once the holds went through, so a failed hold is tried again

def _move_up(repo, plan, entries, picked, state, judge, out):
    """Ask once whether a ticket planned out of order, now specced, should move up in the plan, judging at most MOVE_UP_JUDGES stories a turn.
    Returns the (id, error) of judges that failed."""
    failed, budget = [], MOVE_UP_JUDGES
    for key in [k for k in state["said"] if k.startswith("move-up:") and k[8:] not in state["out_of_order"]]:
        del state["said"][key]
    for ident in list(state["out_of_order"]):
        under = [e for e in entries if e["id"] == ident or e["feature"] == ident]
        ids = [ident] + [s["id"] for s in (plan["features"].get(ident) or {}).get("stories") or []] + [e["id"] for e in under]
        ids = [i for n, i in enumerate(ids) if i not in ids[:n] and (story_of(plan, i) or any(e["id"] == i and e["kind"] == "story" for e in entries))]
        done = next((i for i in ids if i in picked), None)
        for i in ids:
            if done or not budget:
                break
            budget -= 1
            try:
                done = i if judge(i, write=False).get("specced") else None
            except fc_readiness.NotAStory:
                continue
            except (RuntimeError, OSError) as ex:
                failed.append((i, str(ex)))
                continue
        if not done:
            continue
        e = next((e for e in entries if e["id"] == done), None)
        state["out_of_order"].remove(ident)
        if e and not e["waits_on"]:
            continue
        waits = ", ".join(e["waits_on"]) if e else "not-in-plan"
        fc_factorystate.say_once(state, f"move-up:{ident}", f"event factory=move-up ticket={ident} waits_on={waits}  (planned out of order and now specced: ask the "
                                 "product owner whether to move it up; on yes, write the tracks again (sections/0p.md))", out)
    return failed

def _stopped(repo, stopping):
    """A stop asked for while the turn runs: SIGTERM (stopping), or `compose factory stop` on disk."""
    return bool(stopping and stopping()) or bool(fc_factorystate.load_state(repo)["stopped"])

def _dispatch_all(repo, plan, picks, state, now, stopping, saved, out):
    """Start each pick in turn. A story whose dispatch fails backs off BACKOFF_TURNS turns and the rest go on; herdr not answering ends
    the turn's dispatch. saved(story, record) runs after each start, so the record is on disk before the next. False when a stop cut it short."""
    said = state["said"]
    for e, kind in picks:
        if _stopped(repo, stopping):
            return False
        story = e["id"]
        try:
            out.append(dispatch(repo, plan, e, kind, state, now))
        except HerdrDown:
            fc_factorystate.say_once(state, "herdr", fc_factorystate.turn_failed(HERDR_DOWN), out)
            return True
        except RuntimeError as ex:
            state["backoff"][story] = {"why": str(ex), "until_turn": state["turns"] + BACKOFF_TURNS}
            fc_factorystate.say_once(state, "backoff:" + story, fc_factorystate.turn_failed(str(ex), story, f"it waits {BACKOFF_TURNS} turns; the rest go on"), out)
            continue
        said.pop("herdr", None); said.pop("backoff:" + story, None); state["backoff"].pop(story, None)
        if saved:
            saved(story, state["dispatched"][story])
    return True

def _expire(plan, state):
    """Forget the backoffs that ended, and every backoff of a story that left the plan (the line for a story that keeps failing the same way is said once)."""
    for story, b in list(state["backoff"].items()):
        if not isinstance(b, dict) or b.get("until_turn", 0) < state["turns"] or story_of(plan, story) is None:
            del state["backoff"][story]
    for key in [k for k in state["said"] if k.startswith("backoff:") and story_of(plan, k[8:]) is None]:
        del state["said"][key]

def turn(repo, state, now, stopping=None, saved=None):
    """One pass of the build loop: follow the roadmap, start ready stories, pause work whose ground changed, offer what to shape next.
    stopping() says a stop came while the turn runs; saved(story, record) persists each dispatch as it happens. Returns the event lines:
    whatever goes wrong ends the turn with a turn-failed line after the lines it already made, said once until a turn goes through."""
    out = []
    try:
        _turn(repo, state, now, stopping, saved, out)
    except Exception as ex:
        traceback.print_exc()
        fc_factorystate.say_once(state, "turn-crash", fc_factorystate.turn_failed(f"{type(ex).__name__}: {ex}"), out)
    else:
        state["said"].pop("turn-crash", None)
    return out

def _day(now):
    return time.strftime("%Y-%m-%d", time.localtime(now))

def _turn(repo, state, now, stopping, saved, out):
    said = state["said"]
    state["turns"] += 1
    prd = fc_prd.check(repo, state, now)  # never raises: a failed lookup leaves the cache and blocks nothing
    if prd:
        said.pop("prd-missing", None); said.pop("prd-day", None)
    try:
        plan = fc_delivery.product_plan(repo)
        if plan is None and prd is False:
            fc_factorystate.say_once(state, "prd-missing", PRD_FIRST, out)
            PRD_IDLE.get(fc_factorystate.pace(state)["mode"], lambda *a: None)(repo, state, now, out)
            return
        if plan is None:
            fc_factorystate.say_once(state, "bootstrap", "event factory=bootstrap  (no product delivery plan yet: plan it with the product owner from "
                                 "compose delivery seed (sections/0p.md); nothing is dispatched before it exists)", out)
            return
        said.pop("bootstrap", None)
        proposed = (fc_delivery.reconcile(repo) or {}).get("proposed") or []
        for key in [k for k in said if k.startswith("proposed:") and k[9:] not in proposed]:
            del said[key]
        for text in proposed:
            fc_factorystate.say_once(state, "proposed:" + text, f'event factory=proposed fix="{text}"  (bring it to the product owner with its reason; never apply it without their yes)', out)
        plan = fc_delivery.product_plan(repo)
        if plan is None:
            raise RuntimeError("the product delivery plan went away during the turn")
        entries = fc_plan.order_entries(plan["tracks"], plan["features"])
        entries = ORDER_FILTER.get(state["mode"], lambda plan, entries, state: entries)(plan, entries, state)
        _settle(repo, plan, state, out)
        _watch(repo, plan, entries, state, out)
    except (SystemExit, RuntimeError, OSError, ValueError, KeyError) as ex:
        fc_factorystate.say_once(state, "turn-failed", fc_factorystate.turn_failed(str(ex)), out)
        return
    said.pop("turn-failed", None)
    if "herdr" in said and leads_or_none(repo) is not None:  # herdr answers again: a later outage is said again
        said.pop("herdr")
    _expire(plan, state)
    free = 0 if fc_load.held() else max(0, fc_factorystate.pace(state)["cap"] - len(in_flight(plan, state)))
    judge = lambda s, **kw: fc_readiness.story_readiness(repo, s, **kw)
    got = pick(entries, plan, state, free, judge)
    got["failed"] += _move_up(repo, plan, entries, got["specced"], state, judge, out)
    live = {i for i, _ in got["failed"]}
    for key in [k for k in said if k.startswith("judge-failed:") and k[13:] not in live]:
        del said[key]
    for ident, err in got["failed"]:
        fc_factorystate.say_once(state, f"judge-failed:{ident}", f'event factory=judge-failed story={ident} why="{err}"', out)
    if not _dispatch_all(repo, plan, got["picks"], state, now, stopping, saved, out):
        return
    planning = state["planning"] if isinstance(state["planning"], dict) and now - (state["planning"].get("at") or 0) < PLANNING_HOURS * 3600 else None
    offers = not planning and now >= (state["declined_until"] or 0) and not _stopped(repo, stopping)
    if prd is False and offers and fc_factorystate.pace(state)["mode"] == "day" and said.get("prd-day") != _day(now):
        said["prd-day"] = _day(now)  # once a day, ahead of any ticket offer
        out.append(PRD_OFFER)
    if got["picks"] or got["ready_waiting"] or not got["offer"]:
        said.pop("offer", None)
        state["offer"] = None
    elif offers:
        IDLE[fc_factorystate.pace(state)["mode"]](repo, state, got["offer"], now, out)
