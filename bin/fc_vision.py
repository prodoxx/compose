"""The fast model asked about images: one reply as a JSON object, for a caller that needs a reason Jev's choice answers do not carry.
fc_models.fast_json is text only; this is the same call with the images in the message, through OpenRouter, or through Claude Code
when TEXT_MODEL is a `claude:<id>` (which reads the image files itself)."""
import base64, json, urllib.request
from pathlib import Path
import fc_core, fc_claude, fc_models, fc_textmodel

READ_IMAGES = ("Look at each of these image files with the Read tool before you answer, one per screen: {files}. "
               "Reply with only the JSON object, as the last thing you write.")

def image_part(path):
    """One OpenRouter message part holding the PNG or JPEG at `path`, as a data URL."""
    path = Path(path)
    kind = "jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "png"
    return {"type": "image_url", "image_url": {"url": f"data:image/{kind};base64,{base64.b64encode(path.read_bytes()).decode()}"}}

def ask_openrouter(env, model, system, user, images, budget, timeout, key):
    """One OpenRouter chat call with `images` [(label, path)] after `user`; the reply parsed as fc_models.parse_reply does."""
    parts = [{"type": "text", "text": fc_core.scrub(user)}]
    for label, path in images:
        parts += [{"type": "text", "text": f"Screen {fc_core.scrub(label)}:"}, image_part(path)]
    body = {"model": model, "max_tokens": budget, "temperature": 0, "response_format": {"type": "json_object"},
            "reasoning": fc_models.reasoning(env), "usage": {"include": True},
            "messages": [{"role": "system", "content": fc_core.scrub(system)}, {"role": "user", "content": parts}]}
    req = urllib.request.Request(env.get("TEXT_MODEL_BASE_URL", fc_textmodel.TEXT_MODEL_BASE_URL).rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {fc_textmodel.key(env, model)}", "Content-Type": "application/json"})
    out = fc_textmodel.call(fc_models.http_json, req, "openrouter (fast model, images)", model, timeout)
    u = out.get("usage", {})
    fc_core.LEDGER.add("openrouter", u.get("prompt_tokens", 0), u.get("completion_tokens", 0), u.get("cost"))
    choice = (out.get("choices") or [{}])[0]
    return fc_models.parse_reply((choice.get("message") or {}).get("content"), choice.get("finish_reason") == "length", budget, key)

def ask_claude(model, system, user, images, timeout, key):
    """One Claude Code call that reads the image files with its Read tool (the files sit in one folder); the JSON object in its reply."""
    files = ", ".join(f"{Path(p).name} is {label}" for label, p in images)
    prompt = f"{system}\n\n{user}\n\n{READ_IMAGES.format(files=files)}"
    _, reply, _, _ = fc_claude.claude_once(Path(images[0][1]).parent, prompt, timeout=max(timeout, 300), model=fc_claude.claude_id(model), tools="Read", keep=None)
    return fc_models.parse_reply(reply, False, 0, key)

def look_json(env, system, user, images, key, max_tokens=700, timeout=180):
    """The JSON object holding `key` that the fast model answers `system` and `user` with after looking at `images`, [(label, path)] in
    the order they are shown. Asked once more, with more room, when the reply is empty, cut off or not such an object; then raises
    RuntimeError saying so. A model that takes no images, or a missing key, raises too: nothing here guesses an answer."""
    model = env.get("TEXT_MODEL", fc_textmodel.TEXT_MODEL)
    why = "no reply"
    for budget in (max_tokens, max_tokens * fc_models.CUT_RETRY):
        try:
            found = (ask_claude(model, system, user, images, timeout, key) if fc_claude.is_claude(model)
                     else ask_openrouter(env, model, system, user, images, budget, timeout, key))
        except (fc_models.EmptyReply, fc_models.CutOff, ValueError) as e:
            why = str(e)
            continue
        if isinstance(found, list) and len(found) == 1:
            found = found[0]
        if isinstance(found, dict) and key in found:
            return found
        why = f"the reply held no {key}"
    raise RuntimeError(f"the fast model did not answer about the screens: {why}")
