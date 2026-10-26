"""Framelink node trees made self-contained for a judge: templates and styles inlined, variant properties named, hidden slots put back.
Stdlib only, no fc_ module."""
import copy, json, re


STYLE_KEYS = ("layout", "fills", "strokes", "effects", "textStyle")  # the fields Framelink moves into globalVars.styles
GENERATED_STYLE = re.compile(r"^[a-z]+_[0-9a-f]{8,}$")  # Framelink's own style keys; any other key is a named Figma style

def walk(nodes):
    """Every node of a Framelink node list, parents first; children are read after the parent is yielded, so edits to them are walked."""
    for n in nodes:
        yield n
        yield from walk(n.get("children") or [])

def put(node, key, value):
    """The node with key set to value, placed before its children so the node's own fields read first."""
    kids = node.pop("children", None)
    node[key] = value
    if kids is not None:
        node["children"] = kids
    return node

def inline_styles(doc):
    """Make every node of one Framelink document self-contained: a `template` node gets its element's fields back, and every style
    reference its value, so a node's strokes, stroke weight, and fills sit on the node itself and never in a context part a chunked
    judge request does not see. A named style keeps its name beside the value (`fillsStyle`). Styles no node references (rich-text
    `ts` styles) stay in globalVars."""
    elements, styles = doc.pop("elements", None) or {}, (doc.get("globalVars") or {}).get("styles") or {}
    used = set()
    for n in walk(doc.get("nodes") or []):
        body = elements.get(n.pop("template", None)) or {}
        for k, v in copy.deepcopy(body).items():
            put(n, k, v)
        for k in STYLE_KEYS:
            ref = n.get(k)
            if isinstance(ref, str) and ref in styles:
                used.add(ref)
                put(n, k, copy.deepcopy(styles[ref]))
                if not GENERATED_STYLE.match(ref):
                    put(n, f"{k}Style", ref)
    left = {k: v for k, v in styles.items() if k not in used}
    if left:
        doc["globalVars"]["styles"] = left
    else:
        doc.pop("globalVars", None)

def sub_id(prefix, node_id):
    """Figma's id for a main-component node seen inside the instance whose id (without its leading I) is prefix."""
    return f"I{prefix};{node_id.removeprefix('I')}"

def renamed(node, prefix):
    """A copy of a main-component subtree with the ids it has inside the instance."""
    out = {**copy.deepcopy({k: v for k, v in node.items() if k != "children"}), "id": sub_id(prefix, node["id"])}
    if node.get("children"):
        out["children"] = [renamed(c, prefix) for c in node["children"]]
    return out

def restore_hidden(inst, main, props, prefix):
    """Put back, with `"visible": false`, the children Framelink drops from an instance: it keeps a hidden child whose visibility is bound
    to a boolean property inside the main component, but drops it from every instance that has that property off. Matched children are
    walked down to the next nested instance, which is restored against its own main component."""
    kids = list(inst.get("children") or [])
    if not kids and any(not (c.get("componentPropertyReferences") or {}).get("visible") for c in main.get("children") or []):
        return  # cut by --depth, not hidden
    pos = 0
    for mc in main.get("children") or []:
        want = sub_id(prefix, mc["id"])
        at = next((i for i, k in enumerate(kids) if k.get("id") == want), None)
        if at is not None:
            pos = at + 1
            if mc.get("type") != "INSTANCE":
                restore_hidden(kids[at], mc, props, prefix)
        elif props.get((mc.get("componentPropertyReferences") or {}).get("visible")) is False:
            kids.insert(pos, put(renamed(mc, prefix), "visible", False))
            pos += 1
    if kids:
        inst["children"] = kids

def mark_instance_contents(nodes, instance=None):
    """Put `inInstance: <id of the nearest instance above>` on every node inside an instance. Framelink gives such a node nothing but an
    `I...;` id prefix, and a judge request holds a node without its ancestors, so a text or icon an instance brings reads as hand-drawn."""
    for n in nodes:
        if instance:
            put(n, "inInstance", instance)
        mark_instance_contents(n.get("children") or [], n["id"] if n.get("type") == "INSTANCE" else instance)

