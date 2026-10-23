import json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import fc_core, fc_tasks, fc_implement, fc_briefs, fc_worktree


# ---------- bench ----------

def pct(xs, q):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]

CASE_RE = re.compile(r"^\+\s*(?:async\s+)?(?:def test_|func Test|(?:it|test)\()")

def score_test(wt, meta, base):
    paths = meta["protected"]
    green = fc_implement.run_test(meta, wt).returncode == 0
    for f in meta["files"]:
        if fc_core.sh(["git", "cat-file", "-e", f"{base}:{f}"], wt, check=False).returncode:
            (wt / f).unlink(missing_ok=True)
        else:
            fc_core.sh(["git", "checkout", base, "--", f], wt)
    red = fc_implement.run_test(meta, wt).returncode != 0
    fc_core.sh(["git", "add", "-A", "--"] + paths, wt)
    lines = sum(int(x) for l in fc_core.sh(["git", "diff", "--numstat", base, "--"] + paths, wt).stdout.splitlines() for x in l.split("\t")[:2] if x != "-")
    diff = fc_core.sh(["git", "diff", base, "--"] + paths, wt).stdout.splitlines()
    cases = sum(1 for l in diff if CASE_RE.match(l))
    budget = int(meta.get("test_lines") or fc_briefs.TEST_BUDGET)
    return {"lines": lines, "budget": budget, "over_budget": lines > budget, "cases": cases,
            "holdouts": max(0, cases - len(fc_tasks.parse_criteria(meta["body"]))), "green": green, "red": red}

def bench_tests(a):
    if not a.base:
        sys.exit("bench --tests needs --base <rev>: the revision the protected tests are reset to")
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    only = set(a.only.split(",")) if a.only else None
    tasks = [m for m in fc_tasks.load_tasks(proj, required=True) if (not only or m["id"] in only) and m.get("protected")]

    def one(job):
        meta, run, model = job
        wt = proj / "worktrees" / f"{meta['id']}-tests-{re.sub(r'[^a-z0-9]+', '-', model.lower())}-r{run}"
        try:
            fc_core.sh(["git", "clone", "-q", str(repo), str(wt)], repo)
            fc_worktree.ready_checkout(repo, wt)
            for f in meta["protected"]:
                if fc_core.sh(["git", "cat-file", "-e", f"{a.base}:{f}"], wt, check=False).returncode:
                    (wt / f).unlink(missing_ok=True)
                else:
                    fc_core.sh(["git", "checkout", a.base, "--", f], wt)
            prompt = fc_briefs.test_brief(meta, "(none)") + "\nThis is a bench run: there is no contract and no team lead; write and commit the test, report nothing, then stop."
            usage = fc_implement.implementer(wt, model, prompt)[3] or {}
            return {"task": meta["id"], "run": run, "model": model, **score_test(wt, meta, a.base),
                    "cost": usage.get("cost", 0) + usage.get("reference_cost", 0)}
        except Exception as e:
            return {"task": meta["id"], "run": run, "model": model, "error": repr(e)}
        finally:
            shutil.rmtree(wt, ignore_errors=True)

    rows, raw = [], []
    for model in a.models.split(","):
        with ThreadPoolExecutor(max_workers=a.parallel) as ex:
            results = list(ex.map(one, [(t, r, model) for t in tasks for r in range(a.runs)]))
        raw.extend(results)
        ok = [r for r in results if "error" not in r]
        n = len(ok) or 1
        rows.append({"model": model, "runs": len(results), "errors": len(results) - len(ok),
                     "red_rate": sum(r["red"] for r in ok) / n, "green_rate": sum(r["green"] for r in ok) / n,
                     "both_rate": sum(r["red"] and r["green"] for r in ok) / n, "lines_mean": sum(r["lines"] for r in ok) / n,
                     "over_budget": sum(r["over_budget"] for r in ok), "holdouts_mean": sum(r["holdouts"] for r in ok) / n,
                     "cost_per_test": sum(r["cost"] for r in ok) / n})
    for r in rows:
        print(r)
    out = proj / "results" / f"bench-tests-{time.strftime('%Y%m%d-%H%M%S')}.json"
    fc_core.atomic_write(out, json.dumps({"summary": rows, "runs": raw}, indent=2))
    print(f"saved {out}")

def cmd_bench(a):
    """Frozen task set x models. Each task is reset to red in its worktree, then implemented from scratch by each model."""
    if a.tests:
        return bench_tests(a)
    proj = fc_core.project_dir(a); repo = Path(a.repo).resolve()
    only = set(a.only.split(",")) if a.only else None
    if fc_core.sh(["git", "status", "--porcelain"], repo).stdout.strip():
        sys.exit("repo has uncommitted changes; commit first")
    tasks = [m for m in fc_tasks.load_tasks(proj, required=True) if not only or m["id"] in only]
    if not tasks:
        sys.exit("no tasks on Linear under this feature carry a compose block")
    models = a.models.split(",")
    t0 = time.time()
    fc_core.say(f"bench start project={proj.name} tasks={len(tasks)} models={models} runs={a.runs} attempts={a.attempts}")
    rows, raw = [], []
    for model in models:
        before_cost = fc_core.LEDGER.total()
        jobs = [(t, r) for t in tasks for r in range(a.runs)]
        with ThreadPoolExecutor(max_workers=a.parallel) as ex:
            results = list(ex.map(lambda j: fc_implement.run_task(repo, proj, j[0], a.attempts, model=model, reset_files=True,
                                                     tag=f"-bench-{re.sub(r'[^a-z0-9]+', '-', model.lower())}-r{j[1]}"), jobs))
        raw.extend({**r, "bench_model": model} for r in results)
        passed = [r for r in results if r["status"] == "passed"]
        times = [r["seconds"] for r in results]
        rows.append({"model": model, "tasks_x_runs": len(results), "pass_rate": round(len(passed) / len(results), 3),
                     "attempts_mean": round(sum(r["attempts"] for r in results) / len(results), 2),
                     "attempts_max": max(r["attempts"] for r in results),
                     "p50_s": round(pct(times, 0.5), 1), "p95_s": round(pct(times, 0.95), 1),
                     "cost_per_task": round((fc_core.LEDGER.total() - before_cost) / len(results), 5)})
        for r in results:  # keep branches for inspection, drop the clones
            import shutil
            shutil.rmtree(r["worktree"], ignore_errors=True)
    print()
    print(f"{'model':28} {'n':>3} {'pass':>6} {'att.mean':>8} {'att.max':>7} {'p50':>8} {'p95':>8} {'$/task':>9}")
    for r in rows:
        print(f"{r['model']:28} {r['tasks_x_runs']:>3} {r['pass_rate']:>6.0%} {r['attempts_mean']:>8} {r['attempts_max']:>7} "
              f"{fc_core.fmt(r['p50_s']):>8} {fc_core.fmt(r['p95_s']):>8} {'$' + format(r['cost_per_task'], '.4f'):>9}")
    fc_core.say(f"bench done  wall {fc_core.fmt(time.time() - t0)}")
    fc_core.say(fc_core.LEDGER.line())
    out = proj / "results" / f"bench-{time.strftime('%Y%m%d-%H%M%S')}.json"
    fc_core.atomic_write(out, json.dumps({"summary": rows, "runs": raw, "cost": fc_core.LEDGER.snapshot(), "seconds": round(time.time() - t0, 1)}, indent=2))
    print(f"saved {out}")
