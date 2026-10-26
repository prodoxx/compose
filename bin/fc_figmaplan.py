import json, re, sys, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import fc_steps


FIGMA_API = "https://api.figma.com/v1"

def figma_api(path, env_file=None, setup=""):
    """JSON from one Figma REST GET with the Framelink token (the library endpoints Framelink's CLI does not wrap); `setup` is the error when
    there is no token. Never echoes the token."""
    env_file = Path(env_file or fc_steps.FRAMELINK_ENV)
    token = ""
    if env_file.exists():
        token = next((v.strip().strip("\"'") for k, _, v in (l.partition("=") for l in env_file.read_text().splitlines()) if k.strip() == "FIGMA_API_KEY"), "")
    if not token:
        raise RuntimeError(setup or f"no FIGMA_API_KEY in {env_file}")
    name = path.split("?")[0]
    try:
        with urllib.request.urlopen(urllib.request.Request(FIGMA_API + path, headers={"X-Figma-Token": token}), timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"figma api {name} failed: HTTP {e.code}" + (" (the token needs the library_content:read scope)" if e.code == 403 else ""))
    except (OSError, ValueError) as e:
        raise RuntimeError(f"figma api {name} failed: {e}")

# What each plan tier gates that the design flow uses; None is no limit. A prediction: the probe (PROBE_STEPS) measures the real numbers.
PLAN_LIMITS = {"starter": {"pages": 3, "modes": 1, "library_publish": False, "code_connect": False},
               "professional": {"pages": None, "modes": 4, "library_publish": True, "code_connect": False},
               "organization": {"pages": None, "modes": 10, "library_publish": True, "code_connect": True},
               "enterprise": {"pages": None, "modes": 20, "library_publish": True, "code_connect": True}}
DESIGN_SYSTEM_MODES = 2  # the design system's semantic color has Light and Dark (0d)

def plan_tier(text):
    """The tier in `whoami`'s plan text (starter, pro, org, enterprise, or the long names), else None."""
    t = (text or "").lower()
    for tier, words in (("enterprise", "enterprise"), ("organization", "organization|org"), ("professional", "professional|pro"), ("starter", "starter|free")):
        if re.search(rf"\b({words})\b", t):
            return tier

def limit_value(text):
    """A --pages or --modes value: a count, or `unlimited` (None)."""
    if str(text).lower() in ("unlimited", "none", "-"):
        return None
    if not str(text).isdigit():
        sys.exit(f"not a count or `unlimited`: {text}")
    return int(text)

def figma_caps(plan, pages="table", modes="table"):
    """The record stored as `figma_caps`: the tier's predicted limits, each replaced by a probed value when one is given; `source` says which."""
    tier = plan_tier(plan)
    if not tier:
        sys.exit(f"unknown Figma plan {plan!r}: pass the tier whoami shows (starter, pro, org, enterprise)")
    lim = PLAN_LIMITS[tier]
    caps = {"plan": tier, "pages_per_file": lim["pages"], "modes_per_collection": lim["modes"],
            "library_publish": lim["library_publish"], "code_connect": lim["code_connect"], "source": "table"}
    for key, val in (("pages_per_file", pages), ("modes_per_collection", modes)):
        if val != "table":
            caps[key], caps["source"] = val, "probe"
    return caps

def caps_line(caps):
    """One line for the brief and the check output."""
    show = lambda v: "no limit" if v is None else v
    return (f"Figma capabilities ({caps['source']}): plan {caps['plan']}; pages per file {show(caps['pages_per_file'])}; modes per collection "
            f"{show(caps['modes_per_collection'])}; library publish {'yes' if caps['library_publish'] else 'no'}; Code Connect {'yes' if caps['code_connect'] else 'no'}")

