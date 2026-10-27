import base64, json, os, re, subprocess, sys, time, urllib.error, urllib.request, uuid
from pathlib import Path
import fc_core, fc_config, fc_claude, fc_harness, fc_proc, fc_readguard, fc_providers, fc_tiers, fc_notify


def opencode(cwd, model, prompt, session=None, timeout=600):
    """One OpenCode turn. Returns (session, reply_text, tool_calls, usage). Usage comes from step-finish events."""
    # --dir is required: with a git worktree, opencode otherwise resolves the project root to the main checkout and edits it.
    cmd = ["opencode", "run", "--format", "json", "--dir", str(cwd), "--model", model] + (["--session", session] if session else []) + [prompt]
    # stdin must be closed: opencode run reads a piped stdin until EOF and hangs on an open one.
    # OPENCODE_DISABLE_CLAUDE_CODE_SKILLS: its scan of ~/.claude/skills follows a symlink to a checkout holding .treehouse/ worktrees until ENAMETOOLONG, and the run never ends.
    env, key = {**fc_core.clean_env(), "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1"}, fc_config.openrouter_key()
    config = {}
    if key:  # the key reaches OpenCode as a file its config names, so the commands it runs never see it
        config["provider"] = {"openrouter": {"options": {"apiKey": f"{{file:{fc_config.key_file(key)}}}"}}}
    if local := fc_providers.opencode_provider(model):
        config["provider"] = {**config.get("provider", {}), **local}
    guard = fc_readguard.guard_file(cwd)  # the plugin asks the guard before each tool call; bash stays fenced to trail
    if guard.exists():
        config["plugin"] = [f"file://{fc_readguard.OC_PLUGIN}"]
        config["permission"] = {"external_directory": "deny", "bash": {"*": "deny", "trail ask *": "allow", "trail callers *": "allow"}}
        env["COMPOSE_READGUARD"] = fc_readguard.command(guard)
    if config:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    for attempt in range(4):
        p = fc_proc.run(cmd, cwd=cwd, env=env, timeout=timeout)
        # Parallel launches race on opencode's shared SQLite file. Back off and retry; nothing has run yet when this happens.
        if p.returncode and "database is locked" in (p.stdout + p.stderr) and attempt < 3:
            time.sleep(0.7 * (attempt + 1) + 0.3 * (hash(str(cwd)) % 5) / 5)
            continue
        break
    sid, text, tools, usage, errors = session, [], 0, {"tokens_in": 0, "tokens_out": 0, "cost": 0.0}, []
    for line in p.stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = sid or ev.get("sessionID")
        part = ev.get("part", {})
        if ev.get("type") == "error":
            err = ev.get("error") or {}
            data = err.get("data") or {}
            payment_check("cerebras/groq via OpenRouter (OpenCode)", data.get("message") or json.dumps(err), data.get("statusCode"))
            errors.append(str(data.get("message") or err.get("name") or err))
        elif ev.get("type") == "text":
            text.append(part.get("text", ""))
        elif ev.get("type") == "tool_use":
            tools += 1
        elif ev.get("type") == "step_finish":
            t = part.get("tokens", {})
            usage["tokens_in"] += t.get("input", 0) + t.get("cache", {}).get("read", 0) + t.get("cache", {}).get("write", 0)
            usage["tokens_out"] += t.get("output", 0) + t.get("reasoning", 0)
            usage["cost"] += part.get("cost") or 0.0
    if p.returncode and not sid:
        raise RuntimeError(f"opencode failed: {p.stderr[-2000:]}")
    if errors and not text and not tools:
        # The model never ran: the provider refused (bad key, payment required, model missing). Retrying is pointless.
        raise RuntimeError(f"provider refused the request: {'; '.join(errors)[-400:]}")
    usage["cost"] = fc_providers.cost(model, usage)
    fc_core.LEDGER.add(model.split("/")[0], usage["tokens_in"], usage["tokens_out"], usage["cost"])
    reply = "\n".join(text)
    # A provider 429 makes opencode compact the session into a "Work State / Next Move" summary and stop.
    # That session is unusable afterwards; the caller must wait for the window to reset and start a fresh one.
    if RATE_LIMIT_MARK in reply or "Tokens per minute" in (p.stderr or ""):
        raise RateLimited(usage)
    return sid, reply, tools, usage

