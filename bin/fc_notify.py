"""Opt-in phone notifications for the gates only the product owner can clear: a payment halt, a sign-in or other attended prompt,
a verified stack waiting for the push word, and a question the posture cannot answer. Off unless `notify` is in config.json:
`{"notify": {"ntfy": "https://ntfy.sh/<topic>"}}`. A message is one line, the project slug and a fixed phrase, never a key or code.
Imports only lower modules."""

import re, urllib.request

import fc_config, fc_core, fc_tasks

GATE_RE = re.compile(r"sign[ -]?in|log[ -]?in|password|payment|top[ -]?up|credit|allow|approv|push", re.I)  # what a needs-user can only get from the owner

def target():
    """The ntfy topic URL from the active settings' `notify` block, else None (absent, or not an http(s) URL)."""
    try:
        block = fc_config.settings(fc_config.read_config())[1].get("notify")
    except (OSError, ValueError):
        return None
    url = block.get("ntfy") if isinstance(block, dict) else None
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else None

def send(proj, summary):
    """Post one line to the configured topic, titled with the project slug. True when it was sent; never raises, since a phone that
    does not answer must not stop a run."""
    url = target()
    if not url:
        return False
    line = " ".join(fc_core.scrub(summary).split())[:200]
    req = urllib.request.Request(url, data=line.encode(), method="POST", headers={"Title": f"compose {proj.name}", "Priority": "high"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except (OSError, ValueError):
        return False
    return True

def gates(proj, events):
    """Send one notification for each watch event line that is a gate only the owner can clear: an agent blocked on a prompt, a
    verify that passed while the posture's push is off, and, while away or yolo is on, a crew needs-user or blocked whose
    message asks for a sign-in, a payment, an approval or a push."""
    y = fc_tasks.yolo_state(proj)
    for e in events:
        if m := re.match(r"event agent=(\S+) status=blocked", e):
            send(proj, f"{m.group(1)} is waiting on a prompt only you can answer (sign-in or allow)")
        elif e == "event verify=PASS" and y and not y.get("push") and not fc_tasks.submit_after_verify(proj):
            send(proj, "verify passed; the stack waits for your push approval")
        elif y and (m := re.match(r"event crew=(\S+) state=(needs-user|blocked) message=(.*)$", e)) and GATE_RE.search(m.group(3)):
            send(proj, f"{m.group(1)} is {'asking' if m.group(2) == 'needs-user' else 'blocked'} on something only you can clear")
