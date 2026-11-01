import argparse, os
from pathlib import Path
import fc_core, fc_herdr, fc_manager, fc_crew, fc_compact, fc_compactor, fc_transcript, fc_compactlog

# The Stop hook's side of judged compaction (fc_watch.cmd_rewake calls it): which session this is, and whether its settled turn is a
# good moment to compact. Nothing here parks a watch or types into a pane; a qualifying verdict starts the detached compactor.


def session_role(ev):
    """Which compaction-timed session the Stop hook runs in: ("lead", handoff or None, repo) for a team lead (COMPOSE_ROLE orchestrator
    with a project; handoff None when its project cannot be resolved), ("manager", handoff, repo) for an engineering manager (no
    COMPOSE_ROLE, in a herdr pane, and the repository's records say that pane manages something), else None. A failing manager check is None."""
    role, project, repo = os.environ.get("COMPOSE_ROLE"), os.environ.get("COMPOSE_PROJECT"), os.environ.get("COMPOSE_REPO") or "."
    if role == "orchestrator" and project:
        try:
            proj = fc_core.project_dir(argparse.Namespace(repo=repo, project=project))
            return "lead", fc_crew.handoff_path(proj, fc_herdr.agent_name(proj.name, "orchestrator")), repo
        except (Exception, SystemExit):
            return "lead", None, repo  # no handoff to point a compactor at: the watch still parks
    pane = fc_core.session_id()
    if role or not pane:
        return None
    try:
        main = fc_core.main_checkout(Path(ev.get("cwd") or os.getcwd()))
        if fc_manager.is_manager(main, pane):
            return "manager", fc_manager.manager_handoff(main, pane), str(main)
    except Exception:
        pass  # a malformed crew record or an unreadable repo: not a session compose can name a manager
    return None

def judge_turn(role, handoff, repo, ev):
    """Log this settled turn and, unless a compaction of the pane is already under way, ask whether it is a good moment to compact; a verdict
    that qualifies (or the ceiling) starts the detached compactor. Does nothing without a pane, a handoff, or a transcript on disk."""
    pane = fc_core.session_id()
    path = ev.get("transcript_path")
    transcript = Path(path) if path else None
    if not pane or handoff is None or transcript is None or not transcript.is_file():
        return
    tokens = fc_transcript.context_tokens(fc_transcript.records(transcript))
    fc_compactlog.append("turn", pane=pane, role=role, tokens=tokens)
    marker = fc_compactlog.inflight(pane)
    if marker:
        fc_compactlog.append("verdict", pane=pane, role=role, tokens=tokens, decision="in-flight",
                             reason=f"a {marker.get('mode') or 'compaction'} compaction is at {marker.get('step') or 'work'}")
        return
    verdict = fc_compact.consider(pane, role, transcript)
    mode = {"qualifies": "judged", "ceiling": "ceiling"}.get(verdict.get("decision"))
    if mode:
        fc_compactor.start(pane, role, transcript, mode, handoff, repo)
