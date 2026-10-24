import hashlib, json, os, re, sys, time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
import fc_core, fc_config, fc_models, fc_tasks, fc_worktree, fc_layers, fc_jev, fc_verify, fc_ledger


# A query or fragment key that forces a flag or experiment variant for whoever opens the URL, which no real user's page carries.
OVERRIDE_KEY = re.compile(r"variant|experiment|flag|override|force|bucket|treatment|cohort|(?:^|[_.-])(?:ab|ff|exp)(?:$|[_.-])", re.I)

def override_keys(url):
    parts = urlsplit(str(url or ""))
    return [k for k, _ in parse_qsl(parts.query) + parse_qsl(parts.fragment) if OVERRIDE_KEY.search(k)]

def flag_path_lint(cfg):
    """One message per page whose e2e cases all reach their variant through a URL override. The real flag path (the assignment
    a real user gets) is then never tested: every case of one feature forced the variant in the URL and a severe bug shipped.
    A page is covered by one case that carries no override on its URL; a case that forces the variant on purpose says why in
    `url_override`, and is left out."""
    pages = {}
    for c in cfg.get("e2e", []):
        if c.get("url"):
            pages.setdefault(urlsplit(str(c["url"])).path.rstrip("/") or "/", []).append(c)
    msgs = []
    for path, cases in pages.items():
        forcing = [c for c in cases if override_keys(c["url"])]
        for c in forcing:
            if "url_override" in c and not str(c["url_override"] or "").strip():
                msgs.append(f"case {c.get('name')!r}: url_override needs a reason")
        left = [c for c in forcing if not str(c.get("url_override") or "").strip()]
        if forcing and len(forcing) == len(cases) and left:
            keys = ", ".join(sorted({k for c in left for k in override_keys(c["url"])}))
            msgs.append(f"page {path}: {', '.join(repr(c.get('name')) for c in left)} reach the variant only through a URL override ({keys}); "
                        "add a case that gets the variant through the real flag or assignment (a seeded user, the flag service), "
                        'or give the forcing case "url_override": "<why>"')
    return msgs

ASSERT_FAILED_RE = re.compile(r"^ASSERT FAILED\b", re.M)

def judge_failure(cfg, case, r):
    """(verdict, kind, why) from a model that reads a failed case's evidence: verdict is "feature_missing" only when the output
    shows the app loaded and was in the right state, up to the point where the feature should appear. Anything else, also a model
    that cannot be asked, is "environment" (kind "unproven" or "unjudged"): the case is not yet proven to fail for the right reason."""
    run = r.get("run") or {}
    steps = "\n".join(f"{h['step']:>2} {h['kind']} {h['label']}" + ("" if h.get("page_changed") else " (page unchanged)") for h in run.get("history", [])[-8:])
    user = (f"GOAL: {case.get('goal')}\nSTART URL: {case.get('url')}\nENDED ON: {run.get('url')}\nJEV STATUS: {run.get('status')}\n"
            f"STOP REASON: {run.get('stop_reason')}\nLAST STEPS:\n{steps or '(none)'}\nASSERT EXIT: {r.get('assert_exit')}\n"
            f"OUTPUT:\n{((r.get('stdout') or '') + (r.get('stderr') or ''))[-1500:]}")
    system = ("A browser agent ran one end-to-end case against the project's base, before the feature exists. The case should fail only because "
              "the feature is missing. Decide from the evidence. feature_missing: the app loaded, the user was signed in and in the state the "
              "goal needs, nothing blocked the page, and what the goal needs (a new control, text, route, or behavior) is absent. environment: "
              "the page did not load or timed out, login failed or the user was sent to a login page, a cookie banner, consent dialog, tour, "
              "pop-up or other overlay blocked the page, seed data is missing, the path is wrong or a page that already exists answers 404, "
              "or the assert script broke. When the evidence does not show the app working up to the point the feature should appear, "
              'answer environment with kind "unproven". Reply {"verdict": "feature_missing" or "environment", "kind": one of '
              '"feature", "page did not load", "login", "overlay", "seed", "path", "assert", "unproven", "why": one short sentence}.')
    try:
        v = fc_models.fast_object({**fc_core.load_env(cfg.get("env_file", fc_config.ENV_FILE)), **os.environ}, system, user,
                                  '{"verdict": str, "kind": str, "why": str}', "verdict")
    except fc_models.PaymentRequired:
        raise
    except Exception as e:  # noqa: BLE001
        return "environment", "unjudged", f"no model verdict ({type(e).__name__}: {str(e)[:80]}); read the screenshots"
    why = str(v.get("why") or "")[:200]
    if v["verdict"] == "feature_missing":
        return "feature_missing", "feature", why
    return "environment", str(v.get("kind") or "unproven")[:30], why

