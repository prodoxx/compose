"""Read-only machine checks the setup steps table and doctor use. No network, no writes."""
import datetime, json, os, re, shutil, subprocess, time, urllib.parse
from pathlib import Path
import platform
import fc_core
import fc_config
import fc_harness
import fc_worktree


def owned_links(checkout):
    """[(name, link, target)] for the three links compose owns (compose CLI link, compose skill link, bp link) plus, for each
    installed harness row in order, the compose skill link in that harness's skills folder."""
    checkout, home = Path(checkout), Path.home()
    links = [("compose CLI link", home / ".local/bin/compose", checkout / "bin/compose"),
             ("compose skill link", home / ".claude/skills/compose", checkout),
             ("bp link", home / ".local/bin/bp", checkout / "bp/bin/bp.mjs")]
    links += [(f"{fc_harness.HARNESSES[key]['name']} skill link", fc_harness.skills_dir(key) / "compose", checkout)
              for key in fc_harness.installed()]
    return links

def command_copies(key, checkout):
    """[(copy, source)] for one harness row: each <checkout>/commands/*.md sorted by name, copied to its skills folder as
    compose-<name>/SKILL.md (Codex loads a real SKILL.md, not a link); [] when the row's exe is not on PATH."""
    if not shutil.which(fc_harness.HARNESSES[key]["exe"]):
        return []
    return [(fc_harness.skills_dir(key) / f"compose-{c.stem}" / "SKILL.md", c)
            for c in sorted(Path(checkout).glob("commands/*.md"))]

def commands_ok(key, checkout):
    """(ok, detail) for the row's command copies: ok when every copy is a regular file holding its source's bytes, else the
    first bad copy, named as missing (not a regular file) or stale (different bytes)."""
    for copy, source in command_copies(key, checkout):
        if copy.is_symlink() or not copy.is_file():
            return False, f"{fc_core.tilde(copy)} is missing"
        if copy.read_bytes() != source.read_bytes():
            return False, f"{fc_core.tilde(copy)} is stale"
    return True, ""

def hook_records(key, checkout):
    """[(sub, trust key, hash)] for each handler of compose's wanted hook groups found in the row's hooks file; the key and hash
    are None for a group the file lacks."""
    row, out = fc_harness.HARNESSES[key], []
    path = fc_harness.home_dir(key) / row["hooks_file"]
    have = json.loads(path.read_text()).get("hooks", {}) if path.exists() else {}
    for ev, groups in fc_harness.hook_groups(key, checkout).items():
        for g in groups:
            sub = g["hooks"][0]["command"].rsplit('" ', 1)[1]
            i = next((j for j, e in enumerate(have.get(ev, ())) if e == g), None)
            if i is None:
                out.append((sub, None, None))
                continue
            snake = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", ev).lower()
            out += [(sub, f"{path.resolve()}:{snake}:{i}:{j}", row["hash"](ev, g)) for j in range(len(g["hooks"]))]
    return out

def hooks_ok(key, checkout):
    """(ok, detail): (True, "") for a row without hooks or without its exe on PATH; else the first hook the hooks file lacks or
    the first hook with no trust record (a wrong hash counts as none) in the trust file; a file that does not parse is named
    instead of raising (a hooks file that is not valid JSON, a trust file that is not valid TOML)."""
    row = fc_harness.HARNESSES[key]
    if not row.get("hooks") or not shutil.which(row["exe"]):
        return True, ""
    home = fc_harness.home_dir(key)
    try:
        recs = hook_records(key, checkout)
    except json.JSONDecodeError:
        return False, f"{fc_core.tilde(home / row['hooks_file'])} is not valid JSON"
    for sub, k, h in recs:
        if k is None:
            return False, f"{fc_core.tilde(home / row['hooks_file'])} lacks compose's {sub} hook"
    trust = home / row["trust_file"]
    try:
        state = trust_state(trust.read_text()) if trust.exists() else {}
    except ValueError:
        return False, f"{fc_core.tilde(trust)} is not valid TOML"
    for sub, k, h in recs:
        if state.get(k, {}).get("trusted_hash") != h:
            return False, f"{fc_core.tilde(trust)} has no trust record for compose's {sub} hook"
    return True, ""

