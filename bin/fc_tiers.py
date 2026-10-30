"""Model tiers: cheap, standard and frontier, the highest tier each role may start on, and the model a tier and a pin give."""

import fc_config

ORDER = fc_config.TIER_NAMES
PINS = (*ORDER, "no-frontier")
CEILINGS = {"cheap": ("small", "test_reader"),
            "standard": ("explore", "research", "perf", "toolsmith", "groundwork", "orchestrator", "implementer", "tests", "fix"),
            "frontier": ("planner", "architect", "designer", "designer-plan", "builder-check")}

def ceiling(role):
    for t in ORDER:
        if role in CEILINGS[t]:
            return t
    for t in ORDER:
        if role.partition("-")[0].rstrip("0123456789") in CEILINGS[t]:
            return t
    return None

def tier_of(model):
    for t in ORDER:
        names = [m for m, _ in fc_config.TIERS[t]]
        if model in names or "claude:" + model in names:
            return t
    return None

def tier_for(ceiling, pin=None):
    if pin in ORDER:
        return pin
    if pin == "no-frontier":
        return "standard" if ceiling is None or ceiling == "frontier" else ceiling
    return ceiling

def chain(tier, entries):
    kept = [e for e in entries if tier_of(e[0]) is None or ORDER.index(tier_of(e[0])) <= ORDER.index(tier)]
    return kept or list(fc_config.TIERS[tier])

def start_tier(depth, pin=None):
    if pin in ORDER:
        return pin
    return "standard" if depth == "DEEP" else "cheap"

def implementer_model(tier):
    t = tier_of(fc_config.DEFAULT_MODEL)
    return fc_config.DEFAULT_MODEL if t is None or t == tier else fc_config.TIERS[tier][0][0]

def escalate(model):
    return fc_config.TIERS["standard"][0][0] if tier_of(model) == "cheap" else None
