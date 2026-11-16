"""The reviewer: fast-model reviews of a diff, one per perspective, each high or medium finding confirmed by an independent call."""

import hashlib, json, os, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import fc_config
import fc_core
import fc_models


SIMPLICITY_BAR = ("A finding must be a defect against the plan's requirements. Hardening the requirements do not ask for (portability, "
                  "extra error handling, defensive guards, retries) is no defect: no hardening.")

PERSPECTIVES = {
    "correctness": "Find bugs: wrong logic, unhandled inputs, off-by-one, broken edge cases, error paths that lose data. Ignore style.",
    "security": "Find security and data-loss risks: injection, secrets in code, unsafe eval, missing validation at trust boundaries, "
                "destructive operations without guards, credentials leaving the client.",
    "spec": "Check the diff against the plan and acceptance criteria: unmet criteria, scope creep, gamed or weakened tests, "
            "hardcoded site-specific or example-specific logic where general behavior was required.",
    "docs": "Check whether the diff changes behavior, commands, config, or interfaces that the provided docs describe, "
            "and whether those docs were updated to match. Only flag real mismatches.",
}

def finding_id(i):
    """A short id for a review finding, from its file and its words (whitespace and case aside): what a dismissal names."""
    words = " ".join(str(i.get("note", "")).lower().split())
    return hashlib.sha256(f"{str(i.get('file', '')).removeprefix('./')}\n{words}".encode()).hexdigest()[:8]

def dismissed_file(proj):
    """Where the findings the team lead dismissed are kept: the project's dismissed.json, a list of {id, file, note, severity, reason, at}."""
    return Path(proj) / "dismissed.json"

def read_dismissed(proj):
    """The project's dismissals, none when the file is missing or damaged."""
    try:
        found = json.loads(dismissed_file(proj).read_text())
    except (OSError, ValueError):
        return []
    return [d for d in found if isinstance(d, dict) and d.get("id") and d.get("reason")] if isinstance(found, list) else []