def caps_blockers(caps, need_pages=None, need_modes=DESIGN_SYSTEM_MODES):
    """What the design needs that the stored capabilities do not allow, one line each."""
    out = []
    lim = caps.get("modes_per_collection")
    if need_modes and lim is not None and need_modes > lim:
        out.append(f"variable modes: the design system needs {need_modes} per collection{' (Light and Dark)' if need_modes == 2 else ''}, the {caps['plan']} plan allows {lim}")
    lim = caps.get("pages_per_file")
    if need_pages and lim is not None and need_pages > lim:
        out.append(f"pages: the design needs {need_pages} per file, the {caps['plan']} plan allows {lim}")
    return out

BLOCKED_QUESTION = ("BLOCKED by the Figma plan. Before any planning, raise ONE open question in needs-user with a recommendation: upgrade the plan, "
                    "or design around the limit (say what it costs: which tasks reword or merge). Rewording tasks around a limit before that answer is wasted if the plan is upgraded.")

def design_pages(tasks):
    """The distinct Figma pages the design tasks name in `nodes:` (<page>/<name>)."""
    return {n.split("/")[0] for m in tasks if m.get("kind") == "design" for n in m.get("nodes") or []}

def count_pages(text):
    """Pages in a Framelink depth-1 fetch of a file, else None."""
    try:
        nodes = json.loads(text).get("nodes")
    except (ValueError, AttributeError):
        return None
    return len([n for n in nodes or [] if isinstance(n, dict) and n.get("type") == "CANVAS"]) or None

# The one script that measures what the plan allows (pages up to 6, modes up to 5); the designer brief carries it verbatim.
PROBE_SCRIPT = """const out = {pages: 'unlimited', modes: 'unlimited'}; const made = [];
try { while (figma.root.children.length < 6) { const p = figma.createPage(); p.name = 'probe'; made.push(p); } } catch (e) { out.pages = figma.root.children.length; }
made.forEach(p => p.remove());
let col;
try { col = figma.variables.createVariableCollection('probe'); for (let i = 1; i < 5; i++) col.addMode('m' + i); } catch (e) { out.modes = col ? col.modes.length : 0; }
if (col) col.remove();
return out;"""
PROBE_STEPS = ("Capability preflight, once, before any plan, story, or task; never find a plan limit by a failing write.\n"
               "   a. `whoami`: the plan tier of the file's team.\n"
               "   b. `compose figma check --project {slug} --plan \"<that tier text>\" [--need-pages N] [--need-modes N]` stores the limits the tier predicts and prints BLOCKED when the design needs more "
               "(the design system needs 2 modes, Light and Dark, unless a recorded decision says otherwise: then pass the decided count, `--need-modes 1` for light only). A known tier with no BLOCKED: done.\n"
               "   c. BLOCKED, or an unknown tier: measure before raising anything. Load `figma:figma-create-new-file`, create a scratch file `compose-probe` with `create_new_file`, "
               "run this once with `use_figma` on it, then rerun b with its result added as `--pages <pages> --modes <modes>`:\n"
               "   " + PROBE_SCRIPT.replace("{", "{{").replace("}", "}}").replace("\n", "\n   ") + "\n"
               "   d. Still BLOCKED: the check prints the one open question to raise now (upgrade vs design around), before planning. After an upgrade, rerun b.\n"
               "   Plan inside the Figma capabilities line it leaves under the brief, and put that line in the feature's Linear Decisions.")
PUBLISH_STEP = ("Before you ask the product owner to publish a library, run `compose figma publish --project {slug} --library <library url or key> [--keys <component keys the consumer needs>]` "
                "and obey its first line: `publish: none` means nothing is pending, so do not ask (say \"nothing to publish\" and carry on); `publish: open` means an ask is already out, "
                "so add to it and do not ask again; only `publish: ask` may reach the product owner, once per review round, batched with that round's other questions. "
                "Never assert that a publish is pending or done without that line.")

PUBLISH_SLACK = timedelta(minutes=2)  # a publish and the file edit it carries land a moment apart

def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))

