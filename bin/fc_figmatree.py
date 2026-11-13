"""`compose figma tree`: the Framelink tree of nodes saved as a design task's gate evidence, or read for any project, a code project's included."""
import hashlib, json, re, sys
from pathlib import Path
import fc_core, fc_tasks, fc_framelink, fc_figmanodes, fc_figmashots


def caller_projects(repo):
    """The compose projects whose worktree `repo` is in: who is running the command. Empty in a checkout that holds none (a manager in the
    main checkout, a teammate's own worktree), where no project is the caller's."""
    c = fc_core.checkout(repo)
    top = c.top if c else Path(repo).resolve()
    return [name for name, (wt, _) in fc_core.all_projects(top).items() if Path(wt).resolve() == top]

def tree_stem(nodes):
    """A file name for a read of `nodes`: their ids, `233-3_233-55`, shortened with a hash when they would make it long."""
    stem = "_".join(re.sub(r"[^0-9A-Za-z]+", "-", n) for n in nodes)
    return stem if len(stem) <= 80 else stem[:60] + "-" + hashlib.sha1(stem.encode()).hexdigest()[:8]

def figma_tree(a, proj, mine=()):
    """Save the Framelink tree of --node (comma-separated ids), every node self-contained, then the write script's --bindings text.
    For a design task --only it is the task's gate evidence, `results/NNN.tree.txt` with the screens of the nodes in `results/NNN.screens/`,
    which only that task's project may replace (`mine`: the projects of the caller's worktree; --force overrides). Otherwise it is a
    read for any project, a code project's included, saved to --out (default `results/figma/<nodes>.tree.txt` in the project) with screens
    only when --screens asks, in `<out minus .tree.txt>.screens`. The file key is --figma, else the --only task's, else linear.json's."""
    if not a.node:
        sys.exit("figma tree needs --node <node ids>: with --only <design task> it saves that task's tree, else it reads them for any project")
    meta = None
    if a.only:
        meta = next((m for m in fc_tasks.load_tasks(proj, required=True) if m["id"] == a.only), None)
        if not meta:
            sys.exit(f"unknown task {a.only}")
    gate = bool(meta and meta.get("kind") == "design" and not a.out)
    stored = json.loads((proj / "linear.json").read_text()) if (proj / "linear.json").exists() else {}
    key = fc_core.figma_key(a.figma) if a.figma else (meta or {}).get("figma") or stored.get("figma")
    if a.figma and not key:
        sys.exit(f"not a Figma file url or key: {a.figma}")
    if not key:
        sys.exit("no Figma file: compose figma check --figma <url or key>, or pass --figma <url or key>")
    nodes = [n.strip() for n in a.node.split(",") if n.strip()]
    if gate:
        out = proj / "results" / f"{a.only}.tree.txt"
        stem = a.only
    else:
        out = Path(a.out).expanduser().resolve() if a.out else proj / "results" / "figma" / f"{tree_stem(nodes)}.tree.txt"
        stem = out.name[:-len(".tree.txt")] if out.name.endswith(".tree.txt") else out.stem
    shots_dir = out.with_name(f"{stem}.screens")
    if out.is_dir():
        sys.exit(f"--out {out} is a directory: name the file")
    if gate and mine and proj.name not in mine and not getattr(a, "force", False):
        sys.exit(f"{a.only} is a design task of {proj.name}, and this worktree is {' and '.join(mine)}'s: saving would replace "
                 f"{out} and {shots_dir} with these nodes alone. To read nodes for your own project, leave out --only: "
                 f"compose figma tree --node <ids> [--figma <key>] [--screens] [--out <file>]; pass --force to replace {proj.name}'s anyway")
    fetched = []
    for node in nodes:
        try:
            fetched.append((node, fc_framelink.framelink_fetch(key, node=node.replace("-", ":"), depth=a.depth)))
        except RuntimeError as e:
            sys.exit(str(e))
    docs = {}
    for node, text in fetched:
        try:
            doc = json.loads(text)
        except ValueError:
            continue
        if isinstance(doc, dict) and isinstance(doc.get("nodes"), list):
            docs[node] = doc
    shots, notes = [], []
    if gate or a.screens:
        try:  # rendered before the tree is written, so a tree on disk always has the screens of the same fetch
            shots, notes = fc_figmashots.save_screens(key, [node.replace("-", ":") for node, _ in fetched], shots_dir)
        except RuntimeError as e:
            sys.exit(f"no screens of {a.only or ', '.join(nodes)}: {e}")
    absent = fc_figmanodes.self_contained(list(docs.values()))
    parts = [f"NODE {node}\n" + (json.dumps(docs[node], indent=2) if node in docs else text) for node, text in fetched]
    if a.bindings:
        extra = sys.stdin.read() if a.bindings == "-" else Path(a.bindings).read_text()
        parts.append("WRITE RESULT (facts the write script returned: node ids, variable bindings, component keys)\n" + extra.strip())
    out.parent.mkdir(parents=True, exist_ok=True)
    fc_core.atomic_write(out, "\n\n".join(parts) + "\n")
    print(f"saved {out}" + (f" and {len(shots)} screen{'s' * (len(shots) != 1)} in {shots_dir}" if shots else ""))
    for note in notes:
        print(note)
    if absent:
        print(f"instances of {', '.join(absent)} show no hidden slots: their main components are not in the tree; add the local ones to --node")
