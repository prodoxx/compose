"""The model a teammate session runs: the models.crew entry its role resolves to, its fallback chain under the role's tier, and the
first model in that chain its harness can run."""
import sys
import fc_config, fc_claude, fc_harness, fc_models, fc_tiers


def crew_pick(role, explicit=None, pin=None):
    return crew_pick_full(role, explicit, pin)[:3]

def crew_chain(role, explicit=None, pin=None):
    """(key, chain): the models.crew entry a role resolves to and its (model, effort) list in order, unprobed; (None, []) means bare `claude`."""
    return _resolve(role, explicit, pin)[:2]

def _resolve(role, explicit, pin):
    """(key, chain, lowered): crew_chain, and a phrase saying what lowered the first model of its entry to the role's tier, else None.
    A replacement keeps the effort the entry sets."""
    lowered = None
    if explicit and explicit not in fc_config.CREW_MODELS:
        key, chain = "--model", [(explicit, None)]
    else:
        first = role.split("-", 1)[0].rstrip("0123456789")
        key = explicit or (role if role in fc_config.CREW_MODELS else
                           first if first != role and first in fc_config.CREW_MODELS else
                           role if role in fc_config.CREW_DEFAULTS else
                           "planner" if role in fc_config.PLANNING_ROLES and "planner" in fc_config.CREW_MODELS else
                           "default" if "default" in fc_config.CREW_MODELS else None)
        chain = [] if key is None else [(fc_config.CREW_MODELS[key], fc_config.CREW_EFFORT.get(key)), *fc_config.CREW_FALLBACK.get(key, [])] \
            if key in fc_config.CREW_MODELS else [fc_config.CREW_DEFAULTS[key]]
        if key is not None and (tier := fc_tiers.tier_for(fc_tiers.ceiling(role) or fc_tiers.ceiling(key), pin)) is not None:
            kept = fc_tiers.chain(tier, chain)
            if kept[0] != chain[0]:
                lowered = f"lowered to the {tier} tier by " + ("its role's tier ceiling" if tier == (fc_tiers.ceiling(role) or fc_tiers.ceiling(key)) else "a tier pin or the frontier budget")
                if kept[0][1] is None and chain[0][1]:
                    kept[0] = (kept[0][0], chain[0][1])
            chain = kept
    return key, chain, lowered

def crew_pick_full(role, explicit=None, pin=None):
    """(model, effort, note, skipped) where skipped lists why each earlier choice was passed over. (model, effort, note) for a teammate session. `--model` (a `claude:<id>` or `pi:<provider/model>`, or a name under models.crew such as `small`) wins,
    then models.crew[<role>], then `planner` for the planning role (the architect teammate; never the team lead, which delegates), then `default`, then bare `claude`. An entry with a `fallback` (one entry or an ordered list)
    probes its model first and then each fallback in order, taking the first one its harness can run; when none can, it exits naming each model and the error its probe got, rather than starting a session that never comes up. Teammate sessions run in pi or Claude Code."""
    key, chain, lowered = _resolve(role, explicit, pin)
    if key is None:
        return "claude", None, "", []
    label = "--model" if key == "--model" else f"models.crew.{key}" + (" default" if key == role and role in fc_config.CREW_DEFAULTS and key not in fc_config.CREW_MODELS else "") + (f", {lowered}" if lowered else "")
    for m, _ in chain:
        if not (fc_claude.is_claude(m) or str(m).startswith("pi:") or fc_harness.harness_of(m)):
            where = "" if key == "--model" else f" in {fc_config.CONFIG_PATH}"
            rows = [fc_harness.HARNESSES[k] | {"key": k} for k in fc_harness.rows()]
            names = ["Claude Code", "pi", *(r["name"] for r in rows)]
            uses = ["claude:<model-id> (e.g. claude:claude-sonnet-5-5)", "pi:<provider/model> (e.g. pi:openai/gpt-6-astra)",
                    *(f"{r['key']}:<model> (e.g. {r['key']}:{r['example']})" for r in rows)]
            join = lambda xs: ", ".join(xs[:-1]) + ", or " + xs[-1]
            sys.exit(f"{label} is {m!r}, but teammate sessions run in {join(names)}: use {join(uses)}{where}; OpenCode ids are for implementers only")
    skipped = []
    for model, effort in chain:
        if (ok := fc_models.model_available(model))[0]:
            break
        skipped.append(f"{model} unavailable ({ok[1]})")
    else:
        sys.exit(f"{label}: no model in its chain can run: {', '.join(skipped)}. Fix the entry in {fc_config.CONFIG_PATH} or pass --model; "
                 f"`compose models --probe` re-asks after a transient failure")
    return model, effort, label + (": " + ", ".join(skipped) + ", using its fallback" if skipped else ""), skipped

def crew_model(role, explicit=None):
    return crew_pick(role, explicit)[0]

def crew_effort(role, explicit=None):
    """The Claude Code effort for the entry crew_pick chose, or None to keep Claude Code's default."""
    return crew_pick(role, explicit)[1]