def prove_case(cfg, case, r):
    """How one case fared against the project's base: it passes (so it proves nothing), fails because the feature is missing
    (what it must do), or fails because of the environment. The harness-and-page rules decide first; a model reads the rest."""
    row = {"name": case.get("name")}
    if r["exit"] == 0:
        return {**row, "verdict": "passes", "kind": "passes", "why": "it passes without the feature, so it proves nothing: tighten the goal or the assert"}
    found = fc_jev.env_failure(case, r)
    if found:
        verdict, kind, why = "environment", *found
    elif r.get("assert_exit") not in (None, 0) and ASSERT_FAILED_RE.search(r.get("stdout") or ""):  # the case's own check said no: a model's wording must not flip it
        verdict, kind, why = "feature_missing", "feature", "the case's assert reported the failure itself (ASSERT FAILED)"
    else:
        verdict, kind, why = judge_failure(cfg, case, r)
    return {**row, "verdict": verdict, "kind": kind, "why": why}

def prove(repo, proj, cfg):
    """Every e2e case, headless, against the project's base (the checkout as it is before `run`: trunk, the groundwork stubs and the
    tests, with each case's own setup and seed). Each must fail, and only because the feature is missing: a case that fails for the
    environment (a page that never loads, a login that fails, a cookie banner, a tour, seed data that is gone, a wrong path) would
    fail every verify for reasons no implementer can fix. Returns the rows, the flag-path lint, and whether all are proven."""
    lint = flag_path_lint(cfg)
    for m in lint:
        fc_core.say(f"prove  LINT  {m}")
    report = {"e2e": []}
    fc_verify.e2e_stage(repo, {**cfg, "e2e_headed": False}, {}, report, [], proj / "results", retry=False)
    rows = [prove_case(cfg, c, r) for c, r in zip(cfg.get("e2e", []), report["e2e"])]
    for row in rows:
        label = {"feature_missing": "ok   ", "passes": "PASS "}.get(row["verdict"], "ENV  ")
        what = {"ok   ": "fails, feature missing", "PASS ": "passes on the base"}.get(label, f"environment ({row['kind']})")
        fc_core.say(f"prove  {label} {row['name']}: {what} - {row['why']}")
    return rows, lint, len(rows) == len(cfg.get("e2e", [])) and all(x["verdict"] == "feature_missing" for x in rows) and not lint

def verify_config(a):
    """(repo, project dir, verify.json) of `compose verify --project`."""
    proj = fc_core.project_dir(a)
    path = proj / "verify.json"
    return Path(a.repo).resolve(), proj, json.loads(path.read_text()) if path.exists() else {}

PROVED_KEYS = ("e2e", "e2e_setup", "e2e_env")  # what a prove ran: the cases and the environment they start in

def e2e_digest(cfg):
    """A short hash of what a prove ran, so `run` can tell when the cases or their setup changed after it."""
    return hashlib.sha256(json.dumps({k: cfg.get(k) for k in PROVED_KEYS}, sort_keys=True).encode()).hexdigest()[:16]

def unproven(repo, proj, cfg):
    """Why `run` must not start yet, or None: the e2e cases were never proven, the last prove failed, or the cases or their setup
    changed since it. None without e2e cases, and once the stack holds layers (a later run of the same feature cannot be proven)."""
    if not cfg.get("e2e"):
        return None
    path = proj / "results" / "prove.json"
    last = json.loads(path.read_text()) if path.exists() else None
    why = ("its e2e cases were never proven" if last is None else "the last prove failed" if not last.get("passed")
           else "its e2e cases or their setup changed since the last prove" if last.get("e2e_digest") != e2e_digest(cfg) else None)
    return None if why is None or fc_worktree.stack_order(repo, proj) else why

