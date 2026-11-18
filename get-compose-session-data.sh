#!/usr/bin/env bash
# Copies a text digest of this machine's compose session data to the clipboard, to paste into a chat.
# It reads only: nothing on this machine is changed. It never opens auth.json or .env, and redacts key-shaped strings.
#
#   ./get-compose-session-data.sh              copy the digest to the clipboard
#   ./get-compose-session-data.sh digest.txt   write it to a file instead
#
# The digest holds:
#   - the last 250 lines of ~/.compose/history.jsonl
#   - every .compose/<project>/results folder under the home folder (retired ones too): its teammates by role and
#     model, its Linear feature, why each task that ended without passing missed (retro.jsonl), and the last 12 lines
#     of run.log and verify.log
#   - an inventory of the 40 newest Claude Code transcript folders (file counts, sizes, dates), never their content
set -euo pipefail

digest=$(python3 - <<'EOF'
import collections, json, re, subprocess, time
from pathlib import Path

H, out = Path.home(), []
LIMIT = 150000  # characters: what a paste carries comfortably
day = lambda t: time.strftime("%Y-%m-%d", time.localtime(t))


def tail(p, n, w=400):
    """The last n lines of a file, each cut at w characters; nothing when it cannot be read."""
    try:
        return [l[:w] for l in p.read_text(errors="replace").splitlines()[-n:]]
    except OSError:
        return []


hist = tail(H / ".compose/history.jsonl", 10**9, 10**6)
out += [f"===== ~/.compose/history.jsonl: {len(hist)} lines, last 250 ====="] + hist[-250:]

skip = ["Library", "node_modules", ".git", "scratch", ".Trash", ".cache", ".npm"]
find = (["find", str(H), "-maxdepth", "12", "("] + " -o ".join(f"-name {s}" for s in skip).split()
        + [")", "-prune", "-o", "-type", "d", "-name", "results", "-path", "*/.compose/*", "-print"])
for res in sorted(Path(p) for p in subprocess.run(find, capture_output=True, text=True).stdout.splitlines()):
    files = [f for f in res.iterdir() if f.is_file()]
    newest = day(max((f.stat().st_mtime for f in files), default=0))
    out.append(f"\n===== {res} ({len(files)} files, newest {newest}) =====")
    roles = {}
    for c in sorted((res / "crew").glob("*.json")):
        try:
            d = json.loads(c.read_text())
        except ValueError:
            continue
        role = re.sub(r"[-_]?[0-9].*$", "", str(d.get("role")))  # tests-003 and tests-004 count as one role
        roles.setdefault(f"{role} on {d.get('model')}", []).append(str(d.get("started_at"))[:16])
    out += [f"crew  {k}: {len(v)} started, {min(v)} to {max(v)}" for k, v in sorted(roles.items())]
    try:
        out.append(f"feature  {json.loads((res.parent / 'linear.json').read_text()).get('feature')}")
    except (OSError, ValueError):
        pass
    retros = []
    for l in tail(res / "retro.jsonl", 10**9, 10**6):
        try:
            retros.append(json.loads(l))
        except ValueError:
            pass
    if retros:
        kinds = sorted(collections.Counter(f"{r.get('status')}/{r.get('kind')}" for r in retros).items())
        out.append(f"--- retro.jsonl: {len(retros)} tasks ended without passing: " + ", ".join(f"{k} {n}" for k, n in kinds) + "; last 20")
        out += [f"{r.get('at')} {r.get('task')} {r.get('status')}/{r.get('kind')} x{r.get('attempts')} {r.get('model')}: "
                + " | ".join(map(str, r.get("lines") or []))[:1200] for r in retros[-20:]]
    for name, n in (("run.log", 12), ("verify.log", 12)):
        got = tail(res / name, n)
        if got:
            out += [f"--- {name} (last {n})"] + got

rows = []
for d in (H / ".claude/projects").glob("*"):
    fs = list(d.glob("*.jsonl"))
    if fs:
        rows.append((max(f.stat().st_mtime for f in fs), len(fs), sum(f.stat().st_size for f in fs) / 1e6, d.name))
out.append(f"\n===== Claude Code transcripts: {len(rows)} folders, {sum(r[2] for r in rows):.0f} MB; the 40 newest =====")
out += [f"{n:4} files {mb:8.1f} MB  {day(m)}  {name}" for m, n, mb, name in sorted(rows, reverse=True)[:40]]

text = re.sub(r"(sk-|lin_api_|ghp_|gho_|xox[bp]-)[A-Za-z0-9_-]{12,}", r"\1REDACTED", "\n".join(out))
print(text[:LIMIT] + (f"\n[cut at {LIMIT} characters]" if len(text) > LIMIT else ""))
EOF
)

if [ $# -gt 0 ]; then
  printf '%s\n' "$digest" > "$1"
  echo "wrote ${#digest} characters to $1"
else
  printf '%s\n' "$digest" | pbcopy
  echo "copied ${#digest} characters to the clipboard"
fi
