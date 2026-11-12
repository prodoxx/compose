"""pi's openrouter-fast provider in models.json, pi's own auth.json overrides, and the TEXT_MODEL lines in ~/.compose/.env."""
import json
from pathlib import Path
import fc_core, fc_models


PI_MODELS = fc_models.PI_AGENT_DIR / "models.json"
PROVIDER = {"baseUrl": "https://openrouter.ai/api/v1", "api": "openai-completions", "apiKey": "$OPENROUTER_API_KEY",
            "models": [{"id": "z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast", "cost": {"input": 0.15, "output": 0.5, "cacheRead": 0, "cacheWrite": 0}}]}
TEXT_MODEL_DEFAULTS = {"TEXT_MODEL": "anthropic/claude-sonnet-5.5", "TEXT_MODEL_BASE_URL": "https://openrouter.ai/api/v1"}
PI_AUTH = PI_MODELS.parent / "auth.json"

def has_glm(models):
    """True when models is a list holding the GLM preset model."""
    return isinstance(models, list) and any(isinstance(m, dict) and m.get("id") == PROVIDER["models"][0]["id"] for m in models)

def provider_lacks(provider):
    """What an existing openrouter-fast provider dict lacks, in order: apiKey, then the GLM preset model."""
    lacks = []
    if not provider.get("apiKey"):
        lacks.append("apiKey")
    if not has_glm(provider["models"]):
        lacks.append("the GLM preset model")
    return lacks

def provider_declared(path):
    """True when the models.json at path has providers.openrouter-fast with an apiKey and the GLM preset model."""
    try:
        provider = json.loads(Path(path).read_text())["providers"]["openrouter-fast"]
        models = provider["models"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return bool(provider.get("apiKey")) and has_glm(models)

def text_model_set(env_path):
    """True when the .env at env_path has TEXT_MODEL and TEXT_MODEL_BASE_URL lines with values."""
    try:
        lines = Path(env_path).read_text().splitlines()
    except OSError:
        return False
    found = set()
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and value.strip() and key.strip() in TEXT_MODEL_DEFAULTS:
            found.add(key.strip())
    return set(TEXT_MODEL_DEFAULTS) <= found

def pi_auth_overrides(path):
    """Which of "openrouter", "openrouter-fast" are top-level entries of pi's auth.json at path, in that order; [] when unreadable."""
    try:
        auth = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    return [name for name in ("openrouter", "openrouter-fast") if isinstance(auth, dict) and name in auth]

def _read_provider(path):
    """Read the models.json at path: the fix-by-hand line when it cannot be given the provider, else (models, provider, lacks)
    with provider None (and lacks empty) when openrouter-fast is absent."""
    try:
        models = json.loads(path.read_text())
    except FileNotFoundError:
        models = {}
    except ValueError:
        return f"{path} is not valid JSON; fix it by hand"
    if not isinstance(models, dict):
        return f"{path} is not a JSON object; fix it by hand"
    providers = models.setdefault("providers", {})
    if not isinstance(providers, dict):
        return f"{path} has no providers object; fix it by hand"
    provider = providers.get("openrouter-fast")
    if provider is None:
        return models, None, []
    if not isinstance(provider, dict) or not isinstance(provider.setdefault("models", []), list):
        return f"{path} has an openrouter-fast that is not a provider with a models list; fix it by hand"
    return models, provider, provider_lacks(provider)

def declare_provider(path):
    """Add providers.openrouter-fast = PROVIDER to the models.json at path, or give an existing one the apiKey and GLM preset
    model it lacks, keeping the rest; one line saying what it did."""
    path = Path(path)
    read = _read_provider(path)
    if isinstance(read, str):
        return read
    models, provider, added = read
    if provider is None:
        models["providers"]["openrouter-fast"] = PROVIDER
        line = f"declared openrouter-fast in {path}"
    else:
        if "apiKey" in added:
            provider["apiKey"] = PROVIDER["apiKey"]
        if "the GLM preset model" in added:
            provider["models"].append(PROVIDER["models"][0])
        if not added:
            return f"openrouter-fast already declared in {path}; left as is"
        line = f"added {' and '.join(added)} to openrouter-fast in {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(models, indent=2) + "\n")
    return line

def preview_provider(path):
    """The line declare_provider would give for the models.json at path, without writing anything."""
    path = Path(path)
    read = _read_provider(path)
    if isinstance(read, str):
        return read
    _, provider, lacks = read
    if provider is None:
        return f"would declare openrouter-fast in {path}"
    if not lacks:
        return f"openrouter-fast already declared in {path}; left as is"
    return f"would add {' and '.join(lacks)} to openrouter-fast in {path}"

def write_text_model(env_path):
    """Append the TEXT_MODEL_DEFAULTS lines the .env lacks, never changing existing ones; one line saying what it did."""
    path = Path(env_path)
    try:
        text = path.read_text()
    except FileNotFoundError:
        text = ""
    have = set()
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and value.strip():
            have.add(key.strip())
    missing = [(k, v) for k, v in TEXT_MODEL_DEFAULTS.items() if k not in have]
    if not missing:
        return f"{', '.join(TEXT_MODEL_DEFAULTS)} already set in {path}; left as is"
    add = "".join(f"{k}={v}\n" for k, v in missing)
    if text and not text.endswith("\n"):
        add = "\n" + add
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(add)
    return f"appended {', '.join(k for k, _ in missing)} to {path}"