def prove_root(repo, proj, cfg, only):
    """(where the cases run, their config there, the stack worktree to free or None). Before `run` that is the project checkout.
    Once the stack holds layers it is the stack cut below the first of the tasks `only` names (a re-plan's redo tasks): the layers
    under them are the app those cases run on, and their own work is what is missing. Without `only` a stack holds the whole feature."""
    layers = fc_worktree.stack_order(repo, proj)
    if not layers:
        return repo, cfg, None
    if not only:
        sys.exit("prove runs before `run`: the stack already holds the feature, so its cases would pass. After a re-plan, name the tasks to redo: "
                 "`compose verify --prove --only <ids>` proves the cases on the stack below the first of them.")
    order = fc_worktree.layer_order(fc_tasks.code_tasks(fc_tasks.load_tasks(proj, required=True)))
    ids = [t.strip() for t in only.split(",")]
    if [t for t in ids if t not in order]:
        sys.exit(f"prove --only: no task {', '.join(t for t in ids if t not in order)} in the plan")
    below = order[:min(order.index(t) for t in ids)]
    prefix = fc_worktree.task_prefix(repo, proj)
    through = [b for b in layers if b.removeprefix(prefix) in below]
    if not through:
        return repo, cfg, None  # nothing is built under the first task to redo: the cases run on the project checkout
    trunk = fc_core.current_branch(repo)
    wt = fc_worktree.stack_worktree(repo, proj, trunk)
    chain = fc_layers.linearize(repo, proj, trunk, wt, through)
    if fc_layers.STACK_STOP:
        sys.exit(f"prove FAIL  nothing proved or recorded: {fc_layers.STACK_STOP}")
    fc_core.sh(["git", "checkout", "-q", chain[-1]], wt)
    fc_worktree.ready_checkout(repo, wt, install=cfg.get("install"))
    fc_core.say(f"prove  at    {chain[-1]}, the stack below {ids[0]} ({len(chain)} layer(s) on {trunk})")
    olds = (repo, fc_core.worktrees(repo)[0])
    return wt, {**cfg, **{k: fc_verify.at_root(cfg[k], olds, wt) for k in ("checks", "e2e", "e2e_setup", "e2e_env") if k in cfg}, "main_repo": repo}, wt

def cmd_prove(a):
    repo, proj, cfg = verify_config(a)
    if not cfg.get("e2e"):
        fc_core.say("prove  nothing to prove: verify.json has no e2e cases")
        sys.exit(0)
    root, rooted, wt = prove_root(repo, proj, cfg, getattr(a, "only", None))
    t0 = time.time()
    fc_core.say(f"prove  start project={proj.name} cases={len(cfg['e2e'])}")
    try:
        rows, lint, ok = prove(root, proj, rooted)
    except fc_models.PaymentRequired as e:
        sys.exit(f"prove stopped, nothing recorded: {e}")
    finally:
        if wt is not None:
            fc_core.sh(["git", "checkout", "-q", "--detach", fc_core.current_branch(repo)], wt, check=False)  # free the top branch again
    (proj / "results").mkdir(exist_ok=True)
    fc_core.atomic_write(proj / "results" / "prove.json", json.dumps({"passed": ok, "cases": rows, "lint": lint, "e2e_digest": e2e_digest(cfg),
                                                                   "seconds": round(time.time() - t0, 1)}, indent=2))
    linear = proj / "linear.json"
    feature = json.loads(linear.read_text()).get("feature") if linear.exists() else None
    sha = fc_core.sh(["git", "rev-parse", "HEAD"], root, check=False).stdout.strip()
    events = [fc_ledger.event("", sha, [], [], "case", "prove",
                              *{"feature_missing": ("fail_expected", True), "passes": ("pass", False)}.get(row["verdict"], ("error", None)),
                              f"{row['name']}: {row['why']}") for row in rows]
    fc_ledger.record(feature, events)
    bad = [x for x in rows if x["verdict"] != "feature_missing"]
    fc_core.say(f"prove  {'PASS' if ok else 'FAIL'}  {len(rows) - len(bad)}/{len(rows)} case(s) fail because the feature is missing" + (f", {len(lint)} lint" if lint else "")
                + f"  wall {fc_core.fmt(time.time() - t0)}")
    if bad:
        print("\nFix these before any task is implemented: a case that fails for the environment fails every verify, and one that passes proves nothing.")
    fc_core.say(fc_core.LEDGER.line())
    sys.exit(0 if ok else 1)

