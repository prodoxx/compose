"""A code task's Figma nodes as facts a build is held to: each element's fill, stroke, radius, padding, gap and type, read from the
Framelink JSON `run` kept, and `compose figma match`, which measures the page a bp browser shows against them (#496)."""
import json, os, re, subprocess, sys
import fc_core, fc_tasks, fc_figmanodes, fc_framelink


TOL_PX = 1.0       # of a padding, gap, radius, stroke weight or line height: a rounded rem is not a mismatch
TOL_FONT = 0.5     # of a font size
TOL_CHANNEL = 2    # of a colour channel (0-255): a colour converted from another colour space rounds
TOL_ALPHA = 0.02
ROWS_SHOWN = 20    # of a failed match on stdout: an e2e case keeps the last 3000 characters, and the whole table is in the file
MEASURED = re.compile(r"#[0-9a-fA-F]{6}\b|\b\d+(?:\.\d+)?\s?px\b")  # a criterion that states a value measured on a node: a hex colour or a px size
NOT_COMPARED = "sizes, positions, effects, images and icons are not compared"

def colour(value):
    """(r, g, b, a) of a Framelink colour, `#RRGGBB` or `rgba(r, g, b, a)`; None for an image, a gradient or anything else."""
    if not isinstance(value, str):
        return None
    if m := re.fullmatch(r"#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?", value.strip()):
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), round(int(m.group(2), 16) / 255, 2) if m.group(2) else 1.0)
    if m := re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", value.strip()):
        return (round(float(m.group(1))), round(float(m.group(2))), round(float(m.group(3))), float(m.group(4)) if m.group(4) else 1.0)
    return None

def solid(fills):
    """The one colour of a fills list when every fill is a plain colour (Framelink blends stacked solids into one); else None."""
    if not isinstance(fills, list) or not fills or not all(isinstance(f, str) for f in fills):
        return None
    return colour(fills[0])

def sides(value):
    """(top, right, bottom, left) in px of a CSS shorthand like `16px 24px`; None when it holds no px value."""
    v = [float(x) for x in re.findall(r"(-?\d+(?:\.\d+)?)px", str(value or ""))]
    if not v or len(v) > 4:
        return None
    return tuple(v) if len(v) == 4 else (v[0], v[1], v[2], v[1]) if len(v) == 3 else (v[0], v[-1], v[0], v[-1])

def show_colour(c):
    return "none" if not c or c[3] == 0 else "#%02X%02X%02X" % tuple(c[:3]) if c[3] >= 1 else f"rgba({c[0]}, {c[1]}, {c[2]}, {c[3]:g})"

def show_px(v):
    return "none" if v is None else f"{round(v, 2):g}px"

def show_sides(t):
    if t is None:
        return "none"
    a, b, c, d = t
    return show_px(a) if a == b == c == d else f"{show_px(a)} {show_px(b)}" if a == c and b == d else " ".join(show_px(x) for x in t)

