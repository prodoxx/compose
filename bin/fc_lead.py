"""A fresh team lead per story: the team lead is one long session, and over many stories its context fills and compacts until things
get lost. Its durable state lives in files (Linear, the project's tasks, results and decisions, and the handoff it keeps current,
fc_crew.handoff_path), so at a story boundary compose closes it and starts a new one under the same name, briefed to read them.
`compose crew restart` is that step by hand; the factory does it before handing the next story. It never happens mid-turn or
with work in flight."""
import argparse, json, shutil, sys, time
from pathlib import Path
import fc_core, fc_herdr, fc_tasks, fc_crew, fc_update

LEAD_READS = ("SKILL.md, sections/0h.md, sections/0a.md, sections/0e.md, sections/0f.md, sections/0g.md, sections/4.md, sections/5.md, "
              "sections/6.md, sections/6a.md, sections/7.md, sections/8.md and ~/.claude/skills/herdr/SKILL.md")
GONE_SECONDS = 30  # how long a closed team lead's name may stay in herdr's agent list before the start gives up waiting for it

def lead_name(proj):
    return fc_herdr.agent_name(Path(proj).name, "orchestrator")

def first_brief_path(proj, name):
    """Where the first team lead's brief is kept: the restart overwrites results/crew/<name>.brief.md, and what the product owner first asked is in it."""
    return fc_crew.scratch_dir(proj, name) / "brief.first.md"

def _feature(proj):
    try:
        got = json.loads((Path(proj) / "linear.json").read_text())
    except (OSError, ValueError):
        return None
    return got.get("feature") if isinstance(got, dict) else None

def restart_brief(proj, prompt=None):
    """The brief of a team lead that takes over from another: its state is in files, read in order, and the story prompt (if any) comes last."""
    proj = Path(proj)
    slug, wt, name = proj.name, fc_herdr.project_worktree(proj), lead_name(proj)
    handoff, first = fc_crew.handoff_path(proj, name), first_brief_path(proj, name)
    feature = _feature(proj)
    return (f"You are the team lead of project {slug} in {wt}{f', building feature {feature}' if feature else ''}, started fresh in place of the team lead before you: "
            "compose starts a new one at each story boundary so no one context carries every story. Your name is the same, so teammates and the engineering manager "
            "keep reporting to you and prompting you as before.\n"
            "What the one before you knew is in files, not in a conversation. Read, in this order:\n"
            f"1. Your handoff {handoff}: the open threads, holds, pending go's and live teammates it wrote down for you."
            f"{'' if handoff.exists() else ' It does not exist: rebuild all of that from 2.'}\n"
            f"2. The project's state: `compose status --repo {wt} --project {slug}`, `compose watch --repo {wt} --project {slug} --now`, "
            f"`compose crew list --repo {wt} --project {slug}`, the feature and its stories in Linear (their decisions and open questions), and results/. "
            "Where the handoff and this state disagree, the state wins.\n"
            f"3. The first brief of this project's team lead, {first}{'' if first.exists() else ' (not kept)'}: a story prompt in it is history, "
            "that story is done unless the state says otherwise.\n"
            f"Then read {LEAD_READS}, and pick up from 0r, then 0e. Run what you were handed, and nothing the handoff does not hand you.\n"
            f"Keep the handoff current ({handoff}) and rewrite it right before you report `done`.\n"
            + (prompt.rstrip() + "\n" if prompt else "No new job comes with this brief: continue what the handoff says, and wait for a prompt where it says nothing.\n"))

def gaps(proj, agents):
    """(whether the team lead is mid-turn, why it still cannot be replaced) now. Mid-turn is never overridden; the reasons are work
    in flight, an unreported question, or a handoff that would not carry a fresh lead, and `crew restart --force` overrides those."""
    proj, name = Path(proj), lead_name(proj)
    c = fc_crew.crew_record(proj, name)
    mine = next((ag for ag in agents if c and ag.get("name") == c.get("agent", c["name"]) and ag.get("workspace_id") == c.get("space")), {})
    reports = fc_crew.crew_reports(proj, name)
    last = reports[-1] if reports else {}
    why = []
    if last.get("state") != "done":
        why.append(f"its last report is {last.get('state', 'nothing')}, not done" + (f" ({last.get('message', '')[:80]})" if last.get("state") in ("needs-user", "blocked") else ""))
    if mine.get("agent_status") == "blocked":
        why.append("it waits at a prompt")
    wt = fc_herdr.project_worktree(proj)
    for w in fc_update.live_on(proj, wt, ("run", "resume", "verify")):
        why.append(f"a {w['kind']} is running (pid {w['pid']})")
    if not fc_update.live_on(proj, wt, ("run", "resume", "verify")) and (fc_tasks.run_alive(proj) or fc_tasks.fixing(proj)):
        why.append("a run or a verify fix round is going")
    live = []
    for f in sorted(fc_crew.crew_dir(proj).glob("*.json")):
        t = json.loads(f.read_text())
        if t["name"] == name or t.get("closed"):
            continue
        done = (fc_crew.crew_reports(proj, t["name"]) or [{}])[-1].get("state") == "done"
        if not done and not fc_crew.tab_closed(t, agents):
            live.append(t["name"])
    if live:
        why.append(f"teammates are live: {', '.join(live)}")
    if gap := fc_crew.handoff_gap(proj, name):
        why.append(f"{name} {gap}")
    return mine.get("agent_status") == "working", why

