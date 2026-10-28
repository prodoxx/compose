"""The one way the CLI calls Linear's GraphQL API: every call is paced with every other compose process on the machine (fc_pace),
learns the hour's budget from Linear's reply headers, and a rate limit one call runs into holds the whole fleet as long as Linear says.
The key is the active workspace's `linear_api_key`; it goes in a header, never argv or a results file."""
import json, os, sys, threading, time, urllib.error, urllib.request
import fc_config, fc_pace

LINEAR_URL = "https://api.linear.app/graphql"

LINEAR_RETRIES = 5     # waits before a rate-limited call gives up
LINEAR_MAX_WAIT = 300  # the doubling pause stops growing here
LINEAR_CALL_SECONDS = 120  # one call, its turn in the pace, its rate-limit waits and slow replies included, never takes longer: it fails naming the time, so no caller waits silently

# Linear's hour for an API key: 2,500 requests and 3,000,000 complexity points, shared by every compose process using the
# key. Linear's bucket refills continuously (a full limit per hour), so the reset header slides with the clock and is no
# deadline. Calls go at full speed while over a tenth of both remain; below that they keep to the refill rate, down to a
# reserve the CLI never spends (50 requests, 60,000 points), so a call from outside the pace still has room.
LINEAR_PACE_FLOOR = 0.25  # seconds between two calls from this machine at most speed
LINEAR_RESERVE = 0.02     # the share of each hourly limit the CLI leaves untouched
LINEAR_SLOW = 0.1         # the share of each hourly limit left under which calls slow to the refill rate
LINEAR_REFILL = 3600      # seconds the bucket takes to give back a full limit

class LinearRateLimited(RuntimeError):
    def __init__(self, message, wait):
        super().__init__(message)
        self.wait = wait

class LinearBusy(RuntimeError):
    """Linear's rate limit outlasted the call's time: nothing ran, and Linear lets calls through again after `wait` seconds."""

def _number(v):
    try:
        return float(v) if isinstance(v, (str, int, float)) else None
    except ValueError:
        return None

def _reset_at(v):
    """A reset header as epoch seconds: Linear sends milliseconds."""
    return None if v is None else v / 1000 if v > 1e11 else v

def _rate_limit_wait(headers, attempt):
    """Seconds to wait: Retry-After, else the sooner reset header (UTC epoch seconds or milliseconds), else a doubling pause."""
    h = headers or {}
    try:
        if h.get("Retry-After"):
            return max(1, float(h["Retry-After"]))
        resets = [float(h[k]) for k in ("X-RateLimit-Complexity-Reset", "X-RateLimit-Requests-Reset") if h.get(k)]
    except ValueError:
        resets = []
    resets = [r / 1000 if r > 1e11 else r for r in resets]
    return max(1, min(resets) - time.time()) if resets else min(LINEAR_MAX_WAIT, 10 * 2 ** attempt)

def _learn(headers):
    """What the reply's headers say about the hour: each limit, what is left, when it resets, and this call's complexity."""
    h = headers or {}
    facts = {}
    for kind in ("Requests", "Complexity"):
        limit, left, reset = (_number(h.get(f"X-RateLimit-{kind}-{k}")) for k in ("Limit", "Remaining", "Reset"))
        if limit and left is not None and reset:
            facts.update({f"{kind.lower()}_limit": limit, f"{kind.lower()}_left": left, f"{kind.lower()}_reset": _reset_at(reset)})
    cost = _number(h.get("X-Complexity"))
    if facts or cost is not None:
        fc_pace.learn("linear", cost=cost, **facts)

def linear_pace(s, now):
    """fc_pace's policy for Linear, from what the last reply taught (nothing known: full speed): (not before, spacing)."""
    not_before, spacing = 0, LINEAR_PACE_FLOOR
    for kind, per_call in (("requests", 1), ("complexity", max(1, s.get("cost") or 1))):
        limit, left, reset = s.get(f"{kind}_limit"), s.get(f"{kind}_left"), s.get(f"{kind}_reset")
        if not limit or left is None or not reset or reset <= now:  # nothing learned, or the hour it described is over
            continue
        refill = LINEAR_REFILL / limit  # seconds the bucket takes to give back one point
        if left <= limit * LINEAR_SLOW:
            spacing = max(spacing, per_call * refill)
        spare = left - limit * LINEAR_RESERVE
        if spare < per_call:
            not_before = max(not_before, now + (per_call - spare) * refill)
    return not_before, spacing

def _linear_post(query, variables, attempt):
    req = urllib.request.Request(os.environ.get("LINEAR_URL") or LINEAR_URL, data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": fc_config.LINEAR_KEY})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            out, headers = json.load(r), getattr(r, "headers", None)
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read().decode(errors="replace"))
        except ValueError:
            out = None
        headers = e.headers
        e.close()
        _learn(headers)
        if not (isinstance(out, dict) and out.get("errors")):
            if e.code == 429:
                raise LinearRateLimited("Linear rate limit (HTTP 429)", _rate_limit_wait(headers, attempt))
            raise
    else:
        _learn(headers)
    if out.get("errors"):
        message = "; ".join(x.get("message", "?") for x in out["errors"])[:300]
        if any((x.get("extensions") or {}).get("code") == "RATELIMITED" for x in out["errors"]):
            raise LinearRateLimited(message, _rate_limit_wait(headers, attempt))
        raise RuntimeError(message)
    return out["data"]

def _within(seconds, fn):
    """fn() run in a thread of its own and given up on after `seconds`: a reply that trickles in keeps urlopen's per-read timeout from ever firing."""
    box = {}
    def work():
        try:
            box["out"] = fn()
        except BaseException as e:  # noqa: BLE001  re-raised in the caller
            box["err"] = e
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(max(0, seconds))
    if t.is_alive():
        raise RuntimeError(f"Linear gave no answer within {LINEAR_CALL_SECONDS}s; a write may still have gone through, so read the issue before repeating it")
    if "err" in box:
        raise box["err"]
    return box["out"]

def linear_gql(query, variables):
    """One GraphQL call, bounded by LINEAR_CALL_SECONDS: it takes its turn in the fleet's pace first. A rate-limited call did not
    run, so it holds every process off Linear, waits as long as Linear says (while that fits in the time left) and runs again,
    saying so on stderr."""
    deadline = time.monotonic() + LINEAR_CALL_SECONDS
    for attempt in range(LINEAR_RETRIES + 1):
        fc_pace.wait("linear", linear_pace, deadline - time.monotonic(), "Linear")
        try:
            return _within(deadline - time.monotonic(), lambda: _linear_post(query, variables, attempt))
        except LinearRateLimited as e:
            fc_pace.hold("linear", e.wait)
            if e.wait > deadline - time.monotonic() or attempt == LINEAR_RETRIES:
                raise LinearBusy(f"{e} (Linear lets calls through again in about {fc_pace.about(e.wait)}; run it again then)") from None
            print(f"Linear rate limit: waiting {int(e.wait)}s, then retry {attempt + 1} of {LINEAR_RETRIES}", file=sys.stderr)
            time.sleep(e.wait)