def elements(doc):
    """Every visible node of a self-contained Framelink document, parents first: {id, name, type, parent (index or None), depth,
    text (a TEXT node's, else None), direction, variant, props}. `props` holds only what the design gives the node: fill, stroke
    and strokeWeight, radius, padding, gap for a frame; font, size, weight, lineHeight and colour for a text."""
    out = []
    def visit(node, parent, depth):
        if not isinstance(node, dict) or node.get("visible") is False:
            return
        layout = node["layout"] if isinstance(node.get("layout"), dict) else {}
        kids = [k for k in node.get("children") or [] if isinstance(k, dict) and k.get("visible") is not False]
        is_text, props = node.get("type") == "TEXT", {}
        fill = solid(node.get("fills"))
        if is_text:
            style = node["textStyle"] if isinstance(node.get("textStyle"), dict) else {}
            if style.get("fontFamily"):
                props["font"] = str(style["fontFamily"])
            size = style.get("fontSize") if isinstance(style.get("fontSize"), (int, float)) else None
            if size:
                props["size"] = float(size)
            if isinstance(style.get("fontWeight"), (int, float)):
                props["weight"] = int(style["fontWeight"])
            line = str(style.get("lineHeight") or "")
            if m := re.fullmatch(r"(\d+(?:\.\d+)?)(px|em)", line):
                if m.group(2) == "px" or size:
                    props["lineHeight"] = float(m.group(1)) * (1 if m.group(2) == "px" else size)
            if tint := fill or solid(style.get("fills")):
                props["colour"] = tint
        else:
            if fill and fill[3] > 0:
                props["fill"] = fill
            strokes = node.get("strokes")
            held = strokes if isinstance(strokes, dict) else {}
            tint = solid(held.get("colors") if held else strokes)
            weight = sides(held.get("strokeWeights") or node.get("strokeWeights") or held.get("strokeWeight") or node.get("strokeWeight"))
            if tint and weight and any(weight):
                props["stroke"], props["strokeWeight"] = tint, weight
            if (radius := sides(node.get("borderRadius"))) and any(radius):
                props["radius"] = radius
            if (pad := sides(layout.get("padding"))) and any(pad):
                props["padding"] = pad
            gap = sides(layout.get("gap"))
            if gap and gap[0] and layout.get("mode") in ("row", "column") and len(kids) > 1 and layout.get("justifyContent") != "space-between":
                props["gap"] = gap[0]
        variant = node.get("componentProperties") if isinstance(node.get("componentProperties"), dict) else None
        out.append({"id": str(node.get("id", "")), "name": str(node.get("name") or node.get("type") or "node"), "type": str(node.get("type") or ""),
                    "parent": parent, "depth": depth, "text": str(node.get("text") or "") if is_text else None,
                    "direction": layout.get("mode"), "variant": variant, "props": props})
        me = len(out) - 1
        for k in kids:
            visit(k, me, depth + 1)
    for n in doc.get("nodes") or []:
        visit(n, None, 0)
    return out

def shown(el):
    """{property: its value as the facts and a mismatch write it} for one element."""
    p, out = el["props"], {}
    if "fill" in p:
        out["fill"] = show_colour(p["fill"])
    if "stroke" in p:
        out["stroke"] = f"{show_colour(p['stroke'])} {show_sides(p['strokeWeight'])}"
    if "radius" in p:
        out["radius"] = show_sides(p["radius"])
    if "padding" in p:
        out["padding"] = show_sides(p["padding"])
    if "gap" in p:
        out["gap"] = show_px(p["gap"])
    if "font" in p:
        out["font"] = p["font"]
    if "size" in p:
        out["size"] = show_px(p["size"])
    if "weight" in p:
        out["weight"] = str(p["weight"])
    if "lineHeight" in p:
        out["line height"] = show_px(p["lineHeight"])
    if "colour" in p:
        out["colour"] = show_colour(p["colour"])
    return out

def label(el):
    return f"{el['name']} [{el['type']} {el['id']}]"

def fact_line(el):
    """One element as the judge reads it: its name, type and id, a text's words, each property the design gives it, an instance's variant."""
    parts = [f"{k} {v}" for k, v in shown(el).items()]
    if el["direction"] in ("row", "column"):
        parts.append(f"laid out as a {el['direction']}")
    if el["variant"]:
        parts.append("variant " + ", ".join(f"{k}={v}" for k, v in el["variant"].items()))
    return label(el) + (f" {json.dumps(el['text'])}" if el["text"] is not None else "") + (": " + "; ".join(parts) if parts else "")

def facts_text(els):
    """The node as indented lines, one element a line, children under their parent."""
    return "\n".join("  " * el["depth"] + fact_line(el) for el in els)

def node_path(proj, key, node):
    """Where a node's self-contained Framelink JSON is kept for verify: results/figma/nodes/<file key>-<node>.json."""
    return proj / "results" / "figma" / "nodes" / f"{key}-{node.replace(':', '-')}.json"