def verdict(proj):
    """What a story handed to this project's live team lead does with it, as (how, why): "fresh" (a boundary: replace the lead), "wait" (it is mid-turn:
    the boundary comes when the turn ends) or "keep" (hand the story to the same lead; why, when not empty, says what stands in the way). herdr
    not answering its agent list, or no lead in it, is a "keep" with no why: the caller's own live check decides then."""
    listed = fc_herdr.herdr("agent", "list")
    c = fc_crew.crew_record(proj, lead_name(proj))
    agents = listed.get("agents") or [] if isinstance(listed, dict) else None
    if agents is None or not c or fc_crew.tab_closed(c, agents):
        return "keep", ""
    midturn, why = gaps(proj, agents)
    if midturn:
        return "wait", f"{lead_name(proj)} is mid-turn"
    return ("keep", "; ".join(why)) if why else ("fresh", "")

def _gone(name, space):
    """Whether herdr's agent list no longer holds the closed team lead's name, waiting up to GONE_SECONDS for it to go."""
    end = time.time() + GONE_SECONDS
    while True:
        got = fc_herdr.herdr("agent", "list")
        if isinstance(got, dict) and not any(ag.get("name") == name and ag.get("workspace_id") == space for ag in got.get("agents") or []):
            return True
        if time.time() >= end:
            return False
        time.sleep(1)

def restart(a):
    """`crew restart --role orchestrator`: close the team lead and start a fresh one under its name, briefed to read the handoff. Waits for
    the end of its turn rather than cut it; refuses while work is in flight or the handoff is stale, unless --force."""
    proj = fc_core.project_dir(a)
    if (a.role or "").strip().lower() != "orchestrator":
        sys.exit(f"usage: compose crew restart --project {proj.name} --role orchestrator [--brief \"<job>\" | --brief -] [--force]  (only the team lead restarts: its state is in its handoff)")
    if not fc_herdr.herdr_env():
        sys.exit("team lead sessions live in herdr panes; there is no session to restart here")
    fc_herdr.herdr_manager_claim(proj, getattr(a, "takeover", False))
    name = lead_name(proj)
    c = fc_crew.crew_record(proj, name)
    listed = fc_herdr.herdr("agent", "list")
    if listed is None:
        sys.exit("herdr did not answer its agent list; nothing was restarted")
    agents = listed.get("agents") or []
    if not c or fc_crew.tab_closed(c, agents):
        sys.exit(f"no live team lead {name}; start one with: compose crew start --project {proj.name} --role orchestrator --title orchestrator --brief -")
    midturn, why = gaps(proj, agents)
    if midturn:
        sys.exit(f"{name} is mid-turn; restarting it now would cut off what it is doing, with --force too. Wait for its turn to end (`herdr agent read {name} --lines 40`), then run this again")
    if why and not a.force:
        sys.exit(f"{name} was not restarted: " + "; ".join(why) + f". Wait for the story boundary, or pass --force to restart it anyway "
                 f"(the fresh one starts from the handoff and the project's state)")
    brief = sys.stdin.read() if a.brief == "-" else a.brief
    text = restart_brief(proj, brief)
    first = first_brief_path(proj, name)
    brief_file = fc_crew.crew_dir(proj) / f"{name}.brief.md"
    if not first.exists() and brief_file.exists():
        first.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(brief_file, first)
    fc_herdr.herdr("tab", "close", c["tab"])
    fc_crew.crew_closed(proj, name, "crew restart" + (" --force" if why else ""))
    if not _gone(c.get("agent", name), c.get("space")):
        sys.exit(f"{name}'s tab is closed but herdr still lists it; run `compose crew start --project {proj.name} --role orchestrator --title orchestrator --brief -` once it is gone")
    fresh = argparse.Namespace(**{**vars(a), "action": "start", "role": "orchestrator", "title": c.get("title") or "orchestrator", "brief": text,
                                  "model": getattr(a, "model", None) or (c["model"] if c.get("model_from") == "--model" else None)})
    try:
        fc_crew.cmd_crew(fresh)
    except SystemExit as e:
        sys.exit(f"{name} was closed but its fresh replacement did not start: {e}\nStart it with: compose crew start --project {proj.name} --role orchestrator --title orchestrator --brief -")
    fc_crew.write_report(proj, name, "working", f"restarted fresh, from its handoff {fc_crew.handoff_path(proj, name)}")
    print(f"team lead {name} restarted fresh; it reads {fc_crew.handoff_path(proj, name)}")

def cmd_crew(a):
    """compose crew: `restart` here, every other action in fc_crew."""
    return restart(a) if a.action == "restart" else fc_crew.cmd_crew(a)
