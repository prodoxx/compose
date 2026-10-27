import fcntl, json, os, signal, subprocess, time
from pathlib import Path
import fc_worktree, fc_crew, fc_load, fc_started

EMPTY = {"hold": None, "freed": [], "hot": None, "paused": []}

def home():
    return Path(os.path.expanduser("~")) / ".compose"

def state():
    try:
        return json.loads((home() / "shed.json").read_text())
    except (OSError, ValueError):
        return {**EMPTY, "freed": [], "paused": []}

def table():
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pcpu=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        p = line.split(None, 3)
        if len(p) == 4:
            rows.append((int(p[0]), int(p[1]), float(p[2]), p[3]))
    return rows

def tree(pid, table):
    found = [pid]
    for p, pp, _, _ in table:
        if pp in found and p not in found:
            found.append(p)
    return found

def hottest(table, ours):
    rows = [r for r in table if r[0] not in ours]
    return max(rows, key=lambda r: r[2]) if rows else None

def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

def signal_tree(pids, sig):
    for p in pids:
        try:
            os.kill(p, sig)
        except (ProcessLookupError, PermissionError):  # exited since the ps read, or not ours: the rest of the tree still gets it
            pass

def why_freeable(r):
    if r["waiter"] and not alive(r["waiter"]):
        return "waiter gone"
    if r["project"] and not (Path(r["project"]) / "verify.json").exists():
        return "project ended"
    if r["until"] and r["until"] < time.time():
        return "past its limit"

def tick():
    if fc_load.off():  # the load gate switched off holds nothing, so there is nothing to shed
        return []
    d = home()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "shed.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return []
        s = state()
        st = fc_load.update()
        held = bool(st and st["held"])
        if not held and not s["paused"]:
            s["hold"] = s["hot"] = None
            fc_load.write(d / "shed.json", json.dumps(s))
            return []
        lines = []
        if held:
            if s["hold"] is None:
                s["hold"] = {"n": st.get("n", 1), "at": st.get("at", time.time())}
            rows = table()
            recs = fc_started.entries()
            ours = set()
            for r in recs:
                ours.update(tree(r["pid"], rows))
            for r in recs:
                name = os.path.basename(r["project"]) if r["project"] else "-"
                if not r["alive"]:
                    fc_started.forget(r["pid"])
                    cwd = r["cwd"]
                    if r["kind"] == "teammate" and cwd and not any(o["kind"] == "teammate" and o["alive"] and o["cwd"] == cwd for o in recs):
                        fc_worktree.stop_listeners_under(cwd)
                        why = "dev server of a done crew"
                        s["freed"].append({"pid": r["pid"], "kind": r["kind"], "project": name, "why": why, "at": time.time()})
                        lines.append(f"event shed=freed pid={r['pid']} kind={r['kind']} project={name} why={why}")
                    continue
                why = why_freeable(r)
                pids = tree(r["pid"], rows)
                if not why or any("compose verify" in c for p, _, _, c in rows if p in pids):
                    continue
                signal_tree(pids, signal.SIGTERM)
                fc_started.forget(r["pid"])
                s["freed"].append({"pid": r["pid"], "kind": r["kind"], "project": name, "why": why, "at": time.time()})
                lines.append(f"event shed=freed pid={r['pid']} kind={r['kind']} project={name} why={why}")
            if s["hot"] is None and (row := hottest(rows, ours)):
                cmd = os.path.basename(row[3].split()[0])
                s["hot"] = {"pid": row[0], "cpu": row[2], "command": cmd}
                lines.append(f"event shed=hot pid={row[0]} cpu={row[2]:.0f} command={cmd} (not started by compose, left alone)")
            now, step, low = time.time(), s.get("step_at"), fc_load.limits()["low"]
            if (st["memory"] == 4 or now - s["hold"]["at"] >= 120) and (step is None or now - step >= 60):
                done = {e["pid"] for e in s["paused"]}
                for r in sorted(recs, key=lambda r: r["at"], reverse=True):
                    pids = tree(r["pid"], rows)
                    if (r["alive"] and r["kind"] in ("implementer", "teammate") and r["pid"] not in done
                            and not any("compose verify" in c for p, _, _, c in rows if p in pids)):
                        name = os.path.basename(r["project"]) if r["project"] else "-"
                        signal_tree(pids, signal.SIGSTOP)
                        s["paused"].append({"pid": r["pid"], "kind": r["kind"], "project": name, "at": now})
                        s["step_at"] = now
                        lines.append(f"event shed=paused pid={r['pid']} kind={r['kind']} project={name}  (resumes in reverse once the load falls below {low:.1f})")
                        break
        else:
            s["hold"] = s["hot"] = None
            now, step, low = time.time(), s.get("step_at"), fc_load.limits()["low"]
            if s["paused"] and st["avg"] < low and (step == s["paused"][-1]["at"] or now - step >= 60):
                e = s["paused"].pop()
                signal_tree(tree(e["pid"], table()), signal.SIGCONT)
                s["step_at"] = now
                lines.append(f"event shed=resumed pid={e['pid']} kind={e['kind']} project={e['project']}")
        fc_load.write(d / "shed.json", json.dumps(s))
        return lines
