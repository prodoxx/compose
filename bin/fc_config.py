from __future__ import annotations  # the X | None annotations on /usr/bin/python3 3.9
import json, os, re, sys
from pathlib import Path
import fc_auth, fc_core, fc_harness


# ---------- global config and workspaces ----------
# ~/.compose/config.json: top-level settings, and `workspaces` whose active entry (`workspace`) overrides them. It never
# holds a key: every key lives in auth.json (fc_auth), and old ones are moved there on the next apply_config.
#   {"workspace": "work",
#    "models": {"implementer": "pi:...", "fallback": "pi:..." or null, "fallback_after": 2, "test_reader": "claude:<id>" or null, "fix": "pi:...",
#               "crew": {"orchestrator": "claude:<id>", "<role>": "claude:<id>", "default": "claude:<id>", "small": "claude:<id>"}},
#    "env": {"OTHER_VAR": "..."},
#    "parallel": 20,
#    "workspaces": {"work": {}, "personal": {"models": {...}}}}
# `default` is the top level itself; it always exists and is active when `workspace` is unset.
# `parallel` is how many agents start at once: the tasks of run and resume (else 3) and testcrews' writers (else all at once).
# A command's own flag wins. designcrews never reads it: Figma's per-minute write rate limit caps its builders.
# `claude_parallel` (default 3) caps the Claude Code sessions of those commands that run at once, apart from the pi and OpenCode ones:
# parallel Opus writers slow a machine more than the same number of GLM calls.
# A model id names its harness: `pi:<provider/model>` runs pi, `claude:<model-id>` runs Claude Code with that model, bare
# `claude` runs Claude Code with its default model, anything else is an OpenCode model id. Teammate sessions run in pi or Claude Code.