RATE_LIMIT_MARK = "exceeded the provider's size limit"

def pi_error(m):
    """The provider's own error text from a pi assistant message that ended with stopReason=error, else None. A missing preset
    is named with the active workspace, since the preset lives in that workspace's OpenRouter account."""
    if m.get("stopReason") != "error":
        return None
    err = str(m.get("errorMessage") or "provider error")
    try:
        meta = json.loads(err.split(": ", 1)[1])["metadata"]
    except (IndexError, KeyError, TypeError, ValueError):
        meta = {}
    if meta.get("presetErrorCode") == "preset_not_found":
        err += f" (preset '{meta.get('presetSlug')}' not found in workspace '{fc_config.WORKSPACE}')"
    return err

PI_AGENT_DIR = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")  # read once, at import
PI_COMPACTION = {"enabled": True, "reserveTokens": 16384, "keepRecentTokens": 20000}  # pi's own defaults

def pi_agent_dir(cwd):
    """The agent directory a task's pi runs with, beside the task clone as <clone>.pi-agent: the user's models.json and auth.json
    by link, and a settings.json of its own that pins pi's default compaction. pi merges nothing from the user's global
    settings then: a large compaction.reserveTokens there (meant for a small local model) made GLM's turns compact every few
    tool calls until the turn limit."""
    adir = Path(cwd).parent / (Path(cwd).name + ".pi-agent")
    adir.mkdir(exist_ok=True)
    for name in ("models.json", "auth.json"):
        (adir / name).unlink(missing_ok=True)
        if (PI_AGENT_DIR / name).exists():
            (adir / name).symlink_to(PI_AGENT_DIR / name)
    (adir / "settings.json").write_text(json.dumps({"compaction": PI_COMPACTION}))
    return str(adir)

