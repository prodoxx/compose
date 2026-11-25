"""A layer's `## Risk Assessment` (sections/4.md), stated the way no-mistakes' review states it: one line, `<mark> <Level>: <one sentence>`.
The fast model judges each layer's own diff when a verify passes (assess, run by fc_layergate.layer_pass) and the answer is kept in
results/risk.json; the stack only reads it (risk_lines), so writing a PR's text still calls no model."""
import json, re
from pathlib import Path
import fc_config, fc_core, fc_models, fc_routerec, fc_tasks

MARK = {"low": "✅", "medium": "⚠️", "high": "🚨"}
RATIONALE_CHARS = 300  # one sentence; a longer reply is cut at a word and ends in `...`
DIFF_CHARS = 150000  # the reviewers' own cut (fc_review)
SHAPE = '{"risk_level": "low|medium|high", "risk_rationale": str}'
# The three levels are no-mistakes' own words (its review prompt), so a level means the same in both tools' PRs.
SYSTEM = ("You assess how risky one pull request is to merge, from its diff. Its checks and tests pass, and the code review found nothing "
          "that blocks it; the review findings still open on its files are listed. "
          'Set risk_level to "low" if the change is well-bounded, mostly cosmetic, or straightforward with little ambiguity. '
          'Set risk_level to "medium" if the change has room to improve but is safe to merge first with concerns addressed as follow-ups. '
          'Set risk_level to "high" if the change should not be merged without explicit human approval - it is fundamental, risky, '
          "ambiguous, or has strong negative signals. "
          "Provide a risk_rationale of one short sentence, at most 200 characters, explaining why you chose that risk level: name what "
          "could break and where a reviewer should look. Judge the diff, never its size alone, and never the process that built it.")
CHANGED = re.compile(r"^\+\+\+ b/(.+)$", re.M)

def kept_file(proj):
    """Where the layers' assessments are kept: {<PR file name>: {level, rationale, below, tree}}, the trees being what was judged."""
    return Path(proj) / "results" / "risk.json"

def read_kept(proj):
    """The kept assessments, none when the file is missing or damaged."""
    try:
        found = json.loads(kept_file(proj).read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in found.items() if isinstance(v, dict) and v.get("level") in MARK and v.get("rationale")} if isinstance(found, dict) else {}

def tree(repo, ref):
    """The git tree sha of `ref`, "" when it cannot be told."""
    p = fc_core.sh(["git", "rev-parse", "--verify", "-q", f"{ref}^{{tree}}"], repo, check=False)
    return p.stdout.strip() if p.returncode == 0 else ""

def risk_lines(repo, proj, name, branch):
    """The `## Risk Assessment` line of the layer on `branch` whose PR file is `name`; [] when it was never judged, or its tree is
    no longer the one that was (a stale level would misstate the PR)."""
    e = read_kept(proj).get(name)
    return [f"{MARK[e['level']]} {e['level'].capitalize()}: {e['rationale']}"] if e and e.get("tree") == tree(repo, branch) else []

def task_notes(layer, tasks, issues):
    """One line per task of the layer: its title, and the depth compose built it at with the reason (its issue's `## Route`), which
    names the verification policies that cover its files."""
    by_id, lines = {m["id"]: m for m in tasks}, []
    for t in dict.fromkeys(layer["tasks"]):
        if not (m := by_id.get(t)):
            continue
        route = fc_routerec.parse_route((issues.get(m.get("linear")) or {}).get("description") or "") or {}
        checks = (route.get("verification requirements") or "none").strip()
        built = f"built at depth {route['depth']}: {route.get('reason') or 'no reason kept'}" + (f"; checks required: {checks}" if checks != "none" else "") if route.get("depth") else ""
        lines.append(f"- {m['title']}" + (f" ({built})" if built else ""))
    return lines

def assess(repo, proj, layers, names, below, tasks, findings=(), fresh=False):
    """Judge each layer (bottom first, `names` its PR file names, `below` what the bottom one grows from) on its own diff and keep
    the answers, replacing the file: a layer no longer in the stack loses its entry. `findings` are the review's confirmed ones;
    each layer is told those on the files it changes. A layer whose tree and the tree below it are the ones judged before is not
    asked about again unless `fresh`. A call that fails or a reply that is no assessment leaves the layer without one (its PR has no
    section) and says so: this never fails a verify."""
    env = {**fc_models.fast_env(), **({"TEXT_MODEL": fc_config.REVIEW_MODEL} if fc_config.REVIEW_MODEL else {})}
    issues = fc_tasks.linear_cache(Path(proj)).get("issues") or {}
    kept, out, prev = read_kept(proj), {}, below
    for i, (l, name) in enumerate(zip(layers, names), 1):
        label, trees = f"layer {i} of {len(layers)} ({l['branch']})", {"below": tree(repo, prev), "tree": tree(repo, l["branch"])}
        rng, prev = f"{prev}...{l['branch']}", l["branch"]
        old = kept.get(name)
        if old and not fresh and {k: old.get(k) for k in trees} == trees:
            out[name] = old
        else:
            diff = fc_core.sh(["git", "diff", rng], repo, check=False).stdout
            if not diff.strip():
                continue
            files = set(CHANGED.findall(diff))
            notes = [f"- [{f.get('severity')}] {str(f.get('file') or '').removeprefix('./')}{':' + str(f['line']) if f.get('line') else ''}: {f.get('note')}"
                     for f in findings if str(f.get("file") or "").removeprefix("./") in files]
            user = ("TASKS IN THIS PULL REQUEST:\n" + ("\n".join(task_notes(l, tasks, issues)) or "none: groundwork only") +
                    "\n\nREVIEW FINDINGS STILL OPEN ON ITS FILES:\n" + ("\n".join(notes) or "none") +
                    f"\n\nDIFF (may be cut off at the end; truncation is no risk in itself):\n{diff[:DIFF_CHARS]}")
            try:
                v = fc_models.fast_object(env, SYSTEM, user, SHAPE, "risk_level", max_tokens=500)
                level, why = str(v.get("risk_level") or "").strip().lower(), " ".join(str(v.get("risk_rationale") or "").split())
            except Exception as e:  # noqa: BLE001  the verdict stands without it
                fc_core.say(f"risk   not assessed  {label}: {type(e).__name__}: {str(e)[:200]}")
                continue
            if level not in MARK or not why:
                fc_core.say(f"risk   not assessed  {label}: the reply names no level and reason")
                continue
            out[name] = {"level": level, "rationale": why if len(why) <= RATIONALE_CHARS else why[:RATIONALE_CHARS - 3].rsplit(" ", 1)[0] + "...", **trees}
        fc_core.say(f"risk   {out[name]['level']:<6}  {label}  {out[name]['rationale']}")
    fc_core.atomic_write(kept_file(proj), json.dumps(out, indent=2))
    return out