DEFAULT_MODEL = "pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast"  # pi harness; the OpenRouter preset pins Together, then CoreWeave
# Takes over after FALLBACK_AFTER failed attempts (misses, errors, timeouts) on a task's model, or at once on a rate limit. None by
# default: GLM passes where gpt-oss kept missing, so a second model is opt-in (`models.fallback` in the config).
FALLBACK_MODEL = None
FALLBACK_AFTER = 2
# Reads a failed test run and writes the implementer's short fix brief, in Claude Code with read-only tools. None sends the raw tail alone.
TEST_READER = "claude:claude-haiku-5-5"
# Reviews a diff in verify and `compose review`, and confirms each finding: `models.review` in the config. A `claude:<id>` runs
# Claude Code on the subscription, any other id the OpenRouter fast-model endpoint. None keeps the .env's TEXT_MODEL.
REVIEW_MODEL = None
READINESS_MODEL = ("claude:claude-opus-5-5", "high")  # the stronger judge of a technical story's readiness: `models.readiness`, a claude: id or {"model", "effort"}
NIGHT_MODEL = ("claude:claude-opus-5-5", "high")  # the overnight lead: `models.night`, a claude: id or {"model", "effort"}
JEV_DIR = Path(__file__).resolve().parent.parent / "jev"  # the browser agent verify's e2e cases drive: its own uv project
# Harness-reported cost is wrong when a request is pinned to a specific host: OpenCode prices by OpenRouter's catalog default, not
# the pinned provider's rate. These override the reported cost (dollars per million input, output) for the models we pin. The claude:
# entries are list prices, (input, output, cache write, cache read) dollars per million tokens, for reference dollars.
BUILTIN_PRICES = {"openrouter/openai/gpt-oss-120b": (0.35, 0.75), "openrouter-fast/openai/gpt-oss-120b@preset/gpt-oss-120b-cerebras": (0.35, 0.75),
                  "openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast": (0.15, 0.50),
                  "claude:claude-opus-5-5": (4, 20, 5, 0.20), "claude:claude-sonnet-5-5": (2, 10, 2.50, 0.10),
                  "claude:claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01), "claude:claude-fable-5-1": (10, 50, 12.50, 0.25)}
PRICES = dict(BUILTIN_PRICES)
CONFIG_PATH = Path(os.environ.get("COMPOSE_CONFIG", Path.home() / ".compose" / "config.json"))
ENV_FILE = CONFIG_PATH.parent / ".env"  # TEXT_MODEL* (the fast model: review unless models.review, jev's text helper) and TYPESAFE_MODEL; keys live in auth.json; the process env wins
AUTH_PATH = CONFIG_PATH.parent / "auth.json"  # every key: see fc_auth; mode 600 in a 700 directory
OPENROUTER_KEY = None  # the active workspace's key; None leaves each harness on its own auth store
MAX_DIFF_LINES = 350          # per task branch, added plus deleted, against its base; a reviewer reads one layer at a time
DOC_RE = re.compile(r"(^|/)(docs?|documentation)/|\.(md|mdx|rst|txt|adoc)$", re.I)  # implementers never add documents
DEP_DIRS = ("node_modules", "vendor", ".venv")  # git-ignored installs a task clone borrows from the main checkout
# trail otherwise appends /graft/ to the tracked .gitignore and writes .ignore in every clone it builds or refreshes, which
# the scope check rightly reads as an edit outside the task. Children (pi, OpenCode, their trail calls) inherit this.
os.environ.setdefault("GRAFT_NO_GITIGNORE", "1")
os.environ.setdefault("GRAFT_NO_IGNORE", "1")

CREW_MODELS = {}  # models.crew from the config: role (or default, small) -> claude:<model-id>
PARALLEL = None  # `parallel` from the config; None keeps each command's own default
CLAUDE_PARALLEL = 3  # `claude_parallel` from the config: how many Claude Code writers (run's tasks, testcrews' test writers) run at once
LOAD = {}  # `load` from the config: the keys that move limits() off its defaults
FACTORY_DEFAULTS = {"day_tickets": 2, "turn_seconds": 120, "offer_snooze_minutes": 60,
                    "night_tickets": 6, "night_until": "07:00", "night_budget": 20.0, "prep_ahead": 3}
FACTORY = dict(FACTORY_DEFAULTS)  # `factory` from the config over FACTORY_DEFAULTS: the build loop's pace, turn length and offer snooze
COMPACT_DEFAULTS = {"judged": True, "minimum": 40000, "budget": 150000, "ceiling": 150000, "idle_seconds": 180, "cooldown": 20000}
COMPACT = dict(COMPACT_DEFAULTS)  # `compact` from the config over COMPACT_DEFAULTS: when a session is compacted at a task boundary
LINEAR_KEY = None  # the active workspace's Linear API key, set by apply_config; never printed or passed in argv
WORKSPACE = "default"
CREW_EFFORT = {}  # the same keys -> Claude Code --effort, for entries written as {"model": ..., "effort": ...}
CREW_FALLBACK = {}  # the same keys -> [(model, effort), ...] to try in order when the entry's own model fails its availability probe
PLANNING_ROLES = ("architect",)  # the change-of-course architect (step 8) plans on `planner`; the team lead delegates, so it never gets `planner`
CREW_DEFAULTS = {"designer": ("claude:claude-opus-5-5", "high")}  # role -> (model, effort) used when models.crew has no entry for it
PROBE_CACHE = CONFIG_PATH.parent / "model-probe.json"
TIER_NAMES = ("cheap", "standard", "frontier")
DEFAULT_TIERS = {"cheap": ["claude:claude-haiku-5-5", "pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast"],
                 "standard": ["claude:claude-sonnet-5-5"],
                 "frontier": [{"model": "claude:claude-opus-5-5", "effort": "high"}, {"model": "claude:claude-fable-5-1", "effort": "high"},
                              {"model": "pi:openai/gpt-6-astra", "effort": "high"}]}
TIERS = {t: [((e.get("model"), e.get("effort")) if isinstance(e, dict) else (e, None)) for e in es] for t, es in DEFAULT_TIERS.items()}
PROVIDERS = {}  # name -> the active settings' providers entry (base URL, optional models list), set by apply_config
HARNESS = None  # the session's harness, set by apply_config; its block under "harnesses" picks the crew's models


def read_config():
    return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}

def write_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fc_core.atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2) + "\n", keep_secrets=True)  # a config env's *_API_KEY stays as it is
    CONFIG_PATH.chmod(0o600)  # settings stay private

def session_harness(flag=None):
    """The session's harness: the flag, else $COMPOSE_HARNESS, else what its own environment names, else "claude" under
    Claude Code ($CLAUDECODE=1), else None."""
    return flag or os.environ.get("COMPOSE_HARNESS") or fc_harness.detected() or ("claude" if os.environ.get("CLAUDECODE") == "1" else None)