def task_docs(proj, meta, fresh=False):
    """[(node, document)] for each of a code task's `figma_nodes:`: the copies `run` kept when it wrote them into the task's clone, so
    verify holds the build to the design the implementer was given; fetched through Framelink, made self-contained and kept when one
    is missing or `fresh`. A task with no file, a failed fetch, or a fetch that is not JSON raises RuntimeError."""
    nodes = meta.get("figma_nodes") or []
    if not nodes:
        return []
    key, where = fc_framelink.node_file(proj, meta)
    if not key:
        raise RuntimeError("figma_nodes needs a Figma file: a figma: line on the task, or compose figma check --figma <url or key>")
    paths = [node_path(proj, key, n) for n in nodes]
    if not fresh and all(p.exists() for p in paths):
        try:
            return [(n, json.loads(p.read_text())) for n, p in zip(nodes, paths)]
        except ValueError:
            pass
    docs = []
    for node in nodes:
        try:
            docs.append(json.loads(fc_framelink.framelink_fetch(key, node=node.replace("-", ":"))))
        except ValueError:
            raise RuntimeError(f"framelink fetch of node {node} in file {key} (from {where}) is not JSON")
        except RuntimeError as e:
            raise RuntimeError(f"figma file {key} is {where}: {e}")
    fc_figmanodes.self_contained(docs)
    for path, doc in zip(paths, docs):
        path.parent.mkdir(parents=True, exist_ok=True)
        fc_core.atomic_write(path, json.dumps(doc, indent=2))
    return list(zip(nodes, docs))