def self_contained(docs):
    """Framelink documents rewritten so a judge reads each node on its own: shared templates and styles inlined, every instance's variant
    properties (Framelink keeps only boolean and text ones; the variant is in its main component's name, `type=text, state=empty`) put in
    its componentProperties with its component set's name, and its hidden slots put back. Returns the main component ids not in the
    tree, whose instances cannot show their hidden slots."""
    for doc in docs:
        inline_styles(doc)
    comps = {k: v for d in docs for k, v in ((d.get("metadata") or {}).get("components") or {}).items()}
    sets = {k: v for d in docs for k, v in ((d.get("metadata") or {}).get("componentSets") or {}).items()}
    index = {n["id"]: n for d in docs for n in walk(d.get("nodes") or []) if "id" in n}
    absent = set()
    for n in (n for d in docs for n in walk(d.get("nodes") or [])):
        if n.get("type") != "INSTANCE" or not n.get("componentId"):
            continue
        comp = comps.get(n["componentId"]) or {}
        pairs = [p.partition("=") for p in comp.get("name", "").split(",")]
        if comp.get("componentSetId") and all(eq for _, eq, _ in pairs):
            put(n, "componentProperties", {**{k.strip(): v.strip() for k, _, v in pairs}, **(n.get("componentProperties") or {})})
            if sets.get(comp["componentSetId"], {}).get("name"):
                put(n, "componentSet", sets[comp["componentSetId"]]["name"])
        main = index.get(n["componentId"])
        if main:
            restore_hidden(n, main, n.get("componentProperties") or {}, n["id"].removeprefix("I"))
        else:
            absent.add(n["componentId"])
    mark_instance_contents([n for d in docs for n in d.get("nodes") or []])  # after the hidden slots are put back: they are contents too
    return sorted(absent)

NODE_ID = re.compile(r"I?\d+:\d+(?:;\d+:\d+)*")

def tree_node_ids(text):
    """(every node id, the ids marked `inInstance` or of type INSTANCE) of a saved tree file; a section that is not a JSON document holds none."""
    every, inside = set(), set()
    for sec in re.split(r"\n\n(?=NODE |WRITE RESULT)", text.strip("\n")):
        try:
            doc = json.loads(sec.partition("\n")[2])
        except ValueError:
            continue
        for n in walk(doc.get("nodes") or []) if isinstance(doc, dict) else []:
            if "id" in n:
                every.add(n["id"])
                if n.get("inInstance") or n.get("type") == "INSTANCE":  # an instance is no hand-drawn node either
                    inside.add(n["id"])
    return every, inside

def names_only_instance_contents(evidence, text):
    """True when `evidence` names tree nodes and every one of them is a component instance or inside one (marked `inInstance`)."""
    every, inside = tree_node_ids(text)
    named = {t for t in NODE_ID.findall(evidence) if t in every}
    return bool(named) and named <= inside


TREE_CHUNK_CHARS = 30000  # one judge request's share of a tree; the whole tree is judged in as many requests as it takes

def hard_split(text, limit):
    """Last resort for text that has no node boundary left: whole lines, a line longer than the limit cut by characters. Covers every character."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                out.append(cur); cur = ""
            out.append(line[:limit]); line = line[limit:]
        if len(cur) + len(line) > limit:
            out.append(cur); cur = ""
        cur += line
    return out + ([cur] if cur else [])

def split_node(head, node, path, limit):
    """Pieces of one Framelink node, each under the limit: the node whole, else its own fields followed by one run of pieces per child."""
    piece = f"{head}\n" + json.dumps({"under": path, "node": node}, indent=2)
    if len(piece) <= limit:
        return [piece]
    kids = node.get("children") if isinstance(node, dict) else None
    if not kids:
        return hard_split(piece, limit)
    own = {k: v for k, v in node.items() if k != "children"}
    own["children"] = f"{len(kids)} children, each in its own part below"
    here = path + [str(node.get("name") or node.get("id"))]
    out = split_node(head, own, path, limit)
    for kid in kids:
        out += split_node(head, kid, here, limit)
    return out

def tree_chunks(text, limit=TREE_CHUNK_CHARS):
    """The saved tree as texts of at most `limit` characters that together hold all of it, cut along node boundaries: the tree file's
    sections (NODE ..., WRITE RESULT ...) stay apart, an oversized section becomes its context (metadata, styles) and then each node,
    and a node too big for one part becomes its own fields and then each child. Adjacent small pieces share a chunk.
    The WRITE RESULT section (small: the only proof of a variable binding, which Framelink shows as plain hex) rides in every chunk."""
    if len(text) <= limit:
        return [text]
    secs = re.split(r"\n\n(?=NODE |WRITE RESULT)", text.strip("\n"))
    facts = "\n\n".join(s for s in secs if s.startswith("WRITE RESULT"))
    if facts and len(facts) <= limit // 2:
        secs = [s for s in secs if not s.startswith("WRITE RESULT")]
        return [c + "\n\n" + facts for c in tree_chunks("\n\n".join(secs), limit - len(facts) - 2)]
    pieces = []
    for sec in secs:
        if len(sec) <= limit:
            pieces.append(sec)
            continue
        head, _, body = sec.partition("\n")
        try:
            doc = json.loads(body)
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            pieces += hard_split(sec, limit)
            continue
        ctx = {k: v for k, v in doc.items() if k != "nodes"}
        pieces += hard_split(f"{head} (context)\n" + json.dumps(ctx, indent=2), limit) if ctx else []
        for n in doc.get("nodes") or []:
            pieces += split_node(f"{head} (part of a tree too large for one request)", n, [], limit)
    chunks = []
    for piece in pieces:
        if chunks and len(chunks[-1]) + len(piece) + 2 <= limit:
            chunks[-1] += "\n\n" + piece
        else:
            chunks.append(piece)
    return chunks