def pi_turn(cwd, model, prompt, session=None, timeout=None):
    timeout = timeout or TURN_TIMEOUT
    """One turn through pi (github.com/earendil-works/pi), the lean harness: ~1K tokens of fixed context per turn against
    OpenCode's ~12K. Same contract as opencode(): (session, reply_text, tool_calls, usage). The session lives outside the
    task clone so `git add -A` never commits it. Model ids are pi's `provider/model`, e.g. openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast."""
    sdir = Path(cwd).parent / (Path(cwd).name + ".pi-sessions")
    sdir.mkdir(exist_ok=True)
    import uuid
    sid = session or str(uuid.uuid4())  # --session-id must be a UUID
    env = fc_core.clean_env()  # no secret but the key pi bills to: pi's tools, the task's test among them, inherit its env
    env["PI_CODING_AGENT_DIR"] = pi_agent_dir(cwd)
    if "OPENROUTER_API_KEY" in os.environ:  # pi's custom providers read the key from env
        env["OPENROUTER_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
    guard = fc_readguard.guard_file(cwd)  # the extension asks the guard before each tool call
    cmd = ["pi", "-p", "--mode", "json", "--session-id", sid, "--session-dir", str(sdir), "--no-extensions", "--no-skills",
           "--no-prompt-templates", "--thinking", "low", *fc_providers.pi_args(model), "--model", model]
    env.update(fc_providers.pi_env(model))
    if guard.exists():  # -e loads it despite --no-extensions; COMPOSE_READGUARD tells it which shell line runs the guard
        cmd += ["-e", str(fc_readguard.PI_EXT)]
        env["COMPOSE_READGUARD"] = fc_readguard.command(guard)
    cmd.append(prompt)
    p = fc_proc.run(cmd, cwd=cwd, env=env, timeout=timeout)
    text, tools, usage, errors, last = [], 0, {"tokens_in": 0, "tokens_out": 0, "cost": 0.0}, [], None
    for line in p.stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "tool_execution_start":
            tools += 1
        elif t == "message_update" and ev.get("usage"):
            last = ev["usage"]  # cumulative within the current assistant message
        elif t == "message_end" and (ev.get("message") or {}).get("role") == "assistant":
            for part in ev["message"].get("content") or []:
                if part.get("type") == "text":
                    text.append(part.get("text", ""))
            if err := pi_error(ev["message"]):
                payment_check("openrouter (pi)", err)
                errors.append(err[:400])
            if last:
                usage["tokens_in"] += last.get("input", 0) + last.get("cacheRead", 0) + last.get("cacheWrite", 0)
                usage["tokens_out"] += last.get("output", 0) + last.get("reasoning", 0)
                usage["cost"] += (last.get("cost") or {}).get("total") or 0.0
                last = None
        elif t == "error" or ev.get("error"):
            payment_check("openrouter (pi)", json.dumps(ev.get("error") or ev))
            errors.append(str(ev.get("error") or ev)[:300])
    if p.returncode and not text and not tools:
        real = [l for l in (p.stderr or "").splitlines() if l.strip() and not l.startswith("Warning:")]
        payment_check("openrouter (pi)", " ".join(real))
        raise RuntimeError(f"pi failed: {' '.join(real)[-400:] or p.stdout.strip()[-400:]}")
    busy = provider_busy(errors, not text and not tools and not usage["tokens_in"], p.stderr or "")
    if not text and not tools and not usage["tokens_in"] and not busy:  # a 429 or a 503 refuses before any token, and passes
        real = [l for l in (p.stderr or "").splitlines() if l.strip() and not l.startswith("Warning:")]  # pi's session warnings are not the provider's answer
        raise RuntimeError(f"provider refused the request: {'; '.join(errors) or ' '.join(real)[-400:] or 'empty reply with no usage'}")
    usage["cost"] = fc_providers.cost(model, usage)
    fc_core.LEDGER.add("openrouter" if "openrouter" in model else model.split("/")[0], usage["tokens_in"], usage["tokens_out"], usage["cost"])
    if busy:
        raise RateLimited(usage, busy)
    return sid, "\n".join(text), tools, usage

def fallback_for(model):
    """The model to switch to after FALLBACK_AFTER failed attempts or a rate limit, or None when there is nothing to fall back to (a tiered model has only its escalation; otherwise the fallback itself, Claude, or none set)."""
    if fc_tiers.tier_of(model) is None:
        return fc_config.FALLBACK_MODEL if fc_config.FALLBACK_MODEL and model != fc_config.FALLBACK_MODEL and not fc_claude.is_claude(model) else None
    return fc_tiers.escalate(model)


class PaymentRequired(Exception):
    """A provider refused for billing reasons. Nothing retries this: the run halts, the state is saved, the user tops up, `resume` continues."""
    def __init__(self, provider, message):
        super().__init__(f"{provider}: {message}")
        self.provider, self.message = provider, message

PAY_RE = re.compile(r"payment required|insufficient[ _](credits?|quota|funds|balance)|credit balance|out of credits?|exceeded your current quota|"
                    r"billing|\b402\b|account (is )?(suspended|disabled|past due)|top up", re.I)
TOP_UP = {"openrouter": "https://openrouter.ai/settings/credits", "cerebras": "https://cloud.cerebras.ai (Billing)",
          "typesafe": "https://typesafe.ai dashboard (Billing)", "groq": "https://console.groq.com (Billing)"}

def payment_check(provider, text, status=None):
    """Raise PaymentRequired when an HTTP status or an error message says the account cannot pay."""
    if status == 402 or (text and PAY_RE.search(str(text))):
        raise PaymentRequired(provider, str(text)[:300].strip() or f"HTTP {status}")

def http_json(req, provider, timeout=90):
    """urlopen that turns a 402 (or a 200 carrying a billing error) into PaymentRequired instead of a stack trace."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        e.close()
        payment_check(provider, body, e.code)
        raise RuntimeError(f"{provider}: HTTP {e.code} {e.reason}: {body.strip()}") from e
    err = out.get("error") if isinstance(out, dict) else None
    if err:
        payment_check(provider, json.dumps(err), (err.get("code") if isinstance(err, dict) else None))
    return out

class RateLimited(Exception):
    """The provider turned the turn away for a reason that passes: `run` waits and starts a fresh session without spending an attempt."""
    def __init__(self, usage, why="rate-limited by provider"):
        super().__init__(f"{why}; session compacted")
        self.usage, self.why = usage, why


def provider_busy(errors, empty, stderr=""):
    """Why the provider turned a pi turn away for a reason that passes, else None: a rate limit at any point, or, on a turn that got no
    token at all, an overloaded, unavailable or unreachable upstream (a 429 or a 5xx, a connect error). A refusal that stays, such as
    a bad model id, a missing preset or a bad key, matches neither, and a billing refusal raised PaymentRequired before this."""
    if "429" in stderr or any("rate" in e.lower() and "limit" in e.lower() for e in errors):
        return "rate-limited by provider"
    hit = next((e for e in errors if TRANSIENT.search(e)), None) if empty else None
    return f"provider busy: {' '.join(hit.split())[:200]}" if hit else None


def parse_pi_session(path, seen):
    """Read the pi session JSONL from line `seen` on: assistant text, tool calls, usage, and provider errors of the new turns.
    Same accounting as pi_turn, so the ledger and cost line do not depend on which path ran the task."""
    text, tools, usage, errors = [], 0, {"tokens_in": 0, "tokens_out": 0, "cost": 0.0}, []
    lines = Path(path).read_text().splitlines() if path and Path(path).is_file() else []
    for ln in lines[seen:]:
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        m = ev.get("message") or {}
        if ev.get("type") != "message" or m.get("role") != "assistant":
            continue
        for part in m.get("content") or []:
            if part.get("type") == "text":
                text.append(part.get("text", ""))
            elif part.get("type") == "toolCall":
                tools += 1
        u = m.get("usage") or {}
        usage["tokens_in"] += u.get("input", 0) + u.get("cacheRead", 0) + u.get("cacheWrite", 0)
        usage["tokens_out"] += u.get("output", 0) + u.get("reasoning", 0)
        usage["cost"] += (u.get("cost") or {}).get("total") or 0.0
        if err := pi_error(m):
            payment_check("openrouter (pi)", err)
            errors.append(err[:400])
    return len(lines), "\n".join(text), tools, usage, errors

def pi_turn_state(path, seen):
    """(delivered, ended) for the turn a pi session file records from line `seen` on: pi wrote the prompt down, and its last message
    is the assistant's and did not stop for a tool call. The record of a turn herdr never saw working."""
    roles = []
    for ln in (Path(path).read_text().splitlines()[seen:] if path and Path(path).is_file() else []):
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "message":
            m = ev.get("message") or {}
            roles.append((m.get("role"), m.get("stopReason")))
    delivered = any(r == "user" for r, _ in roles)
    return delivered, delivered and roles[-1][0] == "assistant" and roles[-1][1] != "toolUse"


_PROBED = {}

def pi_id(model):
    """The --model value for a `pi:<provider/model>` id."""
    return model.split(":", 1)[1]

def probe_command(model):
    if fc_harness.harness_of(model):
        return fc_harness.probe_command(model)
    if model.startswith("pi:"):
        return ["pi", "-p", "--mode", "json", "--no-session", "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files",
                "--thinking", "off", *fc_providers.pi_args(pi_id(model)), "--model", pi_id(model), "Reply with exactly: ok"]
    return ["claude", "-p", "--model", fc_claude.claude_id(model), "--output-format", "json", "Reply with exactly: ok"]

def probe_result(model, p):
    """(ok, why) from a finished probe process: Claude Code reports `is_error` in its JSON; pi streams events, so a clean exit
    plus an assistant text reply is ok and anything else is the error it printed."""
    if fc_harness.harness_of(model):
        return fc_harness.probe_result(model, p)
    if model.startswith("pi:"):
        events = []
        for line in p.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        done = [e["message"] for e in events if e.get("type") == "message_end" and (e.get("message") or {}).get("role") == "assistant"]
        failed = next(filter(None, map(pi_error, done)), None)  # a refused model still ends its turn
        replied = any(c.get("type") == "text" and c.get("text") for m in done for c in m.get("content") or [])
        if p.returncode == 0 and replied and not failed:
            return True, "answered"
        return False, (failed or p.stderr or p.stdout or f"exit {p.returncode}").strip()
    out = json.loads(p.stdout.strip()) if p.stdout.strip().startswith("{") else {}
    ok = p.returncode == 0 and out.get("is_error") is False
    return ok, "answered" if ok else (out.get("result") or p.stderr or p.stdout or f"exit {p.returncode}").strip()

def model_available(model):
    """Whether the harness named by `model` (a claude:<id> or pi:<provider/model>) can run it on this account: one tiny
    non-interactive turn, cached for the day in ~/.compose/model-probe.json. Returns (ok, why)."""
    mid, today = model.split(":", 1)[1] if ":" in model else "", time.strftime("%Y-%m-%d")
    if not mid:
        key = fc_harness.harness_of(model)
        if key:
            return True, f"{fc_harness.HARNESSES[key]['name']}'s default model"
        return True, "Claude Code's default model"
    local = fc_config.provider_of(model)
    if local:  # a local provider must be answering before anything else: no probe turn, no cache, when it is down
        ok, why = fc_providers.reach(local[0])
        if not ok:
            return False, why
    if mid in _PROBED:
        return _PROBED[mid]
    try:
        cache = json.loads(fc_config.PROBE_CACHE.read_text()) if fc_config.PROBE_CACHE.exists() else {}
    except (OSError, json.JSONDecodeError):
        cache = {}
    hit = cache.get(mid)
    if hit and hit.get("date") == today and hit.get("ok"):  # only a pass is trusted: an old failing entry is probed again
        _PROBED[mid] = (hit["ok"], hit["why"])
        return _PROBED[mid]
    ok, why, transient = probe_once(model, 60)
    if transient and not local:  # a slow start-up or a busy API says nothing about the account: retry once, longer
        ok, why, transient = probe_once(model, 180)
    if ok:  # only a pass is cached: a failure is returned whole and the next call probes again, so a fixed key or preset works at once
        _PROBED[mid] = (ok, why)
        cache[mid] = {"date": today, "ok": ok, "why": why}
        fc_config.PROBE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        fc_core.atomic_write(fc_config.PROBE_CACHE, json.dumps(cache, indent=2))
    return ok, why

TRANSIENT = re.compile(r"overloaded|rate.?limit|too many requests|\b(5\d\d|429)\b|timed? ?out|temporar|try again|unavailable|connection|ECONN", re.I)

def probe_once(model, timeout):
    """(ok, why, transient) from one probe turn. Only a clear answer (ok, or a refusal such as an unknown model) is definite."""
    try:
        env = fc_claude.headless_env()
        if model.startswith("pi:") and "OPENROUTER_API_KEY" in os.environ:  # pi's custom providers read the key from env, never argv
            env["OPENROUTER_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
        if model.startswith("pi:"):
            env.update(fc_providers.pi_env(pi_id(model)))
        p = subprocess.run(probe_command(model), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
        ok, why = probe_result(model, p)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return False, f"{type(e).__name__}: {e}", True
    return ok, why, not ok and bool(TRANSIENT.search(why))

def probe_clear():
    """Forget the day's cached probe results so the next pick asks again."""
    fc_config.PROBE_CACHE.unlink(missing_ok=True)
    _PROBED.clear()

def cmd_models(a):
    """`models --probe`: clear the cached probe results and re-probe every model the crew chains name."""
    if not a.probe:
        sys.exit("models --probe clears the day's cached model probes and probes the crew models again")
    probe_clear()
    for model in dict.fromkeys(m for chain in [[(m, None)] for m in fc_config.CREW_MODELS.values()] + list(fc_config.CREW_FALLBACK.values()) for m, _ in chain):
        if fc_claude.is_claude(model) or str(model).startswith("pi:") or fc_harness.harness_of(model):
            ok, why = model_available(model)
            print(f"{model}: {'ok' if ok else 'unavailable'} ({why})")


TEST_TIMEOUT = 120  # seconds a task's test may run before it counts as hung; a task's block can set `test_timeout:`
TURN_TIMEOUT = 300  # seconds one implementer turn may take; a Sonnet turn of 10 to 18 tool calls and a GLM turn that compacts ran past 180 s, and the task's test runs after the turn, not in it


def payment_halt(repo, proj, command, remaining, args, err):
    """Save what is left to do, tell the user where to top up, exit 3. `compose resume` picks it up. The saved state carries the top-up
    page and the exact resume command too, so `watch` can put both in its `halt=payment` event."""
    key = next((k for k in TOP_UP if k in err.provider.lower()), None)
    resume = f"compose resume --repo {repo} --project {proj.name}"
    state = {"command": command, "remaining": remaining, "args": args, "provider": err.provider, "message": err.message,
             "halted_at": time.strftime("%Y-%m-%d %H:%M:%S"), "top_up": TOP_UP.get(key), "resume": resume}
    (proj / "results").mkdir(exist_ok=True)
    fc_core.atomic_write((proj / "results" / "resume.json"), json.dumps(state, indent=2))
    (proj / "results" / "run.json").unlink(missing_ok=True)
    fc_notify.send(proj, f"payment required at {err.provider}: top up, then compose resume ({len(remaining)} task(s) left)")
    print()
    print(f"PAYMENT REQUIRED  provider: {err.provider}")
    print(f"  {err.message}")
    if key:
        print(f"  top up at: {TOP_UP[key]}")
    if command == "run":
        print(f"  stopped with {len(remaining)} task(s) left: {', '.join(remaining)}. Passed tasks keep their branches.")
    else:
        print("  stopped during verify; task branches are untouched.")
    print(f"  then continue where it stopped:  {resume}")
    sys.exit(3)


def lenient_json(content, key=None):
    """The first JSON value in a reply, whatever prose or fence surrounds it. With `key`, the first object holding it: a
    `{}` quoted in the prose before the answer is not the answer."""
    content = content.strip()
    starts = [m.start() for m in re.finditer(r"\{", content)] or [0]
    first = None
    for at in starts:
        try:
            v = json.JSONDecoder().raw_decode(content[at:])[0]  # small models append prose
        except json.JSONDecodeError:
            continue
        if key is None or isinstance(v, dict) and key in v:
            return v
        first = v if first is None else first
    if first is not None:
        return first
    # Common small-model slip: single-quoted JSON.
    return json.JSONDecoder().raw_decode(re.sub(r"(?<![\\\\])'", '"', content[starts[0]:]))[0]

def fast_env():
    """The env a fast-model call reads: ~/.compose/.env, overridden by the process env (the workspace key)."""
    return {**fc_core.load_env(fc_config.ENV_FILE), **os.environ}

def fast_json(env, system, user, max_tokens=1500, timeout=90, key=None):
    """One call to the fast text model in ~/.compose/.env (Mercury 2.5 by default) through OpenRouter, cost exact, or to Claude Code
    when TEXT_MODEL is a `claude:<id>` (models.review sets it for the review). `key` names the field the caller needs, so the object
    holding it is the one read."""
    if fc_claude.is_claude(env.get("TEXT_MODEL", "")):  # no token cap to cut it off; a whole diff takes longer than a fast model
        return parse_reply(fc_claude.claude_json(fc_core.scrub(system), fc_core.scrub(user), env["TEXT_MODEL"], timeout=max(timeout, 300)), False, max_tokens, key)
    body = {"model": env.get("TEXT_MODEL", "inception/mercury-2.5"), "max_tokens": max_tokens, "temperature": 0,  # the same code gets the same review
            "response_format": {"type": "json_object"}, "reasoning": {"enabled": False}, "usage": {"include": True},
            "messages": [{"role": "system", "content": fc_core.scrub(system)}, {"role": "user", "content": fc_core.scrub(user)}]}
    req = urllib.request.Request(env.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {env['TEXT_MODEL_API_KEY']}", "Content-Type": "application/json"})
    out = http_json(req, "openrouter (fast model)", timeout=timeout)
    u = out.get("usage", {})
    fc_core.LEDGER.add("openrouter", u.get("prompt_tokens", 0), u.get("completion_tokens", 0), u.get("cost"))
    choice = (out.get("choices") or [{}])[0]
    return parse_reply((choice.get("message") or {}).get("content"), choice.get("finish_reason") == "length", max_tokens, key)

def parse_reply(content, cut, max_tokens, key):
    """The JSON object in a fast-model reply, as fast_json returns it; `cut` says the reply ran out of tokens."""
    try:
        if not content or not str(content).strip():
            raise EmptyReply("model returned an empty reply")
        return lenient_json(str(content), key)
    except (EmptyReply, ValueError):
        if cut:  # the budget ran out before the object closed; a whole object followed by cut-off prose still parses above
            raise CutOff(f"reply cut off at max_tokens={max_tokens}")
        raise

CUT_RETRY = 4  # a reply cut off at its budget is asked again with this many times the budget; the same budget would cut it again
def fast_json_whole(env, system, user, max_tokens=1500, timeout=90):
    """fast_json, asked once more: with CUT_RETRY times the budget when the reply was cut off at it, with the same budget when
    it was empty or not valid JSON (Mercury returns a key without its opening quote about once in 30 replies)."""
    try:
        return fast_json(env, system, user, max_tokens=max_tokens, timeout=timeout)
    except CutOff:
        return fast_json(env, system, user, max_tokens=max_tokens * CUT_RETRY, timeout=timeout)
    except (EmptyReply, ValueError):
        return fast_json(env, system, user, max_tokens=max_tokens, timeout=timeout)

class EmptyReply(Exception):
    pass

class CutOff(Exception):
    pass

class BadReply(Exception):
    pass

def fast_object(env, system, user, shape, key, max_tokens=1500, timeout=90):
    """fast_json for a caller that needs one JSON object holding `key`, described to the model as `shape`. The model sometimes
    wraps the object in a list, so a one-element list is unwrapped; a reply that is not valid JSON or not such an object is
    retried once with a stricter instruction and CUT_RETRY times the token budget, then raises BadReply saying what was wrong with it."""
    strict = system + f" Reply with exactly one JSON object {shape} and nothing else: no list, no prose."
    for sysmsg, budget in ((system, max_tokens), (strict, max_tokens * CUT_RETRY)):
        try:
            v = fast_json(env, sysmsg, user, max_tokens=budget, timeout=timeout, key=key)
        except (EmptyReply, CutOff) as e:
            why = str(e)
            continue
        except ValueError as e:
            why = f"reply was not valid JSON, possibly cut off ({e})"
            continue
        if isinstance(v, list) and len(v) == 1:
            v = v[0]
        if isinstance(v, dict) and key in v:
            return v
        why = f"reply was not a {shape} object"
    raise BadReply(why)

class ConfirmError(Exception):
    pass

def confirm_call(env, system, user, max_tokens=500, key="confirmed"):
    """The confirm step shared by figma judge, review, and jev acceptance: returns (the boolean `key`, evidence). A reply that is
    still not an {evidence, key} object after fast_object's retry raises ConfirmError, which the caller records as a judge error
    and never as a verdict."""
    try:
        v = fast_object(env, system, user, f'{{"evidence": str, "{key}": bool}}', key, max_tokens=max_tokens)
    except BadReply as e:
        raise ConfirmError(f"confirm call failed: {e}") from e
    return bool(v[key]), str(v.get("evidence", ""))[:300]


class Image:
    """A PNG or JPEG file put anywhere in a Jev state: jev_ask sends it as an image asset and writes its reference in its place."""
    def __init__(self, path):
        self.path = Path(path)

def with_assets(value, assets):
    """`value` with every Image replaced by a fresh reference, its file added to `assets` once (Jev's `assets` map)."""
    if isinstance(value, Image):
        ref = next((r for r, a in assets.items() if a["path"] == value.path), None)
        if not ref:
            ref = f"img-{uuid.uuid4().hex}"
            fmt = value.path.suffix.lower().lstrip(".")
            assets[ref] = {"path": value.path, "type": "image", "format": "jpg" if fmt == "jpeg" else fmt,
                           "data_base64": base64.b64encode(value.path.read_bytes()).decode()}
        return ref
    if isinstance(value, dict):
        return {k: with_assets(v, assets) for k, v in value.items()}
    if isinstance(value, list):
        return [with_assets(v, assets) for v in value]
    return value

class TooBig(RuntimeError):
    """TypeSafe refused the request as more input tokens than Jev reads (HTTP 400 max_tokens_exceeded, at 32768 tokens): the same
    request never passes, a smaller state can."""


def jev_ask(env, cfg, state, questions, label):
    """One TypeSafe (Jev) request: choice questions against one state, on the cost ledger. Returns Jev's whole reply.
    An Image in the state or the questions goes as an image asset, with the header that turns images on."""
    key = fc_config.typesafe_key(env, label)
    assets = {}
    body = json.loads(fc_core.scrub(json.dumps({"model": env.get("TYPESAFE_MODEL", "jev-latest"), "state": with_assets(state, assets),
                                                "questions": with_assets(questions, assets)})))
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if assets:  # the image bytes are never scrubbed: a secret cannot be in them, and a replaced byte would break the file
        body["assets"] = {r: {k: v for k, v in a.items() if k != "path"} for r, a in assets.items()}
        headers["TypeSafe-Danger-Zone-Images"] = "true"
    req = urllib.request.Request("https://api.typesafe.ai/v1/systemone", data=json.dumps(body).encode(), headers=headers)
    try:
        out = http_json(req, f"typesafe ({label})", timeout=180 if assets else 90)
    except RuntimeError as e:
        if "max_tokens_exceeded" in str(e):
            raise TooBig(str(e)) from e
        raise
    u = out.get("usage", {}) or {}
    price = (cfg.get("prices") or {}).get("typesafe_per_call")
    fc_core.LEDGER.add("typesafe", u.get("input_tokens") or u.get("prompt_tokens") or u.get("total_tokens") or 0, u.get("output_tokens") or 0,
               price if price is not None else None)
    return out

def reask_criterion(env, cfg, state, choices, rules, row, label):
    """The one re-ask both judges share: an UNCLEAR verdict, or a NOT_MET the confirm call refuted (confirmed=false, not a
    judge error), is put to Jev once more for that criterion alone, with the confirmer's evidence as context. Only a MET reply
    turns the row MET; both verdicts stay on the row. Anything else, and every judge error, is left as it was and still blocks."""
    refuted = row["choice"] == "NOT_MET" and row.get("confirmed") is False and not row.get("judge_error")
    if row["choice"] != "UNCLEAR" and not refuted:
        return row
    instructions = {"criterion": row["criterion"], "task": row["task"], "rules": rules}
    if refuted:
        instructions["context"] = f"An independent check was asked to show this criterion is violated and could not: {row.get('evidence', '')}"
    out = jev_ask(env, cfg, state, {"c0": {"type": "choice", "criteria": choices, "instructions": instructions}}, label + " re-ask")
    ans = (out.get("answers") or {}).get("c0") or {}
    probs = ans.get("probabilities") or {}
    row["first_choice"] = row["choice"]
    row["reask"] = {"choice": ans.get("choice"), "p_met": round(probs.get("MET", 0), 3), "p_not_met": round(probs.get("NOT_MET", 0), 3)}
    if ans.get("choice") == "MET":
        row["choice"] = "MET"
    return row
