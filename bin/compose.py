#!/usr/bin/env python3
"""compose: a frontier model plans, parallel fast implementers build, a local
pipeline verifies. Stdlib only.

Layout inside any repo (all of .compose/, graft/, and .trail/ are git-ignored;
`init` ignores them before anything else, nothing here is committed):
  .compose/<project>/linear.json   {"feature": "<Linear issue>"}: the plan is
    its description, its tasks are issues under it
  .compose/<project>/verify.json    local verification config
  .compose/<project>/results/       tasks.json (the team lead's Linear sync),
    one JSON per task run, verify results
  .compose/<project>/worktrees/     one git clone per task
  .compose/<project>/design.f0      the project's Frame0 wireframes; frame0/ is
    its own Frame0 instance's data
Nothing is pushed. The only network calls are the model APIs.
"""
import argparse, os, signal, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fc_cost, fc_mcp  # noqa: E402
import fc_core, fc_proc, fc_readguard, fc_libapi, fc_gate, fc_config, fc_tasks, fc_linear, fc_linear_cmd, fc_claude, fc_models, fc_route, fc_quick, fc_herdr, fc_worktree, fc_layers, fc_blockers, fc_stack, fc_change, fc_frame0, fc_f0pages, fc_ask, fc_crew, fc_hooks, fc_toolbug, fc_init, fc_clean, fc_figma, fc_briefs, fc_implement, fc_shrink, fc_jev, fc_accept, fc_verify, fc_ship, fc_prove, fc_run, fc_status, fc_tiers, fc_tierpin, fc_budget, fc_spec, fc_watch, fc_yolo, fc_bench, fc_setup, fc_update  # noqa: E402

DETACHED = ("run", "verify", "resume")  # the long commands: a closed tab or a crashed session must not end them

def follow(cmd, log):
    """Run cmd detached, writing to log, and stream the log here until it exits; return its exit code. The work is double
    forked into its own session under launchd: a closed herdr tab makes Claude Code kill its background task's whole process
    tree, and that must end only this follower. Ctrl-C here stops the work too."""
    log, done = Path(log), Path(f"{log}.exit")
    log.write_bytes(b"")
    done.unlink(missing_ok=True)
    env = os.environ | {"COMPOSE_DETACHED": "1", "PYTHONUNBUFFERED": "1"}
    group = os.fork()
    if group == 0:
        try:
            os.setsid()
            if os.fork() == 0:
                out = os.open(log, os.O_WRONLY | os.O_APPEND)
                os.dup2(out, 1)
                os.dup2(out, 2)
                os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
                os.execve("/bin/sh", ["/bin/sh", "-c", '"$@"; echo $? > "$0"', str(done), *cmd], env)
        finally:
            os._exit(0)
    os.waitpid(group, 0)  # the middle process exits at once; its pid stays the work's process group
    def ended():
        try:
            os.killpg(group, 0)
            return done.exists() and done.read_text().strip() != ""
        except (ProcessLookupError, PermissionError):  # macOS answers EPERM for a group left with only its zombie leader
            return True
    with open(log, "rb") as f:
        while True:
            try:
                finished = ended()  # before the read: everything the work wrote is in the log once it has ended
                chunk = f.read()
                if chunk:
                    sys.stdout.write(chunk.decode(errors="replace"))
                    sys.stdout.flush()
                if finished:
                    code = done.read_text().strip() if done.exists() else ""
                    return int(code) if code else 1  # no code: it was killed before it could write one
                if not chunk:
                    time.sleep(0.2)
            except KeyboardInterrupt:
                os.killpg(group, signal.SIGINT)  # a person stopping it means the work, not just this view of it

def detach(a):
    proj = fc_core.project_dir(argparse.Namespace(repo=a.repo, project=a.project))
    fc_update.refuse_while_running(a.cmd, proj, a.repo)  # before the log below is emptied under the one still writing it
    log = proj / "results" / f"{a.cmd}.log"
    log.parent.mkdir(exist_ok=True)
    print(f"[{fc_core.now()}] {a.cmd} runs in its own session; it outlives this terminal. Log: {log}", flush=True)
    sys.exit(follow([sys.executable, *sys.argv], log))