def keep_dismissal(proj, finding, reason):
    """Keep the dismissal of `finding` with its reason (a second one for the same id replaces the first); returns the record."""
    record = {"id": finding_id(finding), "file": finding.get("file"), "line": finding.get("line"), "note": finding.get("note"), "severity": finding.get("severity"),
              "reason": " ".join(reason.split()), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    fc_core.atomic_write(dismissed_file(proj), json.dumps([d for d in read_dismissed(proj) if d["id"] != record["id"]] + [record], indent=2))
    return record

def last_confirmed(proj):
    """{id: finding} for every confirmed or held finding of the project's last verify and reviews (results/verify.json, smoke.json, review.json,
    review-*.json), the ones a dismissal may name."""
    found = {}
    results = Path(proj) / "results"
    for f in [results / "verify.json", results / "smoke.json", results / "review.json", *sorted(results.glob("review-*.json"))]:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        reviews = [r.get("review") for r in data.get("rounds", [])[-1:]] if "rounds" in data else [data.get("review")]
        for rv in reviews:
            for i in (rv or {}).get("confirmed", []) + (rv or {}).get("held", []):
                found[finding_id(i)] = i
    return found

def well_formed(i):
    """A finding in the shape the reviewer is asked for. The reply is always valid JSON, so a model that spends it on its
    reasoning returns objects of loose thoughts or the schema echoed back; those are not findings."""
    return (isinstance(i, dict) and isinstance(i.get("file"), str) and i["file"].strip() != "" and i.get("severity") in ("high", "medium", "low")
            and isinstance(i.get("note"), str) and i["note"].strip() != "" and (i.get("line") is None or type(i["line"]) is int))

def llm_review(cfg, diff, plan, docs_text, tests_pass, dismissed=()):
    """Parallel fast-model reviews, one per perspective, then a confirmation pass on each high or medium finding.
    Only confirmed findings count. Fails on any confirmed high. `dismissed`: the team lead's dismissals (read_dismissed): a finding
    with a dismissed id is not confirmed again (it comes back under "dismissed" with its reason), and the confirm call is told those
    claims were judged false, so the same claim in other words is rejected too."""
    by_id = {d["id"]: d for d in dismissed}
    env = {**fc_core.load_env(cfg.get("env_file", fc_config.ENV_FILE)), **os.environ}
    if fc_config.REVIEW_MODEL:
        env["TEXT_MODEL"] = fc_config.REVIEW_MODEL
    wanted = cfg.get("perspectives", ["correctness", "security", "spec"] + (["docs"] if docs_text else []))
    schema = ('Return JSON {"issues": [{"file": str, "line": int|null, "severity": "high|medium|low", "note": str}]}. '
              "Be concrete and terse. An empty list means clean. Never invent files not in the input. "
              "Severity: high only for a bug that breaks a stated requirement, loses data, or is a security hole. Performance or style "
              "concerns at the scale this project runs at are low, never high. " + SIMPLICITY_BAR + " Report it as low if you must mention it. "
              + ("The tests currently PASS; do not report anything the tests already prove works." if tests_pass else ""))
    user = (f"PLAN AND CRITERIA:\n{plan[:15000]}\n\nDIFF OR FILES (may be cut off at the end; a file that ends mid-line "
            f"is truncated by this pack, which is never a finding):\n{diff[:150000]}")
    def one(name):
        """(well-formed findings, how many other entries the reply held, why the call failed or None)."""
        try:
            out = fc_models.fast_json_whole(env, f"You are a strict code reviewer. Perspective: {PERSPECTIVES[name]} {schema}",
                            user + (f"\n\nDOCS:\n{docs_text[:20000]}" if name == "docs" else ""))
            # Small models sometimes return the issues array bare, or something else entirely; never let that crash the stage.
            issues = out.get("issues", []) if isinstance(out, dict) else (out if isinstance(out, list) else [])
            issues = issues if isinstance(issues, list) else []
            kept = [i for i in issues if well_formed(i)]
            malformed, failed = len(issues) - len(kept), None
        except fc_models.PaymentRequired:
            raise
        except Exception as e:  # noqa: BLE001
            # A reviewer that fails to answer is a coverage gap to report, not a defect in the code under review.
            kept, malformed, failed = [{"file": "-", "severity": "low", "note": f"{name} review did not return usable findings this round ({type(e).__name__}: {str(e)[:80]})", "confirmed": False}], 0, f"{type(e).__name__}: {str(e)[:200]}"
        for i in kept:
            i["perspective"] = name
        return kept, malformed, failed
    with ThreadPoolExecutor(max_workers=len(wanted)) as ex:
        batches = list(ex.map(one, wanted))
    raw, malformed = [i for kept, _, _ in batches for i in kept], sum(n for _, n, _ in batches)
    failed = [why for _, _, why in batches if why]
    if wanted and len(failed) == len(wanted):  # nothing was reviewed (every call rate-limited, say): never a pass
        return {"pass": False, "issues": raw, "confirmed": [], "malformed": malformed,
                "error": f"no review perspective returned a result ({len(failed)} of {len(wanted)} failed): {failed[0]}"}
    # Confirmation pass: an independent call must name the line that breaks a requirement and how it must read, or the claim is dropped.
    flat = lambda s: " ".join(str(s).split())
    told = ("DISMISSED BY THE TEAM LEAD (each claim was judged false, for the reason given; the CLAIM below is not a finding when it says the same, in any words):\n"
            + "\n".join(f"- {d.get('file')}: {' '.join(str(d.get('note', '')).split())[:300]} (reason: {d['reason'][:300]})" for d in list(dismissed)[-20:]) + "\n\n") if dismissed else ""
    def confirm(i):
        if i.get("confirmed") or i.get("severity") not in ("high", "medium"):
            i.setdefault("confirmed", False)
            return i
        if (d := by_id.get(finding_id(i))) is not None:  # the team lead judged this claim false: no call, no finding
            i["confirmed"], i["dismissed"], i["evidence"] = False, d["reason"], "dismissed by the team lead: " + d["reason"]
            return i
        docs = docs_text[:20000] if i.get("perspective") == "docs" else ""
        try:
            # The verdict comes last, so the model compares the line with the requirement before it decides.
            v = fc_models.fast_object(
                env, "You are verifying one code-review claim against the actual code, the plan, and the design context. Fill the fields "
                'in order. "requirement": the words of the plan, design context, or docs the claim says are broken, copied verbatim; for a '
                'crash, data loss, or security hole, one sentence naming it. "wrong_line": the line of the code or docs that breaks it, '
                'copied verbatim, nothing else. "fixed_line": that line as it must read to meet the requirement ("" if it must go). '
                '"confirmed": true only if wrong_line really breaks the requirement. If the code already does what the requirement says, '
                "the claim's own text says the code is correct, or the claim contradicts the plan or the design context, confirmed is "
                "false. If the claim is wrong, speculative, contradicted by passing tests, or about style, reject it. "
                '"proof": the name of a test in the code, copied verbatim, whose assertions already prove the code does what the claim says it '
                'does not ("" if no test does). ' + SIMPLICITY_BAR,
                told + (f"DESIGN CONTEXT (authoritative):\n{cfg.get('review_context')}\n\n" if cfg.get("review_context") else "") +
                f"PLAN AND CRITERIA:\n{plan[:15000]}\n\nCLAIM: [{i['severity']}] {i['file']}{':' + str(i['line']) if i.get('line') else ''}: {i['note']}\n\n"
                f"CODE (may be cut off at the end; truncation is not evidence):\n{diff[:150000]}\n\n" + (f"DOCS:\n{docs}\n\n" if docs else "")
                + ("The tests currently pass." if tests_pass else ""),
                '{"requirement": str, "wrong_line": str, "fixed_line": str, "proof": str, "confirmed": bool}', "confirmed")
            wrong, i["fix"] = str(v.get("wrong_line") or ""), str(v.get("fixed_line") or "")
            i["confirmed"], i["evidence"] = bool(v["confirmed"]), wrong[:300]
            if i["confirmed"] and (not flat(wrong) or flat(wrong) not in flat(diff + "\n" + docs)):
                i["confirmed"], i["evidence"] = False, "evidence is not a quote from the code: " + wrong[:300]
            elif i["confirmed"] and flat(i["fix"]) == flat(wrong):
                i["confirmed"], i["evidence"] = False, "the line as it must read is the line as it is: " + wrong[:300]
            proof = flat(v.get("proof") or "")
            if i["confirmed"] and tests_pass and proof and proof in flat(diff + "\n" + docs):  # a claim a passing test disproves is no finding, however it reads
                i["confirmed"], i["evidence"] = False, "a passing test already proves it: " + proof[:300]
        except fc_models.PaymentRequired:
            raise
        except fc_models.BadReply:  # the claim went unjudged: the model's reply is not the code's defect, and it must not fail verify
            i["confirmed"], i["evidence"], i["unjudged"] = False, "confirm unparseable, not judged", True
        except Exception as e:  # noqa: BLE001
            i["confirmed"], i["evidence"], i["judge_error"] = False, str(e), True
        return i
    with ThreadPoolExecutor(max_workers=3) as ex:
        issues = list(ex.map(confirm, raw))
    confirmed = [i for i in issues if i["confirmed"]]
    errors = [i for i in issues if i.get("judge_error")]
    skipped = [{"id": finding_id(i), "file": i["file"], "note": i["note"], "reason": i["dismissed"]} for i in issues if i.get("dismissed")]
    return {"pass": not errors and not any(i["severity"] == "high" for i in confirmed), "issues": issues, "confirmed": confirmed, "malformed": malformed,
            **({"dismissed": skipped} if skipped else {}), **({"error": f"{len(errors)} confirm call(s) failed: {errors[0]['evidence']}"} if errors else {})}