def _stand_in_block(out, stand):
    """A harness block derived from the merged settings: each Claude model becomes `stand`."""
    swap = lambda m: stand if isinstance(m, str) and (m == "claude" or m.startswith("claude:")) else m
    models = out.get("models") or {}
    crew = {}
    for role, e in (models.get("crew") or {}).items():
        if isinstance(e, dict):
            e = dict(e)
            e["model"] = swap(e.get("model"))
            if "fallback" in e:
                fbs = e["fallback"] if isinstance(e["fallback"], list) else [e["fallback"]]
                fbs = [{**f, "model": swap(f.get("model"))} if isinstance(f, dict) else swap(f) for f in fbs]
                fbs = [f for f in fbs if (f.get("model") if isinstance(f, dict) else f) != e["model"]]
                if fbs:
                    e["fallback"] = fbs
                else:
                    del e["fallback"]
        else:
            e = swap(e)
        crew[role] = e
    crew.setdefault("default", stand)
    for role in CREW_DEFAULTS:
        crew.setdefault(role, stand)
    block = {"crew": crew}
    for k in ("implementer", "fallback", "fix", "test_reader"):
        m = models.get(k, TEST_READER if k == "test_reader" else None)
        if m is not None:
            block[k] = swap(m)
    tiers = dict(models.get("tiers") or {})
    for t in TIER_NAMES:
        tiers[t] = [{**e, "model": swap(e.get("model"))} if isinstance(e, dict) else swap(e) for e in tiers.get(t, DEFAULT_TIERS[t])]
    block["tiers"] = tiers
    return {"models": block}

def settings(cfg, harness=None):
    """The active workspace's settings: the top level with the workspace's entry merged over it, one level deep, then
    harness's block under "harnesses" merged over that the same way ("harnesses" itself never reaches the result)."""
    name = cfg.get("workspace") or "default"
    if name != "default" and name not in cfg.get("workspaces", {}):
        sys.exit(f"{CONFIG_PATH}: active workspace {name!r} is not under \"workspaces\"")
    ws = cfg.get("workspaces", {}).get(name, {})
    out = {k: v for k, v in cfg.items() if k not in ("workspace", "workspaces")}
    for k, v in ws.items():
        out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    blocks = out.pop("harnesses", None) or {}
    for n, block in blocks.items():
        if "default" not in (block.get("models") or {}).get("crew", {}):
            sys.exit(f'{CONFIG_PATH}: harnesses.{n}.models.crew has no "default"')
    stand = fc_harness.HARNESSES.get(harness, {}).get("stand_in")
    if stand and harness not in blocks:
        blocks = {**blocks, harness: _stand_in_block(out, stand)}
    if harness and harness != "claude" and harness not in blocks:
        sys.exit(f'{CONFIG_PATH}: no "harnesses.{harness}" block')
    for k, v in (blocks.get(harness) or {}).items():
        out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return name, out

def compact_off():
    """Why compaction is off for this session (COMPOSE_NO_COMPACT is set and non-empty), else None."""
    return "COMPOSE_NO_COMPACT is set" if os.environ.get("COMPOSE_NO_COMPACT") else None

