import contextlib, shlex, json, os, re, shutil, signal, subprocess, tempfile, threading, time
from pathlib import Path
from urllib.parse import urlsplit
import fc_core, fc_proc, fc_config, fc_models, fc_shrink


JEV_TIMEOUT = 150  # seconds one jev e2e case may run; a wandering jev used its whole 60-action budget in run 6

TOOL_ERROR_RE = re.compile(r"JEV_GAVE_UP|/jev_ultrafast/|/browser_harness/|command not found|address already in use|EADDRINUSE|"
                           r"No module named|Chromium not found|did not open the debug port", re.I)


def tool_error(output):
    """A failure the harness caused (a jev or browser crash, a missing tool, a port taken), not the app: retried once and
    never handed to a fixer, who could only 'fix' it by bending the app toward the harness."""
    return bool(TOOL_ERROR_RE.search(output or ""))

def stop_group(proc):
    """End a setup command and everything it started in its process group, even after the shell itself exited
    (its pid is the group id, so `getpgid` on a reaped leader is not needed)."""
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 0)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):  # macOS answers EPERM for a group left with only its zombie leader
            break
        deadline = time.time() + wait
        while time.time() < deadline:
            proc.poll()  # reap the leader so the group can empty
            try:
                os.killpg(proc.pid, 0)
            except (ProcessLookupError, PermissionError):
                # macOS answers EPERM while the leader is still exiting: reap it, so no zombie (or ResourceWarning) is left
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(1)
                return
            time.sleep(0.1)
    proc.poll()

def kill_listeners(ports):
    """Kill whatever listens on these ports: servers and browsers a setup detached from its process group (`&`, `bp launch`)."""
    for port in ports:
        pids = fc_core.sh(f"lsof -ti tcp:{int(port)} -sTCP:LISTEN", ".", check=False).stdout.split()
        if pids:
            fc_core.sh(["kill", "-9", *pids], ".", check=False)

def harness_daemons(name):
    """Pids of the browser-harness daemons running under BU_NAME=`name`. A daemon carries no name on its command line, only
    in its environment, and jev never sets BU_NAME, so the daemons its runs start inherit the stage's: matching the name
    finds exactly one stage's daemons, never another project's stage or the product owner's running at the same time."""
    found = set()
    for pid in fc_core.sh("pgrep -f browser_harness.daemon", ".", check=False).stdout.split():
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace").split("\0")
        except OSError:  # macOS: `ps -E` appends the environment to the command
            env = fc_core.sh(["ps", "-Eww", "-o", "command=", "-p", pid], ".", check=False).stdout.split()
        if f"BU_NAME={name}" in env:
            found.add(pid)
    return found

def end_new_daemons(name, before):
    """restart_daemon starts a fresh browser-harness daemon and jev's own runs leave theirs: end every daemon under the
    stage's BU_NAME that was not running before the stage, never one that was (a project may share one through e2e_env)."""
    new = harness_daemons(name) - before
    if new:
        fc_core.sh(["kill", *new], ".", check=False)
    return new

def e2e_ports(cfg, env):
    """The ports an e2e stage may leave a listener on: the fresh ones it hands out, every case URL's, and the CDP port of a
    browser the setup launches (BU_CDP_URL)."""
    urls = [c.get("url", "") for c in cfg.get("e2e", [])] + [env.get("BU_CDP_URL", "")]
    return {int(env["E2E_PORT"]), int(env["E2E_CLIENT_PORT"])} | {int(m) for u in urls for m in re.findall(r"://[^/:]+:(\d+)", u)}