# Every shown element under the root, parents first, with what the browser computed for it. A colour in any colour space (oklch from
# Tailwind 4) is read back as sRGB from a canvas; a ring drawn as a zero-offset box-shadow or an outline counts as a stroke.
MEASURE_JS = r"""(() => {
  const sel = __ROOT__;
  const root = sel ? document.querySelector(sel) : document.body;
  if (!root) return JSON.stringify({ error: 'no element matches ' + sel, url: location.href });
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', { willReadFrequently: true });
  const seen = new Map();
  const rgba = (c) => {
    if (!c) return null;
    if (seen.has(c)) return seen.get(c);
    let v; const m = c.match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,\/]\s*([\d.]+))?\s*\)$/);
    if (m) v = [Math.round(+m[1]), Math.round(+m[2]), Math.round(+m[3]), m[4] === undefined ? 1 : +m[4]];
    else { cx.clearRect(0, 0, 1, 1); cx.fillStyle = '#000'; cx.fillStyle = c; cx.fillRect(0, 0, 1, 1); const d = cx.getImageData(0, 0, 1, 1).data; v = [d[0], d[1], d[2], Math.round(d[3] / 255 * 100) / 100]; }
    seen.set(c, v); return v;
  };
  const px = (v) => parseFloat(v) || 0;
  const rings = (sh) => {
    if (!sh || sh === 'none') return [];
    const out = [];
    for (const part of sh.split(/,(?![^(]*\))/)) {
      const m = part.trim().match(/^(.*?)\s*(-?[\d.]+)px\s+(-?[\d.]+)px\s+(-?[\d.]+)px\s+(-?[\d.]+)px(\s+inset)?$/);
      if (m && +m[2] === 0 && +m[3] === 0 && +m[4] === 0 && +m[5] > 0) { const c = rgba(m[1]); if (c && c[3] > 0) out.push([+m[5], c]); }
    }
    return out;
  };
  const els = [];
  const walk = (el, p) => {
    if (els.length >= 4000) return;
    const cs = getComputedStyle(el);
    if (cs.display === 'none') return;
    const r = el.getBoundingClientRect();
    let i = p;
    if (r.width > 0 && r.height > 0 && cs.visibility !== 'hidden') {
      i = els.length;
      const tag = el.tagName.toLowerCase();
      let own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(' ');
      if (tag === 'input' || tag === 'textarea') own = el.placeholder || (['button', 'submit', 'reset'].includes(el.type) ? el.value : '');
      const side = (s) => cs['border' + s + 'Style'] === 'none' ? 0 : px(cs['border' + s + 'Width']);
      const bw = [side('Top'), side('Right'), side('Bottom'), side('Left')];
      const first = ['Top', 'Right', 'Bottom', 'Left'][bw.findIndex((w) => w > 0)];
      const half = Math.min(r.width, r.height) / 2;
      const rad = (v) => Math.min(half, v.trim().split(' ')[0].endsWith('%') ? parseFloat(v) / 100 * Math.min(r.width, r.height) : px(v));
      els.push({ p, tag, cls: typeof el.className === 'string' ? el.className.trim().split(/\s+/).slice(0, 4).join('.') : '', own, text: (el.innerText || el.textContent || own || '').slice(0, 2000),
        rect: [r.left, r.top, r.width, r.height], bg: rgba(cs.backgroundColor), color: rgba(cs.color), bw, bc: first ? rgba(cs['border' + first + 'Color']) : null,
        outline: cs.outlineStyle !== 'none' && px(cs.outlineWidth) > 0 ? [px(cs.outlineWidth), rgba(cs.outlineColor)] : null, rings: rings(cs.boxShadow),
        radius: [rad(cs.borderTopLeftRadius), rad(cs.borderTopRightRadius), rad(cs.borderBottomRightRadius), rad(cs.borderBottomLeftRadius)],
        pad: [px(cs.paddingTop), px(cs.paddingRight), px(cs.paddingBottom), px(cs.paddingLeft)], out: cs.position === 'absolute' || cs.position === 'fixed',
        ff: cs.fontFamily, fs: px(cs.fontSize), fw: parseInt(cs.fontWeight, 10) || 400, lh: cs.lineHeight === 'normal' ? null : px(cs.lineHeight) });
      if (tag === 'svg') return;
    }
    for (const k of [...(el.shadowRoot ? el.shadowRoot.children : []), ...el.children]) walk(k, i);
  };
  walk(root, -1);
  return JSON.stringify({ url: location.href, title: document.title, els });
})()"""

def norm(text):
    return " ".join(str(text or "").split()).casefold()

def near(a, b, tol):
    return a is not None and b is not None and abs(a - b) <= tol

def same_colour(a, b):
    return bool(a and b) and all(abs(x - y) <= TOL_CHANNEL for x, y in zip(a[:3], b[:3])) and abs(a[3] - b[3]) <= TOL_ALPHA

def same_sides(a, b, tol=TOL_PX):
    return a is not None and b is not None and all(abs(x - y) <= tol for x, y in zip(a, b))

def built_stroke(e, want):
    """(colour, weights) of the stroke an element shows: its border, else a ring (a zero-offset box-shadow) or an outline, of `want`'s
    colour when it has one."""
    if any(e["bw"]):
        return e["bc"], tuple(e["bw"])
    drawn = [(c, (w,) * 4) for w, c in e.get("rings") or []] + ([(e["outline"][1], (e["outline"][0],) * 4)] if e.get("outline") else [])
    return next((d for d in drawn if same_colour(d[0], want)), drawn[0] if drawn else (None, None))

def built_gaps(dom, kids, i, direction):
    """The gaps between the element's children in flow, along the design's direction, however the build makes them (gap, margins);
    None with fewer than two children."""
    flow = sorted((dom[k]["rect"] for k in kids.get(i, []) if not dom[k].get("out")), key=lambda r: r[1 if direction == "column" else 0])
    at, size = (1, 3) if direction == "column" else (0, 2)
    return [b[at] - (a[at] + a[size]) for a, b in zip(flow, flow[1:])] if len(flow) > 1 else None

