"""The fast text model's identity and health: the id compose falls back to, the errors for a model that cannot answer, and the check
that finds it gone before a run, a verify, a figma judge or a prove spends time on it."""
import json, os, re, time
import fc_claude, fc_config, fc_core

TEXT_MODEL = "anthropic/claude-sonnet-5.5"  # the fast text model when ~/.compose/.env names none: the one place the id is written (fc_pi.TEXT_MODEL_DEFAULTS reads it)
TEXT_MODEL_BASE_URL = "https://openrouter.ai/api/v1"
RETIRED_TEXT_MODELS = ("inception/mercury-2.5",)  # ids compose once wrote into a new .env that OpenRouter no longer serves: setup replaces them
PING_TOKENS = 64


class HttpRefused(RuntimeError):
    """A provider answered a request with an HTTP error status; `code` is the status and `body` the (cut) answer."""
    def __init__(self, message, code, body):
        super().__init__(message)
        self.code, self.body = code, body


class TextModelError(RuntimeError):
    """The fast text model cannot answer at all (no longer served, or no key to ask with). A model error, never a finding about the work
    under check; nothing retries it, and a caller that records errors per item says it once and stops."""


GONE = re.compile(r"no longer available|no endpoints? found|no provider serves|not a valid model|(unknown|invalid|unsupported) model|"
                  r"model[^.]{0,40}(not found|does not exist|not available)", re.I)

def gone(model, e):
    """The line saying the model is gone, when the provider's refusal `e` (an HttpRefused) says so (a 404, or a 400 that names the model),
    else None: the provider's own words and the `.env` line to change."""
    if e.code != 404 and not (e.code == 400 and GONE.search(e.body)):
        return None
    try:
        said = json.loads(e.body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        said = e.body
    return (f"the fast text model {model} is not available (HTTP {e.code}: {' '.join(str(said).split())[:200]}): set TEXT_MODEL in {fc_config.ENV_FILE} "
            f"to a model your provider serves ({TEXT_MODEL} is compose's default); compose setup writes it into a new .env")

def key(env, model):
    """The key the fast text model is asked with, else a TextModelError saying how to store it."""
    if not env.get("TEXT_MODEL_API_KEY"):
        raise TextModelError(f"the fast text model {model} has no key to be asked with: pbpaste | compose workspace key {fc_config.WORKSPACE}")
    return env["TEXT_MODEL_API_KEY"]

def call(http_json, req, provider, model, timeout):
    """`http_json(req, provider, timeout)`, with a refusal that says the model is gone raised as a TextModelError; any other refusal as it came."""
    try:
        return http_json(req, provider, timeout=timeout)
    except HttpRefused as e:
        if line := gone(model, e):
            raise TextModelError(line) from e
        raise

def check(env, ask):
    """Raise TextModelError when the fast text model is gone: one tiny call through `ask` (fast_json) of the model the fast calls use, a pass
    kept for the day in the probe cache so a run of `verify`s does not pay for it again. Anything else that goes wrong (a busy provider, a
    slow answer, an odd reply, a refused key) says nothing about the model and passes; a `claude:` model, no key, and COMPOSE_NO_PREFLIGHT=1
    (offline work) make no call."""
    model = env.get("TEXT_MODEL") or TEXT_MODEL
    if os.environ.get("COMPOSE_NO_PREFLIGHT") or fc_claude.is_claude(model) or not env.get("TEXT_MODEL_API_KEY"):
        return
    entry, today = f"text:{env.get('TEXT_MODEL_BASE_URL', TEXT_MODEL_BASE_URL)}|{model}", time.strftime("%Y-%m-%d")
    try:
        cache = json.loads(fc_config.PROBE_CACHE.read_text()) if fc_config.PROBE_CACHE.exists() else {}
    except (OSError, ValueError):
        cache = {}
    if (cache.get(entry) or {}).get("date") == today:
        return
    try:
        ask(env, "Reply with one JSON object.", '{"say": "hi"}', max_tokens=PING_TOKENS, timeout=30)
    except TextModelError:
        raise
    except Exception:  # noqa: BLE001 - not a verdict on the model, and not cached either
        return
    cache[entry] = {"date": today, "ok": True, "why": "answered"}
    fc_config.PROBE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    fc_core.atomic_write(fc_config.PROBE_CACHE, json.dumps(cache, indent=2))