def apply_config(move=True, harness=None):
    """Point models, keys, and env at the active workspace. Its key wins over the shell and the jev .env, and every
    OpenRouter caller (pi, OpenCode, the fast-model review, jev's e2e text helper) bills to it."""
    global DEFAULT_MODEL, FALLBACK_MODEL, FALLBACK_AFTER, TEST_READER, REVIEW_MODEL, READINESS_MODEL, NIGHT_MODEL, OPENROUTER_KEY, CREW_MODELS, CREW_EFFORT, CREW_FALLBACK, LINEAR_KEY, WORKSPACE, PARALLEL, CLAUDE_PARALLEL, LOAD, FACTORY, COMPACT, PROVIDERS, TIERS, PRICES, HARNESS
    if move:
        for line in fc_auth.move_keys(CONFIG_PATH, ENV_FILE, AUTH_PATH):  # old keys leave config.json and .env first, stdout stays clean
            print(line, file=sys.stderr)
    harness = session_harness(harness)
    name, st = settings(read_config(), harness)
    PROVIDERS = st.get("providers", {})
    for pname, p in PROVIDERS.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", pname):
            sys.exit(f"{CONFIG_PATH}: provider names are lowercase letters, digits, - and _: {pname!r}")
        if pname in ("openrouter", "openrouter-fast"):
            sys.exit(f"{CONFIG_PATH}: providers.{pname} is reserved: openrouter models already route there")
        if pname in fc_harness.RESERVED:
            sys.exit(f"{CONFIG_PATH}: providers.{pname} is reserved: {', '.join(fc_harness.RESERVED)} name harnesses, not providers")
        if not (isinstance(p.get("base_url"), str) and p["base_url"].startswith(("http://", "https://"))):
            sys.exit(f"{CONFIG_PATH}: providers.{pname} needs a base_url such as http://localhost:11434/v1")
        ids = p.get("models")
        if ids is not None and (not isinstance(ids, list) or any(not isinstance(i, str) for i in ids)):
            sys.exit(f"{CONFIG_PATH}: providers.{pname}.models must be a list of model ids")
    PARALLEL = st.get("parallel")
    if PARALLEL is not None and (type(PARALLEL) is not int or PARALLEL < 1):
        sys.exit(f"{CONFIG_PATH}: parallel must be a whole number of 1 or more, not {PARALLEL!r}")
    CLAUDE_PARALLEL = st.get("claude_parallel", 3)
    if type(CLAUDE_PARALLEL) is not int or CLAUDE_PARALLEL < 1:
        sys.exit(f"{CONFIG_PATH}: claude_parallel must be a whole number of 1 or more, not {CLAUDE_PARALLEL!r}")
    LOAD = st.get("load", {})
    for k in ("high", "low", "warn"):
        v = LOAD.get(k, 1)
        if type(v) not in (int, float) or v <= 0:
            sys.exit(f"{CONFIG_PATH}: load.{k} must be a number above 0, not {LOAD[k]!r}")
    if "high" in LOAD and "low" in LOAD and LOAD["low"] >= LOAD["high"]:
        sys.exit(f"{CONFIG_PATH}: load.low must be below load.high")
    FACTORY = {**FACTORY_DEFAULTS, **st.get("factory", {})}
    for k, least in (("day_tickets", 1), ("turn_seconds", 10), ("offer_snooze_minutes", 1)):
        if type(FACTORY[k]) is not int or FACTORY[k] < least:
            sys.exit(f"{CONFIG_PATH}: factory.{k} must be a whole number of {least} or more, not {FACTORY[k]!r}")
    for k, least in (("night_tickets", 1), ("prep_ahead", 0)):
        if type(FACTORY[k]) is not int or FACTORY[k] < least:
            sys.exit(f"{CONFIG_PATH}: factory.{k} must be a whole number of {least} or more, not {FACTORY[k]!r}")
    if type(FACTORY["night_budget"]) not in (int, float) or FACTORY["night_budget"] < 0:
        sys.exit(f"{CONFIG_PATH}: factory.night_budget must be a number of dollars, 0 or more, not {FACTORY['night_budget']!r}")
    if not (isinstance(FACTORY["night_until"], str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", FACTORY["night_until"])):
        sys.exit(f"{CONFIG_PATH}: factory.night_until must be HH:MM, not {FACTORY['night_until']!r}")
    compact = st.get("compact", {})
    if not isinstance(compact, dict):
        sys.exit(f"{CONFIG_PATH}: compact must be an object of settings, not {compact!r}")
    for k in compact:
        if k not in COMPACT_DEFAULTS:
            sys.exit(f"{CONFIG_PATH}: compact has no setting {k!r}: it takes {', '.join(COMPACT_DEFAULTS)}")
    COMPACT = {**COMPACT_DEFAULTS, **compact}
    if type(COMPACT["judged"]) is not bool:
        sys.exit(f"{CONFIG_PATH}: compact.judged must be true or false, not {COMPACT['judged']!r}")
    for k, least in (("minimum", 1), ("budget", 1), ("ceiling", 1), ("idle_seconds", 0), ("cooldown", 0)):
        if type(COMPACT[k]) is not int or COMPACT[k] < least:
            sys.exit(f"{CONFIG_PATH}: compact.{k} must be a whole number of {least} or more, not {COMPACT[k]!r}")
    if COMPACT["ceiling"] < COMPACT["minimum"]:  # a ceiling below the minimum would compact before the judge is ever asked
        sys.exit(f"{CONFIG_PATH}: compact.ceiling must not be below compact.minimum")
    WORKSPACE = name
    HARNESS = harness
    auth = fc_auth.read_auth(AUTH_PATH)
    held = auth.get("workspaces", {})
    own = held.get(name, {}).get("openrouter")
    if own is not None and not own:  # the workspace's own empty key must not bill default's account
        sys.exit(f"{AUTH_PATH}: workspace {name!r} has an empty openrouter key")
    OPENROUTER_KEY = own if own is not None else held.get("default", {}).get("openrouter")
    LINEAR_KEY = fc_auth.workspace_key(auth, name, "linear") or None  # the CLI moves Linear states with it (full path only)
    models = st.get("models", {})
    crew = models.get("crew") or {}  # an entry is "claude:<id>" or {"model": "claude:<id>", "effort": "high"}
    CREW_MODELS = {k: (v.get("model") if isinstance(v, dict) else v) for k, v in crew.items()}
    CREW_EFFORT = {k: v["effort"] for k, v in crew.items() if isinstance(v, dict) and v.get("effort")}
    entry = lambda e: (e.get("model"), e.get("effort")) if isinstance(e, dict) else (e, None)
    CREW_FALLBACK = {k: [entry(f) for f in (v["fallback"] if isinstance(v["fallback"], list) else [v["fallback"]])]
                     for k, v in crew.items() if isinstance(v, dict) and v.get("fallback")}  # one entry or an ordered list
    tiers = models.get("tiers") or {}
    for t in tiers:
        if t not in TIER_NAMES:
            sys.exit(f"{CONFIG_PATH}: models.tiers.{t}: tiers are cheap, standard, frontier")
    TIERS = {t: [entry(e) for e in tiers.get(t, DEFAULT_TIERS[t])] for t in TIER_NAMES}
    user_prices = st.get("prices", {})
    if not isinstance(user_prices, dict):
        sys.exit(f"{CONFIG_PATH}: prices must be an object of model id to 2 or 4 numbers, not {user_prices!r}")
    for m, p in user_prices.items():
        if not (isinstance(p, list) and len(p) in (2, 4) and all(isinstance(n, (int, float)) for n in p)):
            sys.exit(f"{CONFIG_PATH}: prices.{m} must be 2 or 4 numbers")
    PRICES = {**BUILTIN_PRICES, **user_prices}
    DEFAULT_MODEL = models.get("implementer", DEFAULT_MODEL)
    FALLBACK_MODEL = models.get("fallback", FALLBACK_MODEL)
    FALLBACK_AFTER = int(models.get("fallback_after", FALLBACK_AFTER))
    TEST_READER = models.get("test_reader", TEST_READER)
    REVIEW_MODEL = models.get("review", REVIEW_MODEL)
    if "readiness" in models:
        READINESS_MODEL = entry(models["readiness"])
        if not (isinstance(READINESS_MODEL[0], str) and READINESS_MODEL[0].startswith("claude:")):
            sys.exit(f"{CONFIG_PATH}: models.readiness must be a claude: model id")
    else:
        READINESS_MODEL = ("claude:claude-opus-5-5", "high")
    NIGHT_MODEL = entry(models["night"]) if "night" in models else ("claude:claude-opus-5-5", "high")
    if not (isinstance(NIGHT_MODEL[0], str) and NIGHT_MODEL[0].startswith("claude:")):
        sys.exit(f"{CONFIG_PATH}: models.night must be a claude: model id")
    if "fix" in models:
        os.environ["COMPOSE_FIX_MODEL"] = models["fix"]
    for m in [m for m in (models.get("implementer"), models.get("fallback"), models.get("fix"), models.get("test_reader"), *CREW_MODELS.values(),
                          *(f[0] for fs in CREW_FALLBACK.values() for f in fs)) if isinstance(m, str)]:
        if (hit := provider_of(m)) and (ids := PROVIDERS[hit[0]].get("models")) and hit[1] not in ids:
            sys.exit(f"{CONFIG_PATH}: {m} is not in providers.{hit[0]}.models: {', '.join(ids)}")
    os.environ.update({k: str(v) for k, v in st.get("env", {}).items()})
    fc_core.SECRET_VARS.update(k for k in st.get("env", {}) if k.endswith("_API_KEY"))
    if OPENROUTER_KEY:
        os.environ["OPENROUTER_API_KEY"] = os.environ["TEXT_MODEL_API_KEY"] = OPENROUTER_KEY
    if auth.get("typesafe"):
        os.environ["TYPESAFE_API_KEY"] = auth["typesafe"]
    for prov in PROVIDERS:
        if key := fc_auth.workspace_key(auth, WORKSPACE, f"provider:{prov}"):
            os.environ[provider_env(prov)] = key
            fc_core.SECRET_VARS.add(provider_env(prov))
    dotenv = fc_core.load_env(ENV_FILE)
    fc_core.SECRET_VALUES.update(v for v in [OPENROUTER_KEY, LINEAR_KEY, auth.get("typesafe"), *(key for ws in held.values() for key in ws.values()),
                                             *(dotenv[k].strip('"') for k in dotenv if k in fc_core.SECRET_VARS or k.endswith("_API_KEY"))] if v)
    return name

def needs_linear_key(what):
    """The line `what` exits with when the active workspace has no Linear key. On a scratch config (a compose under `compose
    timed`) storing a key would only fill the scratch one: the fix is to run the command directly."""
    if fc_core.is_scratch(CONFIG_PATH):
        return (f"{what} needs a Linear key, but runs on a scratch config with no workspace and no keys, as every command under "
                f"`compose timed` does: run compose's own commands directly")
    return f"{what} needs a Linear key for workspace {WORKSPACE!r}: pbpaste | compose workspace key {WORKSPACE} --linear"

def typesafe_key(env, what):
    """The TypeSafe key in `env` (apply_config puts auth.json's there), for every Jev call. Missing, it raises the line saying
    why: on a scratch config (a compose under `compose timed`) there are no keys by design, so the fix is to run the command
    directly; otherwise the command that stores it in auth.json."""
    if key := env.get("TYPESAFE_API_KEY"):
        return key
    if fc_core.is_scratch(CONFIG_PATH):
        raise RuntimeError(f"{what} needs a TypeSafe key, but runs on a scratch config with no workspace and no keys, as every "
                           f"command under `compose timed` does: run compose's own commands directly")
    raise RuntimeError(f"{what} needs a TypeSafe key, and {AUTH_PATH} has none: pbpaste | compose workspace key {WORKSPACE} --typesafe")

def openrouter_key():
    """The OpenRouter key a harness bills to: the workspace's from auth.json, else the shell's. None leaves the harness on its own auth store."""
    return OPENROUTER_KEY or os.environ.get("OPENROUTER_API_KEY") or None

def key_file(key, name="openrouter.key"):
    """A 0600 file beside the config holding `key`, for a harness that reads its key from a file its config names, not from the env its tools inherit."""
    f = CONFIG_PATH.parent / name
    if not (f.exists() and f.read_text() == key):
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
            out.write(key)
        fc_core.note_home_write(f, tmp)
        os.replace(tmp, f)  # parallel OpenCode turns never read a half-written key
    return f

def provider_of(model) -> tuple[str, str] | None:
    """(name, id) for `pi:<name>/<id>` or `<name>/<id>` when `name` is a declared provider, else None."""
    name, _, mid = model.removeprefix("pi:").partition("/")
    return (name, mid) if name in PROVIDERS and mid else None

def provider_env(name) -> str:
    """The env var apply_config exports provider `name`'s auth.json key as."""
    return re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_API_KEY"

WORKSPACE_ACTIONS = ("add", "key", "remove")

def read_key(name, kind="OpenRouter"):
    """The key from piped stdin (`pbpaste | compose workspace key personal`) or a hidden prompt; never an argument,
    so it stays out of shell history, the process list, and chat."""
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    import getpass
    return getpass.getpass(f"{kind} API key for {name} (empty to set later): ").strip()

def cmd_workspace(a):
    """`workspace` lists, `workspace <name>` switches, `workspace add|key|remove <name>` manages. `default` always exists:
    it is the config's top level, with no overrides."""
    cfg = read_config()
    spaces = cfg.setdefault("workspaces", {})
    action, name = (a.name, a.target) if a.name in WORKSPACE_ACTIONS else (None, a.name)
    if action and not name:
        sys.exit(f"usage: compose workspace {action} <name>")
    if name and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        sys.exit(f"workspace names are letters, digits, - and _: {name!r}")
    if name and name != "default" and action != "add" and name not in spaces:
        sys.exit(f"no workspace {name!r}; have: {', '.join(['default', *spaces])}. Create it: compose workspace add {name}")
    if action == "add":
        if name == "default" or name in WORKSPACE_ACTIONS or name in spaces:
            sys.exit(f"workspace {name!r} already exists" if name in spaces or name == "default" else f"{name!r} is a reserved word")
        key = read_key(name)
        spaces[name] = {}
        fc_auth.set_key(AUTH_PATH, name, "openrouter", key)  # empty fails loudly on use instead of billing another account
        print(f"added {name}" + ("" if key else f"; no key yet: pbpaste | compose workspace key {name}"))
    elif action == "key" and getattr(a, "provider", None):
        prov = a.provider
        if prov not in cfg.get("providers", {}) and prov not in spaces.get(name, {}).get("providers", {}):
            sys.exit(f"no provider {prov!r} in {CONFIG_PATH}; declare providers.{prov} with a base_url first")
        key = read_key(name, f"{prov} provider")
        if not key:
            sys.exit("no key read; copy it, then: pbpaste | compose workspace key " + name + f" --provider {prov}")
        fc_auth.set_key(AUTH_PATH, name, f"provider:{prov}", key)
        print(f"{name}: provider {prov} key set")
    elif action == "key":
        linear = getattr(a, "linear", False)
        ts = getattr(a, "typesafe", False)
        key = read_key(name, "TypeSafe" if ts else "Linear" if linear else "OpenRouter")
        if not key:
            sys.exit("no key read; copy it, then: pbpaste | compose workspace key " + name + (" --typesafe" if ts else " --linear" if linear else ""))
        fc_auth.set_key(AUTH_PATH, name, "typesafe" if ts else "linear" if linear else "openrouter", key)
        print("TypeSafe key set (every workspace uses it)" if ts else f"{name}: {'Linear' if linear else 'OpenRouter'} key set")
    elif action == "remove":
        if name == "default":
            sys.exit("default always exists; clear its key in the config's top level instead")
        del spaces[name]
        auth = fc_auth.read_auth(AUTH_PATH)
        auth.get("workspaces", {}).pop(name, None)
        fc_auth.write_auth(AUTH_PATH, auth)
        if cfg.get("workspace") == name:
            cfg["workspace"] = "default"
            print(f"removed the active workspace {name}; now on default")
    elif name:
        cfg["workspace"] = name
    if action or name:
        write_config(cfg)
    auth = fc_auth.read_auth(AUTH_PATH)
    held = auth.get("workspaces", {})
    dlin = held.get("default", {}).get("linear")
    current = cfg.get("workspace") or "default"
    for n in ["default", *spaces]:
        entry = held.get(n, {})
        key = entry.get("openrouter")
        state = "…" + key[-4:] if key else "not set" if "openrouter" in entry else "harness auth stores" if n == "default" else "inherits default"
        lin = "set" if entry.get("linear") else "inherits default" if n != "default" and dlin else "not set"
        print(f"{'*' if n == current else ' '} {n:16} openrouter key {state}, linear key {lin}")
    _, st = settings(cfg)
    m = st.get("models", {})
    print(f"implementer {m.get('implementer', DEFAULT_MODEL)}\nfallback    {m.get('fallback', FALLBACK_MODEL)} after {m.get('fallback_after', FALLBACK_AFTER)} failed attempts\n"
          f"test reader {m.get('test_reader', TEST_READER)}\nfix         {m.get('fix', '(implementer)')}")
    show = lambda e: (f"{e.get('model')}" + (f" ({e['effort']} effort)" if e.get("effort") else "")) if isinstance(e, dict) else str(e)
    for role, entry in (m.get("crew") or {}).items():
        fb = entry.get("fallback") if isinstance(entry, dict) else None
        fbs = fb if isinstance(fb, list) else [fb] if fb else []  # one fallback or an ordered list of them
        print(f"crew {role:14} {show(entry)}" + "".join(f", else {show(f)}" for f in fbs))