def frame_checks(el, e, gaps):
    """[(property, design, built, passes)] for a frame's properties on one built element; `passes` None when the build cannot show it."""
    p, out = el["props"], []
    if "fill" in p:
        out.append(("fill", show_colour(p["fill"]), show_colour(e["bg"]), same_colour(p["fill"], e["bg"])))
    if "stroke" in p:
        tint, weights = built_stroke(e, p["stroke"])
        out.append(("stroke", f"{show_colour(p['stroke'])} {show_sides(p['strokeWeight'])}", "none" if not weights else f"{show_colour(tint)} {show_sides(weights)}",
                    same_colour(p["stroke"], tint) and same_sides(p["strokeWeight"], weights)))
    if "radius" in p:
        cap = min(e["rect"][2], e["rect"][3]) / 2  # a pill's 9999px is the half height the browser clamps it to
        out.append(("radius", show_sides(p["radius"]), show_sides(tuple(e["radius"])), same_sides(tuple(min(r, cap) for r in p["radius"]), tuple(e["radius"]))))
    if "padding" in p:
        out.append(("padding", show_sides(p["padding"]), show_sides(tuple(e["pad"])), same_sides(p["padding"], tuple(e["pad"]))))
    if "gap" in p:
        out.append(("gap", show_px(p["gap"]), "one child" if gaps is None else ", ".join(dict.fromkeys(show_px(round(g)) for g in gaps)),
                    None if gaps is None else all(near(g, p["gap"], TOL_PX) for g in gaps)))
    return out

def text_checks(el, e):
    """[(property, design, built, passes)] for a text's type on the element that shows it."""
    p, out = el["props"], []
    if "font" in p:
        squash = lambda s: re.sub(r"[^a-z0-9]", "", s.casefold())
        out.append(("font", p["font"], e["ff"].split(",")[0].strip(" \"'"), any(squash(p["font"]) in squash(f) for f in e["ff"].split(","))))
    if "size" in p:
        out.append(("size", show_px(p["size"]), show_px(e["fs"]), near(p["size"], e["fs"], TOL_FONT)))
    if "weight" in p:
        out.append(("weight", str(p["weight"]), str(e["fw"]), p["weight"] == e["fw"]))
    if "lineHeight" in p:
        out.append(("line height", show_px(p["lineHeight"]), "normal" if e["lh"] is None else show_px(e["lh"]), near(p["lineHeight"], e["lh"], TOL_PX)))
    if "colour" in p:
        out.append(("colour", show_colour(p["colour"]), show_colour(e["color"]), same_colour(p["colour"], e["color"])))
    return out