def trust_state(text):
    """A trust file's [hooks.state."<key>"] tables as {key: {"trusted_hash": h}}. When import tomllib succeeds inside it,
    tomllib.loads(text).get("hooks", {}).get("state", {}) (invalid TOML raises, a ValueError); else, in the style of
    fc_init.herdr_toml: a [hooks.state.<json key>] header line gives its key (json.loads), any other [...] header ends it, and
    its trusted_hash = "<v>" line gives the hash (no such line: {}); never raises."""
    try:
        import tomllib
        return tomllib.loads(text).get("hooks", {}).get("state", {})
    except ImportError:
        pass
    state, cur = {}, None
    for line in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", line).strip()
        if m := re.match(r"\[hooks\.state\.(.*)\]$", line):
            try:
                cur = json.loads(m.group(1))
            except ValueError:
                cur = None
            if cur is not None:
                state[cur] = {}
        elif re.match(r"\[.*\]$", line):
            cur = None
        elif cur is not None and (m := re.match(r"trusted_hash[ \t]*=(.*)$", line)):
            state[cur] = {"trusted_hash": m.group(1).strip().strip("\"'")}
    return state

def link_state(path):
    """("ok", target) when path resolves to something that exists, ("dangling", raw link text), or ("missing", None)."""
    path = Path(path)
    if path.exists():
        return ("ok", str(path.resolve()))
    if path.is_symlink():
        return ("dangling", os.readlink(path))
    return ("missing", None)

def local_bin_on_path():
    """True when ~/.local/bin is one of the PATH entries."""
    return str(Path.home() / ".local/bin") in os.environ.get("PATH", "").split(os.pathsep)

def gh_authed():
    """True when `gh auth status` exits 0; False when it does not, hangs past 20 s, or gh is absent."""
    try:
        return fc_core.sh(["gh", "auth", "status"], Path.home(), check=False, timeout=20).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

def rerere_set():
    """True when `git config --global --get rerere.enabled` prints true."""
    try:
        return fc_core.sh(["git", "config", "--global", "--get", "rerere.enabled"], Path.home(), check=False).stdout.strip() == "true"
    except FileNotFoundError:
        return False

def jev_synced(checkout):
    """True when <checkout>/jev/.venv exists."""
    return (Path(checkout) / "jev/.venv").exists()

def chromium_installed():
    """True when the newest Playwright chromium-<n> has its Chrome for Testing binary (as bp/bin/bp.mjs:11-12 finds it)."""
    cache = Path.home() / "Library/Caches/ms-playwright"
    versions = sorted((d for d in os.listdir(cache) if re.fullmatch(r"chromium-\d+", d)), key=lambda d: -int(d[9:])) if cache.is_dir() else []
    if not versions:
        return False
    arch = "arm64" if platform.machine() == "arm64" else "x64"
    return (cache / versions[0] / f"chrome-mac-{arch}/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing").exists()

LOAD_FACTOR = 2  # the 5-minute load average that is "far above" the core count: this many times it
BATTERY_FLOOR = 10  # percent of charge at or under which a Mac on battery refuses to start work
CRASH_RESTARTS, CRASH_WINDOW = 3, 120  # a container restarted this many times, the last one under this many seconds ago, is in a crash loop