def publish_state(library, keys, api):
    """(verdict, reason, last publish time) for a library file, read through api(path). Pending when a needed component key is not
    published, nothing is published, or the file was edited after its last publish; else none."""
    pub = []
    for path in ("components", "component_sets", "styles"):
        pub += (api(f"/files/{library}/{path}").get("meta") or {}).get(path) or []
    have = {p["key"] for p in pub if p.get("key")}
    stamps = [parse_time(p["updated_at"]) for p in pub if p.get("updated_at")]
    last = max(stamps) if stamps else None
    edited = parse_time(api(f"/files/{library}?depth=1")["lastModified"])
    missing = [k for k in keys if k not in have]
    if missing:
        return "pending", f"{len(missing)} needed key(s) are not published: {', '.join(missing)}", last
    if last is None:
        return "pending", "nothing in the library is published yet", last
    if edited - last > PUBLISH_SLACK:
        return "pending", f"the library was edited at {edited:%Y-%m-%d %H:%M}Z, after its last publish at {last:%Y-%m-%d %H:%M}Z", last
    return "none", f"last published {last:%Y-%m-%d %H:%M}Z, library last edited {edited:%Y-%m-%d %H:%M}Z", last

def publish_ruling(data, library, keys, api, withdraw=False):
    """The text `figma publish` prints, whose first line is `publish: none | open | ask`, and linear.json's data with the one open ask
    recorded (`ask`) or cleared (`none`). An ask stays open until the publish shows in the library, so a second check says `open`,
    unless it is withdrawn: then the line reads `none` until a publish shows."""
    if withdraw:
        data.pop("figma_publish_ask", None)
        data["figma_publish_withdrawn"] = {"library": library, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        return f"publish: none\nthe ask is withdrawn: do not ask the product owner to publish library {library}", data
    caps = data.get("figma_caps") or {}
    if caps and not caps.get("library_publish", True):
        return (f"publish: none\nthe {caps['plan']} plan cannot publish a library: do not ask; keep the library and its consumers in one file, "
                "or raise the upgrade question"), data
    try:
        verdict, reason, last = publish_state(library, keys, api)
    except RuntimeError as e:
        return f"publish: unknown\n{e}\ndo not ask for a publish you cannot show is pending", data
    ask = data.get("figma_publish_ask") or {}
    if verdict == "none":
        data.pop("figma_publish_ask", None)
        data.pop("figma_publish_withdrawn", None)
        return f"publish: none\n{reason}: nothing to publish, so do not ask the product owner; import the library's components and carry on", data
    if (data.get("figma_publish_withdrawn") or {}).get("library") == library:
        return f"publish: none\n{reason}\nthe ask was withdrawn: do not ask the product owner to publish library {library}", data
    if ask.get("library") == library and (last is None or parse_time(ask["asked_at"]) > last):
        return f"publish: open\n{reason}\nthe ask went out at {ask['asked_at']} and the publish has not shown yet: do not ask again; add anything new to that ask", data
    data["figma_publish_ask"] = {"library": library, "asked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return f"publish: ask\n{reason}\nask the product owner once, in this review round's batch: publish library {library}, and name what changed", data

def capabilities_report(data, plan, pages, modes, pages_now, need_pages, need_modes=None):
    """The capability half of `figma check`: with a plan, store its limits in data (probed pages and modes replace the tier's predicted ones),
    then (lines to print, whether the design is blocked)."""
    if plan:
        data["figma_caps"] = figma_caps(plan, "table" if pages is None else limit_value(pages), "table" if modes is None else limit_value(modes))
    caps = data.get("figma_caps")
    if not caps:
        return ["capabilities not checked: run `whoami`, then compose figma check --project <slug> --plan \"<its plan tier>\" (the designer brief's step 3)"], False
    lines = [caps_line(caps) + (f"; the file has {pages_now} page(s) now" if pages_now else "")]
    blocked = caps_blockers(caps, need_pages, DESIGN_SYSTEM_MODES if need_modes is None else need_modes)
    if blocked:
        lines += [f"BLOCKED {b}" for b in blocked] + [BLOCKED_QUESTION + ("" if caps["source"] == "probe" else
                 " The limits are the tier's predicted ones: measure them with the probe (brief step 3c) before raising the question.")]
    return lines, bool(blocked)