def expand_ports(value, env):
    """`value` with $E2E_PORT and $E2E_CLIENT_PORT expanded in every string, inside dicts and lists too (a case's `env`)."""
    if isinstance(value, dict):
        return {k: expand_ports(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_ports(v, env) for v in value]
    return re.sub(r"\$\{?(E2E_PORT|E2E_CLIENT_PORT)\}?", lambda m: env[m.group(1)], value) if isinstance(value, str) else value

def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # another user's process
        pass
    return True

def reap_orphans():
    """End the browsers, and their harness daemons, of e2e stages whose verify is gone. A SIGKILL skips every cleanup, and the
    browser `bp launch` detached outlives it. A stage names its browser profile after its verify's pid (fc-e2e-browser-<pid>-),
    so a browser whose verify still runs, another project's included, is left alone. Returns the profiles it ended."""
    browsers = {}
    for line in fc_core.sh(["ps", "-axww", "-o", "pid=,command="], ".", check=False).stdout.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        m = re.search(r"--user-data-dir=(\S*/fc-e2e-browser-(\d+)-\S*)", cmd)
        if m and not alive(int(m.group(2))):
            port = re.search(r"--remote-debugging-port=(\d+)", cmd)
            pids, ports = browsers.setdefault(m.group(1), (set(), set()))
            pids.add(pid)
            ports |= {port.group(1)} if port else set()
    for profile, (pids, ports) in browsers.items():
        fc_core.sh(["kill", "-9", *pids], ".", check=False)
        for port in ports:  # the stage named its daemons after its browser's CDP port
            end_new_daemons(f"fc-e2e-{port}", set())
        shutil.rmtree(profile, ignore_errors=True)
    return sorted(browsers)

SETUP_DEADLINE = 60  # seconds a setup gets to bind the ports it names; a server slow to start under load is waited for, not raced

def await_setup(setup, env, wait):
    """Return once every fresh e2e port the setup names (by variable, or by number once expanded) accepts a connection, within
    SETUP_DEADLINE (or `wait`, if longer), so no case races a server still starting. A setup that names none (a browser on a
    pinned port) gets the fixed `wait`. A port still closed at the deadline is said, and left to the cases to fail on."""
    ports = [int(env[k]) for k in ("E2E_PORT", "E2E_CLIENT_PORT") if env.get(k) and re.search(rf"\b(?:{k}|{env[k]})\b", setup)]
    if not ports:
        return time.sleep(wait)
    deadline = time.monotonic() + max(wait, SETUP_DEADLINE)  # awake time: a machine asleep mid-wait suspends the server too
    while time.monotonic() < deadline:
        closed = [p for p in ports if not fc_core.port_listening(p)]
        if not closed:
            return
        time.sleep(0.1)
    fc_core.say(f"e2e    setup still not listening on {', '.join(map(str, closed))} after {fc_core.fmt(max(wait, SETUP_DEADLINE))}")

@contextlib.contextmanager
def case_setup(case, repo, env):
    """A case's own `setup` (for example a server, or a login, for this case alone), started and awaited before the case
    and ended after it, whatever happens; a SIGTERM or SIGHUP, which skips this cleanup, ends it through fc_proc.HOOKS."""
    if not case.get("setup"):
        yield
        return
    setup = subprocess.Popen(case["setup"], shell=True, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    hook = f"e2e setup {setup.pid}"
    fc_proc.HOOKS[hook] = lambda: stop_group(setup)
    try:
        await_setup(case["setup"], env, case.get("setup_wait", 3))
        yield
    finally:
        fc_proc.HOOKS.pop(hook, None)
        stop_group(setup)

def driver_e2e(case, repo):
    """Run the project's own browser driver command, with optional setup/teardown commands (e.g. start a local server)."""
    env = {**fc_core.clean_env(), **{k: str(v) for k, v in (case.get("env") or {}).items()}}
    with case_setup(case, repo, env):
        # In a group of its own that a SIGTERM or SIGHUP ends (fc_proc.RUNNING), like everything else the stage started.
        p = fc_proc.run(["/bin/sh", "-c", case["driver"]], cwd=repo, env=env, timeout=case.get("timeout", 600))
        out, err, code = p.stdout, p.stderr, p.returncode
        if not code and case.get("assert"):  # the independent check runs after every case, same env and cwd as the driver
            a = fc_proc.run(["/bin/sh", "-c", case["assert"]], cwd=repo, env=env, timeout=case.get("timeout", 600))
            out, err, code = out + a.stdout, err + a.stderr, a.returncode
    if p.returncode and re.search(r"\b402\b|payment required|insufficient credits", p.stdout + p.stderr, re.I):
        raise fc_models.PaymentRequired("jev e2e (TypeSafe or text model)", (p.stderr or p.stdout).strip()[-300:])
    r = {"name": case.get("name"), "exit": code, "stdout": fc_core.scrub(out)[-4000:], "stderr": fc_core.scrub(err)[-2000:]}
    m = re.search(r"^E2E_RESULT (\{.*\})$", out, re.M)
    if m:
        r["run"] = json.loads(m.group(1))
    return r

def run_held(cmd, limit, env, then):
    """jev run with --hold, as fc_implement.timed runs a command (under a machine slot, its group in fc_proc.RUNNING, killed
    at `limit`, its output scrubbed), calling then() while jev still holds the case's tab open: once it printed its
    JEV_RESULT, or ended or was killed without one. Closing jev's stdin then lets it close the tab.
    Returns jev's CompletedProcess (exit 124 at the limit) and what then() returned."""
    with fc_proc.slot(fc_core.say) as held:
        before = fc_core.home_state()
        p = subprocess.Popen(cmd, cwd=fc_config.JEV_DIR, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True, env={**env, "COMPOSE_TEST_SLOT": held})
        fc_proc.RUNNING.add(p.pid)
        out, err, result = [], [], threading.Event()

        def read_out():
            for line in p.stdout:
                out.append(line)
                if line.startswith("JEV_RESULT "):
                    result.set()
            result.set()
        readers = [threading.Thread(target=read_out), threading.Thread(target=lambda: err.extend(p.stderr))]
        for t in readers:
            t.start()
        killed = False
        try:
            if not result.wait(limit):
                fc_proc.kill_group(p.pid)
                killed = True
            checked = then()
        finally:
            p.stdin.close()
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                fc_proc.kill_group(p.pid)
            for t in readers:
                t.join()
            p.stdout.close(); p.stderr.close()
            fc_proc.RUNNING.discard(p.pid)
            fc_core.flag_home(before, f"`{cmd}`", sid=p.pid)
        return subprocess.CompletedProcess(cmd, 124 if killed else p.wait(), fc_core.scrub("".join(out)), fc_core.scrub("".join(err))), checked

def jev_e2e(case, repo):
    """Drive a real browser with jev (`python -m jev_ultrafast`, in its own uv project, jev/) to the case's goal, then run
    its independent assert after any run, one that gave up, crashed, or hit its limit too: an assert may also reset the app
    for the next case. The case's own `setup` runs around both. A case with a `driver` key runs that command instead (the
    project's own browser driver). jev types `secrets` ({"Password": "$E2E_PASSWORD"}) itself, from the env, and
    `record_dir` keeps a screenshot per step of a case that did not pass."""
    if case.get("driver"):
        return driver_e2e(case, repo)
    # case["env"] (verify.json e2e_env merged with the case's env) reaches jev: set BU_CDP_URL/BU_NAME there to point
    # browser-harness at a `bp`-launched Chromium that has the extension under test loaded.
    env = {**os.environ, **{k: str(v) for k, v in (case.get("env") or {}).items()}}
    # setup and assert are project code: no CLI keys, but the rest of ~/.compose/.env (a test user's password, say), which
    # jev reads through --env-file too. The process env wins over the file, as it does for uv.
    dotenv = {**fc_core.load_env(fc_config.ENV_FILE), **env}
    try:  # jev itself would stop on a bare KeyError: say why there is no key, as a harness failure no fixer gets
        fc_config.typesafe_key(dotenv, f"jev case {case.get('name')}")
    except RuntimeError as e:
        return {"name": case.get("name"), "exit": 1, "stdout": "", "stderr": f"JEV_GAVE_UP: {e}\n"}
    project_env = fc_core.clean_env(dotenv)
    for ref in (case.get("secrets") or {}).values():  # nothing verify prints or keeps shows one, not even an assert's output
        name = re.fullmatch(r"\$\{?(\w+)\}?", str(ref))
        if name and dotenv.get(name.group(1)):
            fc_core.SECRET_VALUES.add(dotenv[name.group(1)])
    limit = int(case.get("timeout") or JEV_TIMEOUT)
    record = case.get("record_dir")
    if record:
        shutil.rmtree(record, ignore_errors=True)  # a retry's or an earlier round's steps are not this run's
    env_file = ["--env-file", str(fc_config.ENV_FILE)] if fc_config.ENV_FILE.exists() else []
    flags = (["--secrets", json.dumps(case["secrets"])] if case.get("secrets") else []) + (["--record", str(record)] if record else [])
    cmd = shlex.join(["uv", "run", "--quiet", *env_file, "python", "-m", "jev_ultrafast", "--hold", *flags, "--", case["url"], case["goal"]])
    check = lambda: fc_proc.run(["/bin/sh", "-c", case["assert"]], cwd=repo, env=project_env, timeout=case.get("timeout", 600)) if case.get("assert") else None
    with case_setup(case, repo, project_env):
        p, a = run_held(cmd, limit, env, check)  # the assert reads the case's tab, so it runs before jev closes it
    # JEV_RESULT, its history included, is kept parsed in "run"; stdout keeps what jev and the assert printed besides.
    out = re.sub(r"^JEV_RESULT .*\n?", "", p.stdout, flags=re.M) + (fc_core.scrub(a.stdout) if a else "")
    r = {"name": case.get("name"), "exit": p.returncode, "stdout": out[-3000:], "stderr": (p.stderr + (fc_core.scrub(a.stderr) if a else ""))[-2000:]}
    m = re.search(r"^JEV_RESULT (\{.*\})$", p.stdout, re.M)
    if p.returncode == 124:  # killed at the limit: jev wandered or stuck, a harness failure, retried once, never fixed
        r["stderr"] = f"JEV_GAVE_UP: jev did not reach its goal within {limit}s\n" + r["stderr"]
    elif m:
        r["run"] = json.loads(m.group(1))
        if r["run"].get("status") != "done":
            why = r["run"].get("stop_reason")
            r["stderr"] = f"JEV_GAVE_UP: jev ended {r['run'].get('status')} after {r['run'].get('steps')} steps{': ' + why if why else ''}\n" + r["stderr"]
        u = r["run"]["usage"]
        prices = case.get("prices") or {}
        ts_cost = u["typesafe_calls"] * prices["typesafe_per_call"] if "typesafe_per_call" in prices else None
        fc_core.LEDGER.add("typesafe", u["typesafe_in"], u["typesafe_out"], ts_cost, calls=u["typesafe_calls"])
        fc_core.LEDGER.add("openrouter", u["text_in"], u["text_out"], None, calls=u["text_calls"])  # jev's text helper does not return $
    if a is not None:
        r["assert_exit"] = a.returncode
    # jev exits 0 only on a reached goal: then the assert decides; a run that did not reach it fails the case either way.
    code = a.returncode if a else 0
    r["exit"] = code if p.returncode == 0 else (code or p.returncode)
    if record and r["exit"] == 0 and not case.get("keep_record"):  # a prototype capture keeps its steps: they are the point
        shutil.rmtree(record, ignore_errors=True)
    elif record and Path(record).is_dir():
        with tempfile.TemporaryDirectory(dir=record) as tmp:
            for p in sorted(Path(record).glob("*.jpg")):
                small = fc_shrink.shrink(p, Path(tmp)).path
                if small != p:
                    os.replace(small, p)
        r["screenshots"] = str(record)
    return r

ASSERT_BROKE_RE = re.compile(r"Traceback \(most recent call last\)|ModuleNotFoundError|FileNotFoundError|No such file or directory|command not found|"
                             r"ECONNREFUSED|Connection refused|ENOENT|SyntaxError", re.I)
LOAD_FAIL_RE = re.compile(r"ERR_[A-Z_]+|net::|chrome-error:|refused to connect|can.t be reached|ECONNREFUSED", re.I)
LOGIN_PATH_RE = re.compile(r"/(?:log-?in|sign-?in|auth|sso|oauth)\b", re.I)

def path_of(url):
    return urlsplit(url or "").path

def env_failure(case, r):
    """(kind, why) when a failed case's own output shows the environment, not a missing feature, broke it: the page did not
    load, the user was bounced to a login page or a password could not be typed, the assert script itself crashed, or the
    harness did. None when the output alone cannot tell; a model then reads the steps (fc_prove.judge_failure)."""
    run = r.get("run") or {}
    why, url = str(run.get("stop_reason") or ""), str(run.get("url") or "")
    out = (r.get("stderr") or "") + (r.get("stdout") or "")
    if "skipped the password field" in why:
        return "login", why
    if r.get("assert_exit") not in (None, 0) and ASSERT_BROKE_RE.search(out):
        return "assert", "the assert script itself failed: " + " ".join(out.split())[-200:]
    if run.get("status") == "error":
        return "harness", why or "jev crashed"
    if not run and re.search(r"command not found|address already in use|EADDRINUSE|No module named|Chromium not found|did not open the debug port", out, re.I):
        return "harness", " ".join(out.split())[-200:]
    if LOAD_FAIL_RE.search(why + " " + url) or (run and url in ("", "about:blank") and run.get("steps", 0) == 0):
        return "page did not load", why or url or "the browser never left a blank page"
    if LOGIN_PATH_RE.search(path_of(url)) and not LOGIN_PATH_RE.search(path_of(case.get("url"))):
        return "login", f"the run ended on {url}, a login page the case did not start on"
    return None

def say_case(name, r, verdict):
    """A case's verdict line, then the end of its output; for a jev run that did not reach its goal, why it stopped, where,
    and its last steps, whole (results/verify.json keeps every step), and where its step screenshots are."""
    run = r.get("run")  # a driver case reports one only when it prints an E2E_RESULT line
    stopped = run and run.get("status") != "done" and run.get("stop_reason")
    fc_core.say(f"e2e    {verdict} {fc_core.fmt(r['seconds']):>8}  {name}" + (
        f"  steps={run.get('steps', run.get('decisions', '?'))} run={run.get('elapsed_ms', '?')}ms status={run.get('status', '?')}"
        + (f" assert={r['assert_exit']}" if r.get("assert_exit") is not None else "") + (f"  stop: {stopped}" if stopped else "") if run else ""))
    for line in (r.get("stdout") or "").splitlines()[-8:]:
        if line and not line.startswith(("E2E_RESULT", "JEV_RESULT")):
            print(f"           {line[:160]}")
    if run and run.get("status") != "done" and "history" in run:
        print(f"           at {run.get('url')}")
        for h in run["history"][-5:]:
            print(f"           step {h['step']:>2}  {h['kind']:<6} {h['label']}" + ("" if h.get("page_changed") else "  (page unchanged)"))
    if r.get("screenshots"):
        print(f"           screenshots {fc_core.tilde(r['screenshots'])}")