def main():
    fc_proc.on_termination(fc_core.remove_scratch)  # a SIGTERM or SIGHUP still removes the scratch configs and ends what timed started
    if len(sys.argv) > 1 and sys.argv[1] == "timed":
        return fc_implement.cmd_timed(sys.argv[2:])
    if sys.argv[1:3] == ["frame0", "mcp"]:  # a session's MCP server: stdout is its protocol, so no config and no scrubbing
        return fc_frame0.frame0_mcp(sys.argv[3:])
    ap = argparse.ArgumentParser(prog="compose", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    for name, fn, help_ in (("doctor", fc_init.cmd_doctor, "check every tool and skill file compose depends on; one MISSING line each, exit 1 when a required one is absent"),
                            ("setup", fc_setup.cmd_setup, "install and configure everything compose needs, one question at a time on a terminal; --yes takes every default and never prompts"),
                            ("update", fc_update.cmd_update, "pull this checkout --ff-only (rebasing commits only it has), relink the links compose owns, run doctor; refused while a run or verify is live"),
                            ("init", fc_init.cmd_init, "create .compose/<project>/ skeleton"),
                            ("run", fc_run.cmd_run, "dispatch tasks to parallel implementers (pi by default)"),
                            ("verify", fc_verify.cmd_verify, "local no-mistakes-style pipeline: checks, confirmed fast-model review, jev e2e, auto-fix loop"),
                            ("bench", fc_bench.cmd_bench, "frozen task set x models: pass rate, attempts, p50/p95 time, cost per task"),
                            ("resume", fc_run.cmd_resume, "continue a run or verify that stopped on PAYMENT REQUIRED"),
                            ("done", fc_status.cmd_done, "record tasks the team lead finished by hand: --only ids --note why"),
                            ("sync", fc_linear.cmd_sync, "store the feature and its tasks, read from Linear with the workspace key; --stdin reads them as JSON instead"),
                            ("linear", fc_linear_cmd.cmd_linear, "read and write Linear with the workspace key: get | list | create | update | comment | labels | project | doc"),
                            ("tasks", fc_spec.cmd_tasks, "the task list to review before dispatch: ids, titles, files, after, tests; warns on shared files"),
                            ("route", fc_route.cmd_route, "route each task: a workflow and a depth, project policies first, Jev for the rest; --recalculate recomputes"),
                            ("quick", fc_quick.cmd_quick, "pin check-only on tasks, stories, or the feature, then route them"),
                            ("spec-check", fc_spec.cmd_spec_check, "check the synced feature, its stories, and its tasks against the spec; --stdin reads the sync JSON"),
                            ("ready", fc_linear.cmd_ready, "mark the stories and features that can start now with the Ready label in Linear, and print them"),
                            ("projects", fc_status.cmd_projects, "list every project (feature) with its task progress"),
                            ("cost", fc_status.cmd_cost, "tokens per teammate session and per run provider; --json"),
                            ("status", fc_status.cmd_status, "projects and every task's state, attempts, time, cost, stack layer; --watch redraws live"),
                            ("watch", fc_watch.cmd_watch, "block until a task finishes, an agent blocks, a run ends, or verify finishes; print one event line and exit"),
                            ("top", fc_status.cmd_top, "check out the stack top in the CLI worktree for manual testing; --detach to release it"),
                            ("stack", fc_ship.cmd_stack, "cut unmerged task branches into review layers (verify.json review_layer_lines, default 300) as one gh stack on the real base; --submit checks each layer on its own tree, then pushes and opens one draft PR per layer"),
                            ("reword", fc_ship.cmd_reword, "list the stack layers' commits with the ones that break the commit rules flagged; --set SHA=MESSAGE rewords one and rebuilds the layers (never pushes)"),
                            ("land", fc_ship.cmd_land, "merge the submitted stack bottom up with gh pr merge, rebasing and retargeting each next PR; prints the plan, acts only with --yes"),
                            ("clean", fc_clean.cmd_clean, "the post-merge step: remove task clones and merged branches, move the tasks proven merged to Done in Linear and roll their story and the feature up, and remove a merged project's worktree and branch"),
                            ("groundwork", fc_briefs.cmd_groundwork, "print the groundwork teammate's brief; --extra adds the project's notes"),
                            ("explore", fc_briefs.cmd_explore, "print the explore teammate's brief (a fast trail-only map of an existing repo); --extra adds notes"),
                            ("trail", fc_init.cmd_trail, "set trail up in the project's worktree again: wire it for Claude Code and build the graph"),
                            ("wayfinder", fc_briefs.cmd_wayfinder, "print the wayfinder teammate's brief (questions on one Lavish page); --extra adds the project's notes"),
                            ("planner", fc_briefs.cmd_planner, "print the planner teammate's brief; --issue is the ticket (default: the project's feature); --extra adds the project's notes"),
                            ("testcrews", fc_briefs.cmd_testcrews, "start one test-writing teammate per task in parallel from the groundwork contract"),
                            ("designer", fc_figma.cmd_designer, "print the designer teammate's brief; --figma stores the file key, --extra adds notes"),
                            ("designcrews", fc_figma.cmd_designcrews, "start one design-NNN builder per open design task"),
                            ("triage", fc_run.cmd_triage, "fast-model look at each passed task: clean or flag (run does this at its end)"),
                            ("reflect", fc_change.cmd_reflect, "screen the settled decisions against evidence with Jev: HOLDS | BROKEN | UNCLEAR per line"),
                            ("hold", fc_change.cmd_hold, "hold tasks back from dispatch while the plan changes: --only ids --reason why; --release ids|all"),
                            ("away", fc_yolo.cmd_yolo, "the product owner is away until their first message: [--push] [words] enters, `decided \"<q>: <a>\"` logs an answer taken, --clear-words empties the words, `digest` prints the digest, `off` prints it and archives"),
                            ("yolo", fc_yolo.cmd_yolo, "the engineering manager answers until an explicit off, and the product owner may steer: [--push] [words] enters, `decided \"<q>: <a>\"` logs an answer taken, --clear-words empties the words, `digest` prints the digest, `off` prints it and archives"),
                            ("change", fc_change.cmd_change, "start | done | undo a change of course: what changed in Linear and why, undoable"),
                            ("crew", fc_crew.cmd_crew, "start | list | focus | stop teammates, the Claude sessions in the project's herdr space (the team lead, the wayfinder, helpers)"),
                            ("report", fc_crew.cmd_report, "a teammate session reports up: working | needs-user | blocked | done, with one line"),
                            ("tool-bug", fc_toolbug.cmd_tool_bug, "file a tool bug (file) or open its fix's PR (pr --worktree): scrub --body and post it on the maintainer's machine or with --approved, else save a draft; --comment N or --new skips the search"),
                            ("toolsmith", fc_briefs.cmd_toolsmith, "print the toolsmith teammate's brief for the project's newest tool-bug entry"),
                            ("tier", fc_tierpin.cmd_tier, "pin a story's or the project's tier: compose tier --project <slug> [--story <id>] cheap|standard|frontier|no-frontier|clear"),
                            ("budget", fc_budget.cmd_budget, "set a project's frontier spend limit: compose budget --project <slug> frontier <dollars>"),
                            ("models", fc_models.cmd_models, "--probe: clear the day's cached model probes and probe the crew models again"),
                            ("frame0", fc_f0pages.cmd_frame0_pages, "up | draw | save | down the project's own Frame0 (wireframes in .compose/<project>/design.f0); draw runs a JSON list of frame0 tool calls in one go; pages | move | delete | links | lint list, reorder, remove, and check its pages"),
                            ("figma", fc_figma.cmd_figma, "check | tree | judge | publish: store the target Figma file key, prove Framelink reads it, record the plan's limits; save a task's node tree; judge it with Jev; say whether a library publish is pending"),
                            ("teardown", fc_crew.cmd_teardown, "close the project's implementer, verify, and finished teammate tabs (the team lead's done does this)"),
                            ("guard", fc_hooks.cmd_guard, "Claude Code PreToolUse hook: the team lead writes no code (reads the hook event on stdin)"),
                            ("rewake", fc_watch.cmd_rewake, "Claude Code Stop hook: park a watch after each team lead turn; its event wakes the session"),
                            ("reenter", fc_watch.cmd_reenter, "Claude Code SessionStart hook: after a compaction, point the teammate back at its brief, the board, and Linear"),
                            ("skillcheck", fc_hooks.cmd_skillcheck, "Claude Code UserPromptSubmit and PostToolUse(Skill) hook: tell a compose session the skill changed since it read it"),
                            ("shrink", fc_shrink.cmd_shrink, "make a small JPEG copy of each image so an LLM reads it for fewer tokens; prints its pixels and bytes before and after"),
                            ("workspace", fc_config.cmd_workspace, f"show workspaces, or switch: `workspace personal` (config: {fc_config.CONFIG_PATH})")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--repo", default=".")
        s.add_argument("--project", required=(name == "init"))
        s.set_defaults(fn=fn)
        if name == "init":
            s.add_argument("--here", action="store_true", help="create the project in this checkout instead of its own worktree")
            s.add_argument("--branch", help="name the project's branch (default compose/<project>); on an existing project, renames its branch to this")
            s.add_argument("--after", metavar="PROJECT", help="branch from another project's branch (compose/<PROJECT> unless it chose one): a feature that needs that feature's code")
        if name == "setup":
            s.add_argument("--yes", action="store_true", help="take every default, skip every key, never prompt")
            s.add_argument("--dry-run", action="store_true", help="walk the whole setup and change nothing: every change prints as a would line")
        if name == "sync":
            s.add_argument("--stdin", action="store_true", help="read the sync JSON from stdin instead of Linear")
        if name == "linear":
            s.add_argument("action", choices=("get", "list", "create", "update", "comment", "labels", "project", "doc"))
            s.add_argument("target", nargs="*")
            for flag in ("--body", "--comments", "--json", "--append", "--description", "--any-project", "--fresh"):
                s.add_argument(flag, action="store_true")
            s.add_argument("--label", action="append")
            for flag in ("--parent", "--search", "--state", "--priority", "--title", "--blocked-by", "--file", "--section", "--expect", "--create"):
                s.add_argument(flag)
        if name == "cost":
            s.add_argument("--json", action="store_true", help="print the rows and their total as JSON")
        if name == "tier":
            s.add_argument("value", choices=(*fc_tiers.PINS, "clear"))
            s.add_argument("--story")
        if name == "budget":
            s.add_argument("tier", choices=("frontier",))
            s.add_argument("dollars", type=float)
        if name == "route":
            s.add_argument("--only", help="comma-separated task ids")
            s.add_argument("--recalculate", action="store_true", help="recompute every route, even one whose context is unchanged")
        if name == "shrink":
            s.add_argument("images", nargs="+", help="image files to shrink")
            s.add_argument("--out", default=None, help="directory for the copies (default: <system temp>/compose-shrink)")
            s.add_argument("--max-edge", type=int, default=1000, help="longest edge in pixels")
            s.add_argument("--quality", type=int, default=72, help="JPEG quality, 1-100")
        if name == "quick":
            s.add_argument("ids", nargs="+", help="task ids, task or story Linear ids, or the feature id")
        if name == "spec-check":
            s.add_argument("--stdin", action="store_true", help="read the sync JSON from stdin instead of the project's cache")
        if name == "run":
            s.add_argument("--only", help="comma-separated task ids")
            s.add_argument("--attempts", type=int, default=3)
            s.add_argument("--parallel", type=int, help="tasks at once (default: parallel in ~/.compose/config.json, else 3)")
            s.add_argument("--trunk", default=None, help="trunk branch the stack targets (default: the branch you are on)")
            s.add_argument("--no-stack", action="store_true", help="do not rebase the existing layers before dispatch or register passed branches with gh stack")
            s.add_argument("--redo", action="store_true", help="rebuild tasks whose passed branch still exists (otherwise they are kept)")
            s.add_argument("--unproven", action="store_true", help="run although `verify --prove` has not passed on the current e2e cases")
        if name == "resume":
            s.add_argument("--parallel", type=int, help="tasks at once (default: parallel in ~/.compose/config.json, else the halted run's)")
        if name == "done":
            s.add_argument("--only", required=True, help="comma-separated task ids")
            s.add_argument("--note", required=True, help="why it was done by hand, e.g. 'provider out of credit'")
        if name == "top":
            s.add_argument("--detach", action="store_true")
        if name == "watch":
            s.add_argument("--now", action="store_true", help="print the current state as events and exit (session-start reconcile)")
            s.add_argument("--timeout", type=float, default=0, help="seconds to wait before exiting 2 with a timeout event and a state summary (default 0 = forever: a timeout is a wake-up with nothing to do, and a stall event already reports silence)")
            s.add_argument("--stall", type=float, default=None, help=f"seconds of silence with work pending before a stall event (default verify.json stall_seconds, else {fc_watch.STALL_SECONDS})")
            s.add_argument("--interval", type=float, default=fc_watch.WATCH_INTERVAL)
            s.add_argument("--settle", type=float, default=fc_watch.WATCH_SETTLE, help="seconds to gather the events of one burst into one wake (a question, a block, or a stall wakes at once; 0 = off)")
            s.add_argument("--takeover", action="store_true", help="watch from this session although another live engineering manager session drives the project (its teammates then report here)")
            s.add_argument("--from", dest="from_", help="only these teammates wake you (roles or agent names, comma-separated); the engineering manager passes orchestrator, its team lead's role")
        if name == "status":
            s.add_argument("--watch", action="store_true", help="redraw every 2 seconds")
            s.add_argument("--digest", action="store_true", help="one line per project: tasks done, run, last verify and why it failed, questions waiting")
        if name == "stack":
            s.add_argument("--trunk", default=None, help="default: the branch you are on")
            s.add_argument("--submit", action="store_true", help="also `gh stack submit --auto`: pushes the layers and opens a draft PR per layer as a GitHub stack")
            s.add_argument("--update", action="store_true", help="with --submit: push submitted layers that changed, with --force-with-lease on the PR head")
        if name in ("reword", "land"):
            s.add_argument("--trunk", default=None, help="default: the branch you are on")
        if name == "reword":
            s.add_argument("--set", action="append", metavar="SHA=MESSAGE", help="the message a layer commit gets from now on; repeat for more")
            s.add_argument("--as-is", action="store_true", help="take the message even when it breaks the commit rules")
        if name == "land":
            s.add_argument("--yes", action="store_true", help="merge and push for real: only once the user has said to land this stack")
            s.add_argument("--method", choices=("squash", "merge", "rebase"), help="how each PR merges (default: land_method in verify.json, else squash)")
            s.add_argument("--timeout", type=float, default=30, help="minutes to wait on each PR's required checks (default 30)")
        if name == "bench":
            s.add_argument("--models", default="pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast,pi:openrouter-fast/openai/gpt-oss-120b@preset/gpt-oss-120b-cerebras,claude",
                           help="comma-separated model ids: OpenCode ids, 'pi:<provider/model>' for the pi harness, 'claude' = Claude Code one-shot")
            s.add_argument("--only", help="comma-separated task ids")
            s.add_argument("--runs", type=int, default=1, help="repeats per task per model")
            s.add_argument("--attempts", type=int, default=3)
            s.add_argument("--parallel", type=int, default=4)
        if name in ("groundwork", "wayfinder", "explore", "planner", "designer"):
            s.add_argument("--extra", help="project-specific notes appended to the brief; `-` reads them from stdin")
        if name == "planner":
            s.add_argument("--issue", help="the ticket to plan (default: the project's feature)")
        if name == "designer":
            s.add_argument("--figma", help="the target Figma file's url or key: stored in linear.json before the brief prints")
        if name == "designcrews":
            s.add_argument("--only", help="comma-separated task ids")
            s.add_argument("--print", dest="print_", action="store_true", help="print the briefs instead of starting builders")
            s.add_argument("--max", type=int, help="most builders to start at once (default: design_concurrency in verify.json, else parallel in ~/.compose/config.json, else 3)")
        if name == "wayfinder":
            s.add_argument("--change", help="an open change of course, e.g. C2: its own page, asking only what the evidence questions")
        if name == "testcrews":
            s.add_argument("--contract", help="default: .compose/<project>/results/contract.md")
            s.add_argument("--only", help="comma-separated task ids")
            s.add_argument("--print", dest="print_", action="store_true", help="print the briefs instead of starting teammates")
            s.add_argument("--budget", type=int, help="test lines per task (default 60; a task's test_lines: wins)")
            s.add_argument("--collect", action="store_true", help="cherry-pick every test-writing teammate's commits onto the project branch in task order")
            s.add_argument("--redo", action="store_true", help="rewrite the tests of passed tasks too, for a re-plan that changed their briefs (as run --redo)")
            s.add_argument("--parallel", type=int, help="writers to start at once, Claude Code ones too (default: parallel in ~/.compose/config.json, and claude_parallel for Claude writers)")
        if name == "triage":
            s.add_argument("--only", help="comma-separated task ids (default: every passed task)")
        if name == "reflect":
            s.add_argument("--evidence", help="what was found: the product owner's words, a report, a rejection reason; `-` reads stdin")
            s.add_argument("--dismiss", help="why the evidence so far needs no change of course (a false alarm, a task-sized fix): later screens skip it")
            s.add_argument("--against", action="append", help="a Linear line the evidence contradicts, quoted; repeatable")
        if name == "hold":
            s.add_argument("--only", help="comma-separated task ids to hold")
            s.add_argument("--reason", help="one line: why they wait")
            s.add_argument("--release", help="comma-separated task ids, or all")
        if name in ("away", "yolo"):
            s.add_argument("--push", action="store_true", help="permission, given in advance, to submit the project's verified stack")
            s.add_argument("--clear-words", action="store_true", help="empty the words, keeping since, push, and the decisions")
            s.add_argument("words", nargs="*", help="what pre-answers questions, verbatim; or `decided <q>: <a>`; or `digest`; or `off`")
        if name == "change":
            s.add_argument("action", choices=("start", "done", "undo"))
            s.add_argument("id", nargs="?", help="undo: the change to undo, e.g. C2")
            s.add_argument("--why", help="start: one line, the reason for the change")
        if name == "crew":
            s.add_argument("action", choices=("start", "list", "focus", "stop"))
            s.add_argument("--role", help="orchestrator (the team lead), wayfinder, or a helper's job; the agent is named <slug>-<role>")
            s.add_argument("--title", help="tab label, e.g. the ticket's title")
            s.add_argument("--brief", help="what to do; `-` reads it from stdin")
            s.add_argument("--force", action="store_true", help="stop: close it even though it has not reported done")
            s.add_argument("--cwd", help="start: run the session in its own new worktree of this repo (a plain folder: in it) instead of the project worktree")
            s.add_argument("--work-repo", help="start: work on its own new worktree of this repo from a session in the project worktree, so a repo Claude Code never trusted (the CLI's own checkout) never shows the trust prompt")
            s.add_argument("--frame0", action="store_true", help="start: give the session the project's Frame0 tools")
            s.add_argument("--model", help="start: claude:<model-id>, or a name under models.crew (e.g. small); default models.crew[<role>], then models.crew.default")
            s.add_argument("--takeover", action="store_true", help="start, stop: drive the project from this session although another live engineering manager session does (its teammates then report here)")
        if name == "models":
            s.add_argument("--probe", action="store_true")
        if name == "frame0":
            s.add_argument("action", choices=("up", "draw", "save", "down", "pages", "move", "delete", "links", "lint"))
            s.add_argument("--file", help="draw: the JSON list of frame0 tool calls (default: stdin)")
            s.add_argument("--page", action="append", help="move | delete: a page by id, name, or the start of its name (repeatable; move keeps the order given)")
            s.add_argument("--after", help="move: put the pages right behind this page")
            s.add_argument("--first", action="store_true", help="move: put the pages first")
            s.add_argument("--empty", action="store_true", help="delete: every page without a shape; pages: only those")
            s.add_argument("--retarget", action="append", help="links: point every link to <old page or id> at <new page>, as old=new (repeatable)")
            s.add_argument("--drop-dangling", action="store_true", help="links: clear the links to pages that are gone")
            s.add_argument("--dry-run", action="store_true", help="move | delete | links: show the changes without making them")
        if name == "figma":
            s.add_argument("action", choices=("check", "tree", "judge", "publish"))
            s.add_argument("--figma", help="check: the target Figma file's url or key, stored in linear.json")
            s.add_argument("--plan", help="check: the plan tier whoami shows (starter, pro, org, enterprise): stores the limits compose's design flow depends on")
            s.add_argument("--pages", help="check: pages per file the probe measured (a count or unlimited), replacing the tier's predicted limit")
            s.add_argument("--modes", help="check: modes per collection the probe measured (a count or unlimited), replacing the tier's predicted limit")
            s.add_argument("--need-pages", type=int, help="check: pages per file the design needs; BLOCKED when the plan allows fewer")
            s.add_argument("--library", help="publish: the library file's url or key (stored after the first use)")
            s.add_argument("--keys", help="publish: comma-separated component keys the consumer file needs")
            s.add_argument("--only", help="judge: comma-separated design task ids; tree: one id")
            s.add_argument("--node", help="tree: comma-separated node ids to fetch through Framelink")
            s.add_argument("--depth", type=int, help="tree: how deep Framelink walks (default: all)")
            s.add_argument("--bindings", help="tree: a file (or - for stdin) with what the write script returned, appended to the tree")
        if name == "report":
            s.add_argument("--as", dest="as_", required=True, help="your agent name, <slug>-<role>")
            s.add_argument("state", help="working | needs-user | blocked | done | tool-bug | contradiction")
            s.add_argument("message", nargs="+")
        if name == "tool-bug":
            s.add_argument("action", choices=("file", "pr"), help="file: post the report as an issue; pr: push the fix branch and open its pull request")
            ex = s.add_mutually_exclusive_group()
            ex.add_argument("--comment", type=int, help="comment on issue N instead of searching the open issues")
            ex.add_argument("--new", action="store_true", help="open a new issue instead of searching the open issues")
            s.add_argument("--approved", action="store_true", help="post although this machine is not the maintainer's")
            s.add_argument("--body", help="file: the file holding the raw report")
            s.add_argument("--worktree", help="pr: your own worktree of the compose repo, on the fix's branch")
            s.add_argument("--title", help="pr: the PR title, default the branch's first commit subject")
            s.add_argument("--entry", help="the `at` of the tool-bugs.jsonl entry the posted URL is recorded on")
        if name == "workspace":
            s.add_argument("name", nargs="?", help="workspace to make active, or add | key | remove")
            s.add_argument("target", nargs="?", help="the workspace add, key, or remove acts on")
            g = s.add_mutually_exclusive_group()
            g.add_argument("--linear", action="store_true", help="key: store the workspace's Linear API key (stdin) instead of OpenRouter's")
            g.add_argument("--typesafe", action="store_true", help="key: store the TypeSafe key (stdin); one key serves every workspace")
            g.add_argument("--provider", metavar="NAME", help="key: store the key (stdin) of a provider declared in the config")
        if name == "update":
            s.add_argument("--force", action="store_true", help="update although a run or verify is live on this machine")
        if name == "clean":
            s.add_argument("--force", action="store_true", help="with --project: clean it although teammates are still working")
            s.add_argument("--abandon", action="store_true", help="with --project: remove an unmerged project (worktree, branches, herdr space) after listing what goes and what stays; Linear and pushed branches are untouched")
            s.add_argument("--dry-run", action="store_true", help="with --abandon: list what it would remove and what it keeps, remove nothing")
            s.add_argument("--delete-unmerged", action="store_true", help="also delete task branches not merged into HEAD (layers a reviewer has not merged yet)")
        if name == "verify":
            s.add_argument("--skip-e2e", action="store_true")
            s.add_argument("--skip-review", action="store_true")
            s.add_argument("--prove", action="store_true", help="before `run`: every e2e case against the project's base must fail because the feature is missing, never because of the environment")
            s.add_argument("--review-only", action="store_true", help="right after `run`: only the diff review and the acceptance questions, no checks, e2e, or fixes")
            s.add_argument("--prototype", action="store_true", help="after `run`: capture each verify.json `prototype` screen of the built app into results/prototype.md to compare with its reference")
            s.add_argument("--fix", type=int, default=2, help="auto-fix rounds with the fast model before escalating (0 = report only)")
    if len(sys.argv) == 1:
        fc_herdr.herdr_attach()
        ap.print_help(sys.stderr)
        sys.exit(2)
    a, extra = ap.parse_known_args()
    if a.cmd == "verify" and (a.prove or a.prototype):  # their own flows, with their own reports
        a.fn = fc_prove.cmd_prove if a.prove else fc_prove.cmd_prototype
    if extra and a.cmd in ("away", "yolo"):
        a.words += extra  # `yolo decided --project x "<q>: <a>"`: words after the flags are still words
    elif extra:
        sub.choices[a.cmd].error(f"unrecognized arguments: {' '.join(extra)}")
    if a.cmd == "tool-bug" and not (a.body if a.action == "file" else a.worktree):
        sub.choices[a.cmd].error(f"tool-bug {a.action} needs " + ("--body" if a.action == "file" else "--worktree"))
    if a.cmd in DETACHED:
        fc_proc.restore_sigint()  # the long commands are stopped with a SIGINT, even when whoever started them ignored it
    detached = os.environ.pop("COMPOSE_DETACHED", None)  # the marker is for this process alone: a compose run by anything it starts must detach on its own
    if a.cmd in DETACHED and not detached and not fc_run.joins_live_run(a):  # it only queues its tasks on the live run: nothing to outlive the terminal
        return detach(a)
    if a.cmd in DETACHED:  # the work itself: from here `compose update` on this machine waits for it
        fc_update.hold(a.cmd, fc_core.project_dir(argparse.Namespace(repo=a.repo, project=a.project)).name, a.repo)
    if a.fn not in (fc_config.cmd_workspace, fc_hooks.cmd_guard, fc_watch.cmd_rewake, fc_hooks.cmd_skillcheck):  # the hooks run in every session: no config, no output
        ws = fc_config.apply_config(move=not getattr(a, "dry_run", False))  # "default" when no workspace is chosen
        sys.stdout, sys.stderr = fc_core.Scrubbed(sys.stdout), fc_core.Scrubbed(sys.stderr)
        if a.cmd in ("run", "verify", "bench", "resume"):  # the commands that spend; `watch` output stays one line
            fc_core.say(f"workspace {ws}")
    try:
        a.fn(a)
    except KeyboardInterrupt:
        fc_proc.stop()  # what the command started in groups of their own goes with it
        print(f"\n[{fc_core.now()}] {a.cmd} stopped by Ctrl-C" + ("; `compose resume` continues it" if a.cmd in ("run", "resume") else ""), file=sys.stderr)
        sys.exit(130)

if __name__ == "__main__":
    main()
