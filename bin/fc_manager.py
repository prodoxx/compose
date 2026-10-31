"""The engineering manager's seat: who holds each project and the factory, what a takeover carries and whom it tells, and the lines
`compose status` leads with so a session that is not the manager finds the one that is.
fc_herdr keeps the record of one project (results/manager.json); a session is one manager, so a takeover moves everything the
old session held in the repo, and tells it, instead of leaving two half managers."""
import os, sys
from pathlib import Path
import fc_core, fc_herdr, fc_factorystate

TOLD = ("compose: the engineering manager session in herdr pane {new} took over {what} at {at}. You no longer manage {them}. Stop every "
        "`compose watch` and `compose factory watch` you run for {them}, start no teammate or watch there again, and drop any question you "
        "still have open about {them}: ask the product owner nothing more about {them}, and mention the move to them at most once, as a "
        "statement. Its teammates now report to {new}. Anything else you manage stays yours. If the product owner wants {them} back they "
        "will say so; then run the same command with --takeover.")

def tell(pane, text):
    """Prompt the session in herdr pane `pane`; whether herdr took it (a pane at a question dialog refuses, and the session's watch says it too)."""
    return fc_herdr.herdr("agent", "prompt", pane, text) is not None

def held_by(repo, pane):
    """The project directories of this repository whose manager record names herdr pane `pane`."""
    return [proj for _, (_, proj) in sorted(fc_core.all_projects(Path(repo)).items()) if fc_herdr.herdr_manager_record(proj).get("session") == pane]

def take_factory(repo, old):
    """Move the factory's manager seat from herdr pane `old` to this session; whether `old` held it."""
    if fc_factorystate.load_state(repo)["manager_pane"] != old:
        return False
    with fc_factorystate.edit(repo) as state:
        state["manager_pane"] = fc_core.session_id()
    return True

def claim(proj, takeover=False, repo=None):
    """Claim the project for this session (fc_herdr.herdr_manager_claim) and label its pane `manager: <slug>`. A takeover from a live session
    moves every other project of the repository that session held and the factory's seat too, relabels both panes, and prompts the old
    session to stop what it ran; the words go to stderr, so a `watch` keeps printing event lines only."""
    proj = Path(proj)
    got = fc_herdr.herdr_manager_claim(proj, takeover)
    if got is None:
        return
    old = got["from"]
    if not old:
        fc_herdr.herdr_name_manager(proj)
        return
    repo = Path(repo) if repo else proj.parent.parent
    moved = [proj, *[p for p in held_by(repo, old) if p != proj]]
    for p in moved[1:]:
        fc_herdr.herdr_manager_claim(p, True)
    for p in moved:
        fc_herdr.herdr_unname_manager(p.name)  # off the old pane's label (and off ours, for the name below to put it back once)
        fc_herdr.herdr_name_manager(p)
    names = [p.name for p in moved]
    factory = take_factory(repo, old)
    what = ", ".join(names) + (" and the factory" if factory else "")
    told = tell(old, TOLD.format(new=fc_core.session_id(), what=what, at=fc_herdr.herdr_manager_record(proj).get("since", "?"), them="them" if len(names) > 1 or factory else "it"))
    print(f"took over {what} from the engineering manager session in herdr pane {old}; "
          + ("it was told to stop." if told else "it could not be prompted (its pane is busy); its watch and its next compose command tell it."), file=sys.stderr)

def factory_claim(repo, takeover=False):
    """The factory's manager seat (`factory.json` manager_pane) goes to the session that watches it, as one session per project does. Another
    session's live pane refuses unless `takeover`, which prompts that session to stop; a session outside herdr only records its pane."""
    mine = os.environ.get("HERDR_PANE_ID")
    state = fc_factorystate.load_state(repo)
    old = state["manager_pane"]
    if not mine or old == mine:
        return
    live = bool(old) and fc_herdr.herdr_env() and not os.environ.get("COMPOSE_ROLE") and fc_herdr.herdr_manager_live(old)
    if live and not takeover:
        sys.exit(f"the factory is watched by another engineering manager session (herdr pane {old}): two sessions both acting on its events would answer "
                 "the same offers twice. From here you may look (`compose factory status`). To watch it from this session instead, run the same command with --takeover.")
    with fc_factorystate.edit(repo) as disk:
        disk["manager_pane"] = mine
    if live:
        told = tell(old, TOLD.format(new=mine, what="the factory", at=fc_core.now(), them="it"))
        print(f"took over the factory from the engineering manager session in herdr pane {old}; " + ("it was told to stop." if told else "it could not be prompted."), file=sys.stderr)

