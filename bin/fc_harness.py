"""Harnesses other than Claude Code and pi: one table of them, and the accessors every other module reads it through."""
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import fc_core


def _codex_probe(cid):
    return ["codex", "exec", "--ephemeral", "--skip-git-repo-check"] + (["-m", cid] if cid else []) + ["Reply with exactly: ok"]

def _codex_result(p):
    if p.returncode == 0:
        return True, "answered"
    for line in p.stderr.splitlines():
        if line.startswith("ERROR: "):
            body = line[len("ERROR: "):]
            try:
                return False, json.loads(body)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                return False, body.strip()
    tail = [l.strip() for l in p.stderr.splitlines() if l.strip()] or [l.strip() for l in p.stdout.splitlines() if l.strip()]
    return False, tail[-1] if tail else f"exit {p.returncode}"

def _git_dirs(f):
    c = fc_core.checkout(f)
    return [c.common, c.git_dir] if c else []

def _codex_argv(cid, effort, prompt, skill, cwd, results, work=None, more=()):
    argv = ["-c", "check_for_update_on_startup=false"]
    if cid is not None:
        argv += ["-m", cid]
    if effort:
        argv += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
    argv += ["-s", "workspace-write", "-a", "never", "-c", "sandbox_workspace_write.network_access=true",
             "-c", 'projects={%s={trust_level="trusted"}}' % json.dumps(os.path.realpath(cwd)),
             "-c", "developer_instructions=" + json.dumps(prompt + "\n\nThe compose skill is the file " + str(skill) + ".")]
    seen = set()
    for d in [results, *_git_dirs(cwd)] + ([work, *_git_dirs(work)] if work is not None else []) + list(more):
        if d is None or (r := str(Path(d).resolve())) in seen:
            continue
        seen.add(r)
        argv += ["--add-dir", str(d)]
    return argv

def _codex_events(text):
    """Parse `codex exec --json` stdout, one JSON event per line: (reply, tools, usage). Lines that are not JSON and
    unknown event types are skipped. reply is the item.text of the last item.completed agent_message; tools counts the
    other item.completed items but reasoning; usage sums input_tokens and output_tokens over every turn.completed
    (input already covers cached input), and cost is 0.0: Codex runs on the ChatGPT subscription."""
    reply, tools, tin, tout = "", 0, 0, 0
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message":
                reply = item.get("text", "")
            elif item.get("type") != "reasoning":
                tools += 1
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            tin += u.get("input_tokens", 0)
            tout += u.get("output_tokens", 0)
    return reply, tools, {"tokens_in": tin, "tokens_out": tout, "cost": 0.0}

def _codex_once(cid, cwd, prompt, read_only=False):
    argv = ["codex", "-a", "never", "exec", "--json", "--ephemeral", "--skip-git-repo-check"] + (["-m", cid] if cid is not None else [])
    if read_only:
        argv += ["-s", "read-only"]
    else:
        argv += ["-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"]
        seen = set()
        for d in _git_dirs(cwd):
            if str(d) not in seen:
                seen.add(str(d))
                argv += ["--add-dir", str(d)]
    return argv + [prompt]

def _codex_hash(event, group):
    o = {"event_name": re.sub(r"(?<=[a-z])(?=[A-Z])", "_", event).lower(),
         "hooks": [{"type": h["type"], "command": h["command"], "timeout": h.get("timeout", 600), "async": h.get("async", False),
                    **({"statusMessage": h["statusMessage"]} if h.get("statusMessage") else {})} for h in group["hooks"]]}
    if group.get("matcher"):
        o["matcher"] = group["matcher"]
    return "sha256:" + hashlib.sha256(json.dumps(o, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

HARNESSES = {
    "codex": {"name": "Codex", "kind": "codex", "exe": "codex", "home_env": "CODEX_HOME", "home": "~/.codex", "sigil": "$",
              "session_env": "CODEX_THREAD_ID", "stand_in": "codex",
              "example": "gpt-6.1-sol", "herdr_ready": False, "probe": _codex_probe, "result": _codex_result, "argv": _codex_argv,
              "events": _codex_events, "once": _codex_once,
              "login_check": ("login", "status"), "login": ("login",),
              "login_screens": ("Sign in with ChatGPT", "Finish signing in via your browser"),
              "hooks_file": "hooks.json", "trust_file": "config.toml", "hash": _codex_hash,
              "hooks": (("PreToolUse", "Write|Edit|MultiEdit|NotebookEdit", "guard"), ("PreToolUse", "Bash", "guard"),
                        ("UserPromptSubmit", None, "skillcheck"), ("SessionStart", "compact", "reenter"))},
    "gemini": {},
    "grok": {},
}

RESERVED = tuple(HARNESSES)


def harness_of(model):
    for key in rows():
        if model == key or (model or "").startswith(key + ":"):
            return key
    return None

def model_id(model):
    key = harness_of(model)
    return (model[len(key) + 1:] or None) if key and model != key else None

def rows():
    return [k for k, v in HARNESSES.items() if v]

def installed():
    return [k for k in rows() if shutil.which(HARNESSES[k]["exe"])]

def detected(env=None):
    """The harness whose session_env names a variable that is set and non-empty in `env` (the process env by default), else
    None: a session started by hand has no flag and no $COMPOSE_HARNESS, so it is read out of its own environment."""
    return next((k for k in rows() if (os.environ if env is None else env).get(HARNESSES[k]["session_env"])), None)

def home_dir(key):
    row = HARNESSES[key]
    return Path(os.environ.get(row["home_env"]) or os.path.expanduser(row["home"]))

def signed_in(key):
    row = HARNESSES[key]
    try:
        p = subprocess.run([row["exe"], *row["login_check"]], stdin=subprocess.DEVNULL, timeout=30,
                           env={**os.environ, row["home_env"]: str(home_dir(key))})
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0

def login_message(key):
    row, home = HARNESSES[key], home_dir(key)
    return (f"{row['name']} is not signed in for {row['home_env']}={home}. compose never signs in for you: "
            f"run `{row['home_env']}={home} {row['exe']} {' '.join(row['login'])}` yourself, then the same crew start again.")

def skills_dir(key):
    return home_dir(key) / "skills"

def probe_command(model):
    return HARNESSES[harness_of(model)]["probe"](model_id(model))

def probe_result(model, p):
    return HARNESSES[harness_of(model)]["result"](p)

def crew_argv(model, effort, prompt, skill, cwd, results, work=None, more=()):
    return HARNESSES[harness_of(model)]["argv"](model_id(model), effort, prompt, skill, cwd, results, work, more)

def once_command(model, cwd, prompt, read_only=False):
    return HARNESSES[harness_of(model)]["once"](model_id(model), cwd, prompt, read_only)

def once_result(model, p):
    key = harness_of(model)
    reply, tools, usage = HARNESSES[key]["events"](p.stdout)
    if p.returncode != 0 and not reply:
        raise RuntimeError(f"{HARNESSES[key]['name']} failed: {HARNESSES[key]['result'](p)[1][:300]}")
    fc_core.LEDGER.add(f"{key}-subscription", usage["tokens_in"], usage["tokens_out"], 0.0)
    return None, reply[-1500:], tools, usage

def hook_groups(key, checkout):
    out = {}
    for ev, matcher, sub in HARNESSES[key].get("hooks", ()):
        g = {"hooks": [{"type": "command", "command": f'"{checkout}/bin/compose" {sub}', "timeout": 10}]}
        if matcher:
            g["matcher"] = matcher
        out.setdefault(ev, []).append(g)
    return out