def compare(els, dom):
    """(rows, stats) of one node's elements against the measured page. A TEXT node is the built element that shows its words; a frame
    is, of the built elements that hold every text under it and no other matched text, the one that carries most of its properties
    (the innermost on a tie), so one built element may stand for a frame and the wrapper frame around it. A row is a property that
    differs: {id, element, property, design, built, at}. stats: compared, matched, texts (found, of), unmeasured (frames with no text
    to find them by) and unshown (properties the build has too few children to show)."""
    d = dom["els"]
    ups = lambda i: [] if i < 0 else [i, *ups(d[i]["p"])]
    kids = {}
    for i, e in enumerate(d):
        kids.setdefault(e["p"], []).append(i)
    by_own, by_all = {}, {}
    for i, e in enumerate(d):
        if norm(e["own"]):
            by_own.setdefault(norm(e["own"]), []).append(i)
        if norm(e["text"]):
            by_all.setdefault(norm(e["text"]), []).append(i)
    def candidates(text):
        if text in by_own:
            return by_own[text]
        hits = by_all.get(text, [])
        outer = {d[i]["p"] for i in hits}
        return [i for i in hits if i not in outer]
    at = lambda i: f"<{d[i]['tag']}{'.' + d[i]['cls'] if d[i]['cls'] else ''}>"[:60]
    rows, shows, used, compared, matched, unshown = [], {}, set(), 0, 0, 0
    wanted = [k for k, el in enumerate(els) if el["text"] is not None and norm(el["text"])]
    for k in wanted:
        hit = next((i for i in candidates(norm(els[k]["text"])) if i not in used), None)
        if hit is None:
            rows.append({"id": els[k]["id"], "element": label(els[k]), "property": "text", "design": json.dumps(els[k]["text"][:60]), "built": "not on the page", "at": ""})
            continue
        used.add(hit)
        shows[k] = hit
    under = {}  # built element -> the matched texts it holds
    for hit in shows.values():
        for i in ups(hit):
            under.setdefault(i, set()).add(hit)
    holds = {}  # design element -> the built elements of the texts under it
    for k, hit in shows.items():
        j = k
        while j is not None:
            holds.setdefault(j, set()).add(hit)
            j = els[j]["parent"]
    unmeasured = []
    for k, el in enumerate(els):
        if not el["props"]:
            continue
        if el["text"] is not None:
            checks, where = (text_checks(el, d[shows[k]]), shows[k]) if k in shows else ([], None)
        elif not holds.get(k):
            unmeasured.append(label(el))
            continue
        else:
            mine = holds[k]
            chain = [i for i in ups(next(iter(mine))) if mine <= under.get(i, set())]  # from their lowest common ancestor up
            fits = [i for i in chain if under[i] <= mine and d[i]["tag"] not in ("html", "body")] or [i for i in chain[:1] if d[i]["tag"] == "body"]
            if not fits:  # its texts sit beside another frame's in one built element: the build gave it no element of its own to measure
                unmeasured.append(label(el))
                continue
            tried = [(frame_checks(el, d[i], built_gaps(d, kids, i, el["direction"])), i) for i in fits]
            checks, where = max(tried, key=lambda t: sum(c[3] is True for c in t[0]))  # max keeps the first, the innermost, on a tie
        for prop, design, built, passes in checks:
            if passes is None:
                unshown += 1
                continue
            compared += 1
            matched += bool(passes)
            if not passes:
                rows.append({"id": el["id"], "element": label(el), "property": prop, "design": design, "built": built, "at": at(where)})
    compared += len(wanted)
    matched += len(shows)
    return rows, {"compared": compared, "matched": matched, "texts": [len(shows), len(wanted)], "unmeasured": unmeasured, "unshown": unshown}

def table(rows):
    """The mismatches as aligned columns: element, property, design, built, built element."""
    head = ("element", "property", "design", "built", "built element")
    lines = [head] + [(r["element"][:44], r["property"], r["design"][:28], r["built"][:28], r["at"]) for r in rows]
    widths = [max(len(l[c]) for l in lines) for c in range(4)]
    return "\n".join("  ".join(v.ljust(w) for v, w in zip(l[:4], widths)) + "  " + l[4] for l in lines)