def prototype_cases(cfg):
    """The `prototype` entries of verify.json as jev cases: each names a screen of the prototype, how to reach it in the built app,
    and what must match the reference."""
    cases = cfg.get("prototype") or []
    for c in cases:
        miss = [k for k in ("name", "url", "goal") if not c.get(k)] + ([] if c.get("must_match") and isinstance(c["must_match"], list) else ["must_match"])
        if miss:
            sys.exit(f"verify.json prototype entry {c.get('name') or c} lacks {', '.join(miss)}: each needs a name, a url, a goal that reaches the screen, and a must_match list")
    return cases

def prototype_report(cases, results):
    """The one prototype report: per screen, the reference, the capture verify took of the built app, and what must match.
    A reader opens the two images side by side and ticks each line."""
    out = ["# Prototype check", "", "Open each reference and its capture, and tick every line that matches.", "A line that does not match is a task before the first verify.", ""]
    for c, r in zip(cases, results):
        shots = sorted(Path(r["screenshots"]).glob("*.jpg")) if r.get("screenshots") else []
        got = f"`{shots[-1]}`" if r["exit"] == 0 and shots else "not captured: " + str((r.get("run") or {}).get("stop_reason") or "jev did not reach the screen")
        out += [f"## {c['name']}", "", f"- reference: {c.get('reference') or '(none given: the Frame0 or Figma page for this screen)'}", f"- capture: {got}", f"- page: {(r.get('run') or {}).get('url') or c['url']}", "",
                *[f"- [ ] {m}" for m in c["must_match"]], ""]
    return "\n".join(out)

def cmd_prototype(a):
    """Drive jev to each prototype screen of the built app (the stack top, as the full verify sees it), keep its screenshots, and
    write results/prototype.md. It fails only when a screen could not be captured: whether it matches the prototype is for the
    reader of the report."""
    repo, proj, cfg = verify_config(a)
    cases = prototype_cases(cfg)
    if not cases:
        sys.exit("verify.json has no `prototype` entries: the plan lists what must match the prototype, one entry per screen")
    t0 = time.time()
    root, _, wt = fc_verify.verify_root(repo, proj, cfg)
    try:
        if root != repo:
            cfg = {**cfg, **{k: fc_verify.at_root(cfg[k], (repo, fc_core.worktrees(repo)[0]), root) for k in ("e2e_setup", "e2e_env") if k in cfg}}
            cases = fc_verify.at_root(cases, (repo, fc_core.worktrees(repo)[0]), root)
        report = {"e2e": []}
        fc_verify.e2e_stage(root, {**cfg, "e2e": cases}, {}, report, [], proj / "results" / "prototype", keep=True)
    finally:
        if wt is not None:
            fc_core.sh(["git", "checkout", "-q", "--detach", fc_core.current_branch(repo)], wt, check=False)
    path = proj / "results" / "prototype.md"
    path.write_text(prototype_report(cases, report["e2e"]))
    got = sum(r["exit"] == 0 for r in report["e2e"])
    fc_core.say(f"prototype {'PASS' if got == len(cases) else 'FAIL'}  {got}/{len(cases)} screen(s) captured; compare each with its reference in {fc_core.tilde(path)}"
                f"  wall {fc_core.fmt(time.time() - t0)}")
    fc_core.say(fc_core.LEDGER.line())
    sys.exit(0 if got == len(cases) else 1)