def owner_lines(proj, watching=False):
    """The line a session gets about a project it does not hold. A parked `watch` (`watching`) of a session that lost the project gets
    `owner=taken` once and ends; `watch --now` says `owner=taken` to the session the record says lost it, else `owner=other` to a viewer
    while the holder's pane lives. [] for a crew teammate, the holder itself, and a project nobody holds."""
    mine, rec = fc_core.session_id(), fc_herdr.herdr_manager_record(proj)
    holder = rec.get("session")
    if os.environ.get("COMPOSE_ROLE") or not mine or not holder or holder == mine:
        return []
    if watching or (rec.get("from") or {}).get("session") == mine:
        return [f"event owner=taken project={Path(proj).name} pane={holder} since={rec.get('since')!r}  (another engineering manager session took this project over "
                f"from this one: stop every watch you run for it, start no teammate or watch there, drop any open question about it, and ask the product owner "
                f"nothing more about it; `compose status` shows where the manager is)"]
    if fc_herdr.herdr_manager_live(holder):
        return [f"event owner=other pane={holder} since={rec.get('since')!r}  (another engineering manager session drives this project: look "
                f"here, do not start teammates or a watch; --takeover on `crew start` or `watch` moves it to this session)"]
    return []

def where(pane, agents, labels):
    """Where herdr pane `pane` is, from the agent list (None when herdr could not say): its state, workspace, the session's title and the pane's label."""
    a = (agents or {}).get(pane)
    if agents is None:
        return f"herdr pane {pane}"
    if a is None:
        return f"herdr pane {pane} (gone: the next crew start or watch from any session replaces it)"
    title, label = a.get("terminal_title_stripped") or "", labels.get(pane) or ""
    return f"herdr pane {pane} ({a.get('agent_status', 'live')}, workspace {a.get('workspace_id')}{', ' + repr(title) if title else ''}{', labelled ' + repr(label) if label else ''})"

def panes():
    """({pane id: agent entry}, {pane id: label}) from herdr; the first is None when herdr could not say."""
    listed = fc_herdr.herdr("agent", "list")
    labels = {p.get("pane_id"): p.get("label") for p in (fc_herdr.herdr("pane", "list") or {}).get("panes", [])}
    return (None if listed is None else {a.get("pane_id"): a for a in listed.get("agents", [])}), labels

def seat_lines(repo, projects):
    """What `status` leads with: one line per manager session (where its pane is and what it runs), then, for a session that is not
    one of them, the line saying so and how to reach or replace the manager. Nothing outside herdr unless a record exists."""
    held, mine = {}, os.environ.get("HERDR_PANE_ID") if not os.environ.get("COMPOSE_ROLE") else None
    for proj in projects:
        held.setdefault(fc_herdr.herdr_manager_record(proj).get("session"), []).append(Path(proj).name)
    if pane := fc_factorystate.load_state(repo)["manager_pane"]:
        held.setdefault(pane, []).append("the factory")
    free = held.pop(None, [])
    if not held and not (free and fc_herdr.herdr_env()):
        return []
    agents, labels = panes()
    out = [f"engineering manager  {where(p, agents, labels)} runs {', '.join(names)}" + ("  <- this session" if p == mine else "") for p, names in held.items()]
    if free:
        out.append(f"engineering manager  none yet for {', '.join(free)}: the first crew start or continuous watch from a manager session claims it")
    if mine and mine not in held and held:
        out.append(f"this session is herdr pane {mine} and runs nothing here: ask the manager's pane above, or take over with --takeover on `crew start` or `watch` "
                   "(it moves everything that session runs, the factory too)")
    return out

def project_line(proj, agents):
    """The one status line under a project that says which session manages it; "" when none does and herdr is not in use."""
    pane = fc_herdr.herdr_manager_record(proj).get("session")
    if not pane:
        return "  manager  none yet" if fc_herdr.herdr_env() else ""
    state = " (this session)" if pane == os.environ.get("HERDR_PANE_ID") else "" if agents is None else " (live)" if pane in agents else " (gone)"
    return f"  manager  herdr pane {pane}{state}"