def bp(args, timeout=60):
    """One bp command against the browser on BP_PORT; its stdout, or RuntimeError with what it printed."""
    try:
        p = subprocess.run(["bp", *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"bp {args[0]} failed: {e}")
    if p.returncode:
        raise RuntimeError(f"bp {args[0]} failed: {(p.stderr or p.stdout).strip()[-400:]}")
    return p.stdout

def pages(page=None):
    """The page ids to measure: `page` (an id or a part of its url), else every open page that shows a site."""
    if page:
        return [page]
    found = [l.split() for l in bp(["list"]).splitlines() if l.startswith("page")]
    return [p[1] for p in found if len(p) > 2 and not p[2].startswith(("about:", "chrome", "devtools"))]

def measure(page, root=None):
    """What the page shows, as MEASURE_JS reads it; RuntimeError when the page or the root cannot be read."""
    try:
        dom = json.loads(bp(["eval", page, MEASURE_JS.replace("__ROOT__", json.dumps(root or ""))]))
    except ValueError:
        raise RuntimeError(f"bp eval on page {page} did not return the measured page")
    if dom.get("error"):
        raise RuntimeError(f"{dom['error']} on {dom.get('url')}")
    return dom

def figma_match(a, proj):
    """`compose figma match --only <task> | --node <ids>`: measure the page of the bp browser on BP_PORT against the nodes and print
    each property that differs. It is an e2e case's `assert`: it exits 1 on any mismatch, and prints `ASSERT FAILED` when under half the
    texts of a node are on the page (the feature is not built, which `verify --prove` expects before `run`). The whole table goes to
    results/design/<task or nodes>.match.txt."""
    if not os.environ.get("BP_PORT"):
        sys.exit("figma match measures the page of a bp browser: set BP_PORT to its port (an e2e case's assert has it; else `bp launch <url>` first)")
    meta = {"id": None, "figma_nodes": []}
    if a.only:
        meta = next((m for m in fc_tasks.load_tasks(proj, required=True) if m["id"] == a.only), None) or sys.exit(f"unknown task {a.only}")
    if a.node:
        meta = {**meta, "figma_nodes": [n.strip() for n in a.node.split(",") if n.strip()]}
    if a.figma:
        meta = {**meta, "figma": fc_core.figma_key(a.figma) or sys.exit(f"not a Figma file url or key: {a.figma}")}
    if not meta["figma_nodes"]:
        sys.exit(f"task {a.only} has no figma_nodes: line: name the nodes with --node <ids>" if a.only else "figma match needs --only <task with figma_nodes:> or --node <node ids>")
    try:
        docs = task_docs(proj, meta)
        doms = [measure(p, a.root) for p in pages(a.page)]
    except RuntimeError as e:
        sys.exit(f"figma match: {e}")
    if not doms:
        sys.exit("figma match: the browser on BP_PORT shows no page")
    stem = meta["id"] or "-".join(n.replace(":", "-") for n in meta["figma_nodes"])[:80]
    out, bad, short, whole = proj / "results" / "design" / f"{stem}.match.txt", False, [], []
    for node, doc in docs:
        els = elements(doc)
        rows, stats, dom = max(((*compare(els, dom), dom) for dom in doms), key=lambda t: t[1]["texts"][0])  # the page that shows most of the node's words
        found, of = stats["texts"]
        bad = bad or found * 2 < of or not of or bool(rows)
        if not of:
            line, rows = f"figma match node {node}: FAIL  the node has no text, so nothing on the page can be found as its elements: drop this case and compare the node by eye", []
        elif found * 2 < of:  # a stub page with its heading alone is the feature missing, as `verify --prove` expects before `run`
            line, rows = f"ASSERT FAILED: {found} of the {of} texts of node {node} are on the page {dom['url']}, so the node is not built there", []
        else:
            differ = stats["compared"] - stats["matched"]
            notes = ([f"{len(stats['unmeasured'])} element(s) could not be found by a text of their own: {', '.join(stats['unmeasured'][:6])}"] if stats["unmeasured"] else []) + [NOT_COMPARED]
            line = (f"figma match node {node} on {dom['url']}: {'FAIL' if differ else 'ok'}  {stats['matched']} of {stats['compared']} properties match"
                    + (f", {differ} differ" if differ else "") + f" ({'; '.join(notes)})")
        short.append((table(rows[:ROWS_SHOWN]) + "\n" + (f"... and {len(rows) - ROWS_SHOWN} more\n" if len(rows) > ROWS_SHOWN else "") if rows else "") + line)
        whole.append((table(rows) + "\n" if rows else "") + line)
    out.parent.mkdir(parents=True, exist_ok=True)
    fc_core.atomic_write(out, "\n\n".join(whole) + "\n")
    print("\n\n".join(short))
    if bad:
        print(f"the whole table: {fc_core.tilde(str(out))}")
        sys.exit(1)
