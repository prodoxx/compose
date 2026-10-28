"""TypeSafe makes choices; an optional small OpenAI-compatible model writes field values."""

import json
import math
import os
import random
import time
from email.utils import parsedate_to_datetime

import httpx

from .questions import NEXT_ACTION, TARGET, TEXT_RETRY, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=25)

BUSY = {429, 502, 503, 504, 529}  # a provider that is rate limiting, overloaded, or behind a gateway that is: it passes
RETRY_BUDGET = 30.0  # seconds one call may wait on a busy provider in all; a jev e2e case has 150s for every call it makes
RETRY_MAX_WAIT = 8.0  # the longest backoff between two tries (a Retry-After may ask for more, within the budget)
RETRY_TRIES = 10  # tries in all, so a provider answering Retry-After: 0 forever still ends


def retry_after(response):
    """The seconds the provider's Retry-After header asks for, as a number or an HTTP date, else None."""
    value = response.headers.get("retry-after", "").strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError, IndexError):
        return None


def post_json(url, key, body):
    """POST `body` and return the JSON reply. A busy provider (BUSY) is tried again after the wait its Retry-After asks for,
    else an exponential backoff, both with jitter so the calls it turned away do not all return at once, until RETRY_BUDGET
    seconds of waiting or RETRY_TRIES tries are spent. Any other error fails at once."""
    waited = 0.0
    for attempt in range(RETRY_TRIES):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        status = response.status_code
        if status not in BUSY:
            if response.is_error:
                raise RuntimeError(f"Model provider returned HTTP {status}; no action executed.")
            return response.json()
        asked, backoff = retry_after(response), min(RETRY_MAX_WAIT, 0.5 * 2**attempt)
        if asked is not None and waited + asked > RETRY_BUDGET:
            raise RuntimeError(f"Model provider returned HTTP {status} and asked to wait {asked:.0f}s, past the "
                               f"{RETRY_BUDGET:.0f}s retry budget; no action executed.")
        if waited >= RETRY_BUDGET or attempt == RETRY_TRIES - 1:
            break
        wait = asked + random.uniform(0, 0.5) if asked is not None else backoff / 2 + random.uniform(0, backoff / 2)
        wait = min(wait, RETRY_BUDGET - waited)
        time.sleep(wait)
        waited += wait
    raise RuntimeError(f"Model provider returned HTTP {status} {attempt + 1} times over {waited:.0f}s; no action executed.")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT", "hover": "HOVER"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded", "secret") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


CYCLE_WARN, CYCLE_STOP = 4, 8


def cycling(history, n):
    """The last n actions (no waits) started and ended in at most two page states: A,B,A,B or one no-op again."""
    recent = history[-n:]
    return (
        len(recent) == n
        and all(h["kind"] != "wait" and h.get("before") and h.get("after") for h in recent)
        and len({h[k] for h in recent for k in ("before", "after")}) <= 2
    )


def choose(state, goal, history):
    elements, targets, controls = action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal; "
                     "the run types a secret field's value itself.",
        "SELECT": "Select an observed dropdown value.",
        "HOVER": "Move the pointer over an element, without clicking, to open a menu or tooltip that opens on hover.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded", "secret") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    page = {k: state[k] for k in ("url", "title", "text")}
    # Text and controls cover only the visible area. Say where the page continues, or a control below the fold
    # (a form's Submit) does not exist for the model and it stops BLOCKED with every field filled.
    hidden = [where for where, op in (("above", "SCROLL_UP"), ("below", "SCROLL_DOWN")) if op in controls]
    if hidden:
        page["offscreen"] = (f"The page continues {' and '.join(hidden)} the visible area. "
                             "Controls there are not listed until scrolled into view.")
    # Disabled controls are not targets, but a disabled Submit tells the model the form still waits for something,
    # and the unanswered list says what. Without them it skipped a question and looped beside a locked Submit.
    for key in ("disabled", "unanswered"):
        if state.get(key):
            page[key] = state[key]
    if cycling(history, CYCLE_WARN):
        page["no_progress"] = (
            f"Your last {CYCLE_WARN} actions only moved between the same page states; nothing advanced. "
            "Do not repeat them. A disabled control is waiting on something unanswered or empty: "
            "answer a visible unanswered item now, scroll only if none is visible, or choose BLOCKED."
        )
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": page,
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    started = time.perf_counter()
    result = post_json("https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], body)
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": usage(result),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def usage(result):
    """TypeSafe reports input_tokens and output_tokens; add the total_tokens an OpenAI-style consumer sums."""
    counts = dict(result.get("usage") or {})
    if "total_tokens" not in counts and {"input_tokens", "output_tokens"} <= counts.keys():
        counts["total_tokens"] = counts["input_tokens"] + counts["output_tokens"]
    return counts


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def field_text(context):
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {"reasoning": {"effort": "low"}}
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    started = time.perf_counter()
    messages = [{"role": "system", "content": TEXT_VALUE}, {"role": "user", "content": json.dumps(context)}]
    usage = {}
    # Small models sometimes refuse ({"text": null}) or fence the JSON. Ask once more, then stop untyped.
    for _ in range(2):
        result = post_json(
            base + "/chat/completions",
            key,
            {
                "model": model,
                "max_tokens": 1024,
                "response_format": {"type": "json_object"},
                **reasoning,
                "messages": messages,
            },
        )
        for k, v in result.get("usage", {}).items():
            if type(v) is int:
                usage[k] = usage.get(k, 0) + v
        content = None
        try:
            content = result["choices"][0]["message"]["content"]
            value = text_value(content)
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            messages += [{"role": "assistant", "content": str(content)}, {"role": "user", "content": TEXT_RETRY}]
            continue
        return value, {"model": model, "latency_ms": round((time.perf_counter() - started) * 1000), "usage": usage}
    raise ValueError("Text helper returned no valid field value; nothing typed.")


def text_value(content):
    """The value of a reply holding exactly one {"text": "..."} object, bare or inside a code fence."""
    output = json.JSONDecoder().raw_decode(content[content.index("{") :])[0]
    value = output["text"]
    if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError()
    return value