def battery_report():
    """What `pmset -g batt` prints; empty off macOS, without pmset, and when it does not answer within 10 s."""
    if not shutil.which("pmset"):
        return ""
    try:
        return fc_core.sh(["pmset", "-g", "batt"], None, timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""

def on_battery():
    """True when `pmset -g batt` says the Mac runs on its battery; False on AC power, off macOS, and without pmset."""
    return "Battery Power" in battery_report()

def battery_stop(force=False):
    """The line that stops a start while the Mac runs on a nearly empty battery (BATTERY_FLOOR or less), with why and how to
    override; None on AC power, with charge to spare, when pmset cannot say or names no charge, with `force`, and under
    COMPOSE_NO_PREFLIGHT=1. A charged battery starts: caffeinate holds the Mac awake, and only a closed lid sleeps it (a warning)."""
    if force or os.environ.get("COMPOSE_NO_PREFLIGHT"):
        return None
    out = battery_report()
    m = re.search(r"(\d+)%", out)
    if "Battery Power" not in out or not m or int(m[1]) > BATTERY_FLOOR:
        return None
    try:  # `compose watch` says `event power=ac` once the Mac is plugged in, so what stopped here starts again with no ask
        POWER_HELD.parent.mkdir(parents=True, exist_ok=True)
        fc_core.atomic_write(POWER_HELD, json.dumps({"at": time.time(), "charge": int(m[1])}))
    except OSError:
        pass
    return (f"the Mac is on battery at {m[1]}%: it can die before the work ends; plug it in, "
            "or run again with --force to start anyway; `compose watch` says `event power=ac` once it is plugged in")

POWER_HELD = fc_config.CONFIG_PATH.parent / "live" / "battery-held"  # the last battery_stop: {"at": epoch seconds, "charge": percent}
POWER_HELD_FOR = 6 * 3600  # seconds a battery stop stays worth announcing: one older than this was dealt with or forgotten

def power_back(seen):
    """(event line, stop time) once the Mac runs on AC again after a battery_stop newer than `seen`, the last stop this watcher
    announced; (None, seen) otherwise. pmset is asked only while such a stop is waiting."""
    try:
        at = float(json.loads(POWER_HELD.read_text())["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, seen
    if at <= (seen or 0) or time.time() - at > POWER_HELD_FOR or on_battery() or not battery_report():
        return None, seen
    return (f"event power=ac held={time.strftime('%H:%M:%S', time.localtime(at))}  (the Mac is plugged in: start again, with no ask, "
            "whatever a low battery stopped)"), at

def busiest_process():
    """(cpu percent, command name) of the process using the most CPU right now, else None."""
    try:
        out = fc_core.sh(["ps", "-Ao", "pcpu=,comm=", *(["-r"] if platform.system() == "Darwin" else ["--sort=-pcpu"])], None, timeout=10, check=False).stdout
        cpu, _, comm = out.strip().splitlines()[0].strip().partition(" ")
        return float(cpu), comm.strip().rsplit("/", 1)[-1]
    except (OSError, subprocess.TimeoutExpired, IndexError, ValueError):
        return None

def restarting_containers():
    """Names of the docker containers stuck in a crash loop (restarting now, or restarted CRASH_RESTARTS times and started under
    CRASH_WINDOW seconds ago). Empty when docker is not installed, not running, or does not answer within 10 s: never an error."""
    if not shutil.which("docker"):
        return []
    try:
        ids = fc_core.sh(["docker", "ps", "-q", "--filter", "status=running", "--filter", "status=restarting"], None, timeout=10, check=False)
        if ids.returncode or not ids.stdout.split():
            return []
        rows = fc_core.sh(["docker", "inspect", "--format", "{{.Name}}|{{.State.Status}}|{{.RestartCount}}|{{.State.StartedAt}}", *ids.stdout.split()],
                          None, timeout=10, check=False).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return []
    now, looping = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), []
    for row in rows:
        name, _, rest = row.partition("|")
        status, restarts, started = (rest.split("|") + ["", "0", ""])[:3]
        try:
            age = (now - datetime.datetime.strptime(started[:19], "%Y-%m-%dT%H:%M:%S")).total_seconds()
            recent = int(restarts) >= CRASH_RESTARTS and age < CRASH_WINDOW
        except ValueError:
            recent = False
        if status == "restarting" or recent:
            looping.append(name.lstrip("/"))
    return looping

def machine_warnings():
    """One WARNING line per thing that makes long work lie or stop, each with the fix: a Mac on battery (closing the lid sleeps
    it, which caffeinate cannot prevent), a 5-minute load average above LOAD_FACTOR times the cores (naming the busiest process),
    and docker containers in a crash loop (they burn the CPU a verify's timings and e2e cases need). Read-only; never raises."""
    out = []
    if on_battery():
        out.append("WARNING: the Mac is on battery: closing the lid will sleep it and stop the work, which keeping it awake cannot prevent; plug it in or leave the lid open")
    cores = os.cpu_count() or 1
    try:
        load = os.getloadavg()[1]
    except OSError:
        load = 0
    if load > LOAD_FACTOR * cores:
        top = busiest_process()
        out.append(f"WARNING: load average {load:.0f} on {cores} cores: checks and e2e run slow and time out"
                   + (f"; busiest process is {top[1]} at {top[0]:.0f}% CPU, stop it or wait" if top else ""))
    for name in restarting_containers():
        out.append(f"WARNING: docker container {name} keeps restarting and burns CPU, so a verify measures the machine, not the code: docker stop {name}")
    return out


# ---------- the startup checklist: power, docker, the database targets, sleep ----------
# Printed when a run, resume or verify starts, after the problems above are settled, so the user sees on one screen what the
# work runs on. The power and docker items stop a start when wrong; a database target is recorded when the project starts and
# a start stops when it changes, so no run writes a database the user did not confirm.
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")

def compose_files(wt):
    """The docker compose files at the work tree's root: what makes docker a need of the project rather than a bystander."""
    return [n for n in COMPOSE_FILES if (Path(wt) / n).exists()]

def docker_running():
    """None without docker; else whether its daemon answers `docker info` within 10 s."""
    if not shutil.which("docker"):
        return None
    try:
        return fc_core.sh(["docker", "info"], None, timeout=10, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False

def docker_names(*filters):
    """The names of the containers `docker ps` lists under `filters`; empty without docker or when it does not answer."""
    if not shutil.which("docker"):
        return []
    try:
        p = fc_core.sh(["docker", "ps", *(x for f in filters for x in ("--filter", f)), "--format", "{{.Names}}"], None, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return p.stdout.split() if p.returncode == 0 else []

def docker_problems(wt):
    """Problem lines with their fix, only where the project has a compose file: a daemon that is down, and containers whose
    own health check fails (what runs against them fails for the machine's reasons, not the code's)."""
    needs = compose_files(wt)
    if not needs:
        return []
    up = docker_running()
    if up is False:
        return [f"docker is not running and {needs[0]} needs it: start Docker Desktop, bring the services up, and run again"]
    return [f"docker container {n} is unhealthy, so what runs against it fails for the wrong reason: docker logs {n}, then fix or restart it"
            for n in (docker_names("health=unhealthy") if up else [])]

DB_SCHEMES = {"postgres", "postgresql", "mysql", "mysql2", "mariadb", "mongodb", "mongodb+srv", "redis", "rediss", "mssql", "sqlserver",
              "cockroachdb", "sqlite", "libsql", "file"}
ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")

def env_values(path):
    """{VAR: value} of a dotenv file: `export` and quotes dropped, a trailing ` # comment` of an unquoted value dropped."""
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return {}
    out = {}
    for l in lines:
        if m := ENV_LINE.match(l):
            v = m[2]
            out[m[1]] = v[1:-1] if len(v) > 1 and v[0] in "\"'" and v.endswith(v[0]) else v.split(" #")[0].strip()
    return out

def database_target(value):
    """`host[:port]/name` of a database URL (the file, for sqlite), never its credentials; None when the value is no database URL."""
    u = urllib.parse.urlsplit(value.strip())
    scheme = u.scheme.lower()
    if scheme not in DB_SCHEMES:
        return None
    if scheme in ("sqlite", "libsql", "file"):
        return (u.netloc + u.path) or value.strip()
    try:
        port = u.port
    except ValueError:
        port = None
    return f"{u.hostname or '?'}{f':{port}' if port else ''}{u.path.rstrip('/')}"

def database_targets(wt):
    """{"<env file> <VAR>": target} for every database URL the work tree's env files set (fc_worktree.env_files: the git-ignored
    .env and .env.* anywhere in the tree, so an app/ with a database of its own is a line of its own): what the work reads and writes."""
    out = {}
    for f in fc_worktree.env_files(wt):
        for var, value in env_values(Path(wt) / f).items():
            if (t := database_target(value)) is not None:
                out[f"{f} {var}"] = t
    return out

def database_problems(now, recorded):
    """Problem lines for a database target that is not the one recorded when the project started (`database` in verify.json),
    or that was not there then: a run that would touch a database the user did not confirm stops here, with how to confirm it."""
    out = []
    for key, target in now.items():
        was = recorded.get(key)
        if was is None:
            out.append(f"database target not confirmed: {key} points at {target}, which the project did not start with; confirm it with "
                       f"the user, then add {{\"{key}\": \"{target}\"}} to `database` in verify.json (or take it out of the env file)")
        elif was != target:
            out.append(f"database target changed: {key} points at {target}, the project started on {was}; confirm the change with the user, "
                       f"then set `database` {{\"{key}\": \"{target}\"}} in verify.json (or restore the env file)")
    return out

def power_line():
    out = battery_report()
    if not out:
        return "power: unknown (no pmset here)"
    charge = f" {m[1]}%" if (m := re.search(r"(\d+)%", out)) else ""
    return f"power: battery{charge}" if "Battery Power" in out else f"power: AC{charge}"

def docker_line(wt):
    up, needs = docker_running(), compose_files(wt)
    need = f" ({needs[0]} is here)" if needs else ""
    if up is None:
        return f"docker: not installed{need}"
    if not up:
        return f"docker: not running{need}"
    names, bad = docker_names(), docker_names("health=unhealthy")
    return f"docker: {len(names)} container{'' if len(names) == 1 else 's'} up" + (f", unhealthy: {', '.join(bad)}" if bad else ", none unhealthy")

def awake_line():
    if os.environ.get("COMPOSE_NO_AWAKE"):
        return "sleep: COMPOSE_NO_AWAKE is set, so the Mac may sleep during the work"
    if platform.system() != "Darwin" or not shutil.which("caffeinate"):
        return "sleep: no caffeinate here; keep the machine from sleeping yourself"
    return "sleep: caffeinate holds the Mac awake for as long as this command lives (a closed lid on battery still sleeps it)"

def startup_lines(wt, targets):
    """The checklist a start prints, one `startup  <item>: <state>` line each: power, docker, every database target, sleep."""
    db = [f"database: {k} -> {v}" for k, v in targets.items()] or ["database: no database URL in the env files"]
    return [f"startup  {l}" for l in (power_line(), docker_line(wt), *db, awake_line())]
