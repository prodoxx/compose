"""The delivery plan's section text: pure, section text in and section text out; fc_linear reads and writes Linear."""

import hashlib
import re

import fc_tasks

def heading_level(line):
    """2 for `## Notes`: the count of leading #s when a space follows them, else 0 (not a heading)."""
    n = len(line) - len(line.lstrip("#"))
    return n if 0 < n <= 6 and line[n:n + 1] == " " else 0

def headings(lines):
    """(index, level) of each heading line outside fenced code, where a `# comment` line is code, not a heading."""
    fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        elif not fence and heading_level(line):
            yield i, heading_level(line)

def compose_block_start(lines):
    """Index of the line that opens a task's `compose` block outside fenced code, else None."""
    fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            if not fence and line.startswith("```compose"):
                return i
            fence = not fence

def section_span(lines, heading):
    """The section's lines as (start, end): from its heading to the next heading of the same or a higher level, so an H2
    takes its H3s and an H3 leaves its siblings alone, or to the task's `compose` block, which is no section's text.
    None when the heading is not there."""
    marks = list(headings(lines))
    start = next((i for i, _ in marks if lines[i] == heading), None)
    if start is None:
        return None
    block = compose_block_start(lines)
    stop = block if block is not None and block > start else len(lines)
    return start, next((i for i, n in marks if start < i < stop and n <= heading_level(heading)), stop)

LINEAR_BULLET, LINEAR_NUMBER = re.compile(r"^([ \t]*)[-+*](?=[ \t])"), re.compile(r"^([ \t]*)\d+[.)](?=[ \t])")
LINEAR_RULE, LINEAR_HASHES, LINEAR_TASK = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}$"), re.compile(r"^(#{1,6} .*?)[ \t]+#+$"), re.compile(r"^([ \t]*\* )\[x\]")
LINEAR_CODE_SPAN, LINEAR_LINK = re.compile(r"(`+[^`]*`+)"), re.compile(r"\]\(<([^<>\s]*)>\)")
LINEAR_URL = re.compile(r"\[(https?://[^\]\s]+)\]\(<?\1>?\)|<(https?://[^<>\s]+)>")  # a bare address, which Linear stores as a link to itself
LINEAR_STRONG, LINEAR_EM = re.compile(r"(?<![\w\\])__(?=[^\s_])(.*?[^\s_])__(?!\w)"), re.compile(r"(?<![\w\\])_(?=[^\s_])([^_]*[^\s_])_(?!\w)")
LINEAR_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")

def _linear_inline(text):
    text = LINEAR_LINK.sub(r"](\1)", LINEAR_URL.sub(lambda m: m.group(1) or m.group(2), text))
    text = LINEAR_EM.sub(r"*\1*", LINEAR_STRONG.sub(r"**\1**", text))  # before the escapes go: `\_` is no emphasis
    return LINEAR_ESCAPE.sub(r"\1", text).replace("&amp;", "&")

def linear_markdown(text):
    """The text reduced to what Linear keeps of it, so two texts Linear stores alike read the same. Linear writes the markdown its own way
    on every save: `\\_` and `\\*` escapes, a bullet as `*`, a number as `1.`, `_em_` as `*em*`, a link target in `<>`, a bare address as a link
    to itself, `~~~` fences, a rule as `---`, closing `#`s dropped, `[x]` as `[X]`, blank lines wherever it likes. Code is left alone, and a
    blank line in it counts. Not covered: tables, setext headings, indented code (`same_words` is the net for those)."""
    out, fence = [], False
    for line in (l.rstrip() for l in (text or "").strip().split("\n")):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            out.append("```" + line.lstrip().lstrip("`~"))
        elif fence:
            out.append(line)
        elif line:
            if LINEAR_RULE.match(line):
                line = "---"
            else:
                line = LINEAR_TASK.sub(r"\1[X]", LINEAR_NUMBER.sub(r"\g<1>1.", LINEAR_BULLET.sub(r"\1*", LINEAR_HASHES.sub(r"\1", line))))
            out.append("".join(p if i % 2 else _linear_inline(p) for i, p in enumerate(LINEAR_CODE_SPAN.split(line))))
    return "\n".join(out)

def same_markdown(a, b):
    """Whether Linear stores the two texts alike, so writing one over the other changes nothing and Linear keeps serving the old."""
    return linear_markdown(a) == linear_markdown(b)

def same_words(a, b):
    """Whether the two texts hold the same words in the same order, whatever markup is around them: the check for a rewrite Linear
    makes that `linear_markdown` has not met yet. Whether a text was kept does not depend on how Linear punctuates it."""
    words = lambda t: re.findall(r"[^\W_]+", linear_markdown(t).lower())
    return words(a) == words(b)

def section_hash(body, heading):
    """A short hash of the section as it stands, subsections included; None when it is not there. A writer passes the hash it
    read to `--expect`, and the write refuses when the section changed since."""
    lines = body.split("\n")
    span = section_span(lines, heading)
    return hashlib.sha256("\n".join(lines[span[0]:span[1]]).strip().encode()).hexdigest()[:8] if span else None

def section_hashes(body):
    """(heading, hash) for each heading, first occurrence only: the one a section write finds."""
    lines = body.split("\n")
    names = list(dict.fromkeys(lines[i] for i, _ in headings(lines)))
    return [(h, section_hash(body, h)) for h in names]

def replace_section(body, heading, text):
    text = text.strip()
    first = text.split("\n")[0]
    new = first if heading_level(first) == heading_level(heading) else heading
    section = new + "\n\n" + text.removeprefix(new).strip() if text else ""
    lines = body.split("\n")
    span = section_span(lines, heading)
    if not span:
        return body.rstrip("\n") + "\n\n" + section + "\n" if section else body
    i, end = span
    parts = [p for p in ("\n".join(lines[:i]).rstrip("\n"), section.rstrip("\n"), "\n".join(lines[end:]).rstrip("\n")) if p]
    return "\n\n".join(parts) + "\n" if parts else ""

PLAN_SECTIONS = ("## Start today", "## Lanes", "## Waves", "## Critical path", "## Outside waits", "## How this maps to compose",
                 "## Priority when something has to give")
PLAN_NEXT = "Next: the planner writes this at its cut."

def plan_skeleton():
    return "\n\n".join(f"{h}\n\n{PLAN_NEXT}" for h in PLAN_SECTIONS) + "\n"

def with_plan_link(description, title, url):
    if url in description:
        return description
    line = f"* Delivery plan: [{title}]({url})"
    lines = description.split("\n")
    if span := section_span(lines, "## Notes"):
        i, end = span
        sub = next((j for j, _ in headings(lines) if i < j < end), end)
        own = [j for j in range(i + 1, sub) if lines[j].strip()]
        if own:
            ins = own[-1] + 1
            return "\n".join(lines[:ins] + [line] + lines[ins:])
        j = next((k for k in range(i + 1, end) if lines[k].strip()), end)
        return "\n".join(lines[:i + 1] + ["", line, ""] + lines[j:])
    return description.rstrip("\n") + "\n\n## Notes\n\n" + line + "\n"

def start_today(lanes):
    starts = [s for l in lanes for s in l["stories"] if s["state"].lower() in {"todo", "backlog"}
              and all(fc_tasks.blocker_finished(b["state"], b["labels"]) for b in s["blockers"])]
    if not starts:
        return "Nothing can start now."
    return "\n".join(f"{n}. [{s['id']}]({s['url']}) {s['title']}" for n, s in enumerate(starts, 1))

def lane_order(stories):
    """The lane's non-canceled stories, in-lane blockers first and ties by number: what lanes_text numbers."""
    stories = [s for s in stories if s["state"].lower() != "canceled"]
    mine = {s["id"] for s in stories}
    num = lambda s: int(s["id"].rsplit("-", 1)[1])
    done, order = set(), []
    while True:
        ready = [s for s in stories if s["id"] not in done
                 and all(b["id"] in done or b["id"] not in mine for b in s["blockers"])]
        if not ready:
            break
        s = min(ready, key=num)
        done.add(s["id"])
        order.append(s)
    return order + sorted((s for s in stories if s["id"] not in done), key=num)

def lanes_text(lanes):
    ids = {s["id"] for l in lanes for s in l["stories"] if s["state"].lower() != "canceled"}
    blocks = []
    for l in lanes:
        mine = {s["id"] for s in l["stories"] if s["state"].lower() != "canceled"}
        lines = []
        for n, s in enumerate(lane_order(l["stories"]), 1):
            lines.append(f"{n}. [{s['id']}]({s['url']}) {s['title']} ({s['state']})")
            needs = [b["id"] for b in s["blockers"] if b["id"] in ids and b["id"] not in mine]
            if needs:
                lines.append("   Needs: " + ", ".join(needs))
        blocks.append("\n".join([f"### [{l['id']}]({l['url']}) {l['title']}", ""] + (lines or ["No stories yet."])))
    return "\n\n".join(blocks)

def waves_text(lanes):
    stories = [s for l in lanes for s in l["stories"] if not fc_tasks.blocker_finished(s["state"], s["labels"])]
    if not stories:
        return "Nothing left to schedule."
    mine = {s["id"] for s in stories}
    num = lambda s: int(s["id"].rsplit("-", 1)[1])
    done, waves = set(), []
    while True:
        wave = sorted((s for s in stories if s["id"] not in done
                       and all(b["id"] in done or b["id"] not in mine for b in s["blockers"])), key=num)
        if not wave:
            break
        done.update(s["id"] for s in wave)
        waves.append(wave)
    rows = [f"| {n} | {', '.join(s['id'] for s in w)} | {len(w)} |" for n, w in enumerate(waves, 1)]
    return "\n".join(["| Wave | Stories | Count |", "| -- | -- | -- |"] + rows)

def critical_path_text(lanes):
    stories = [s for l in lanes for s in l["stories"] if not fc_tasks.blocker_finished(s["state"], s["labels"])]
    if not stories:
        return "Nothing left to schedule."
    mine = {s["id"] for s in stories}
    num = lambda i: int(i.rsplit("-", 1)[1])
    after = {s["id"]: {b["id"] for b in s["blockers"] if b["id"] in mine} for s in stories}
    blocks = {i: sorted((j for j in mine if i in after[j]), key=num) for i in mine}
    chains = []
    def walk(i, path):
        for j in blocks[i]:
            if j not in path:
                walk(j, path + [j])
        if not blocks[i]:
            chains.append(path)
    for i in sorted((i for i in mine if not after[i]), key=num):
        walk(i, [i])
    best = max(map(len, chains), default=0)
    if best < 2:
        return "No blocked-by chains."
    return "\n".join(f"{n}. {' -> '.join(c)} ({len(c)} stories)"
                     for n, c in enumerate((c for c in chains if len(c) == best), 1))

def plan_sections(lanes):
    return {"## Start today": start_today(lanes), "## Lanes": lanes_text(lanes), "## Waves": waves_text(lanes),
            "## Critical path": critical_path_text(lanes)}


PRODUCT_NEXT = "Next: written with the product owner, then kept in step by compose delivery reconcile."
TRACK_LINE = re.compile(r"^\d+\.\s+(?P<name>[^:]+?):\s+(?P<ids>.+?)(?:\s+\(after\s+(?P<after>(?:[^()]|\([^)]*\))+)\))?\s*$")
TRACK_ID = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")

class TracksError(ValueError):
    """The Tracks section does not parse; the message names the offending line."""

def split_entries(raw):
    """Comma separated entries, each `[X](url)` link reduced to X."""
    return [re.sub(r"^\[([^\]]*)\]\(.*\)$", r"\1", e.strip()) for e in raw.split(",")]

def parse_tracks(text):
    """The `## Tracks` body as [{"rank", "name", "items", "after"}] in list order; TracksError names the offending line."""
    tracks, lines = [], []
    seen_items, seen_names = {}, {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.strip() == PRODUCT_NEXT:
            continue
        m = TRACK_LINE.match(line.strip())
        items = split_entries(m.group("ids")) if m else []
        if not m or not all(TRACK_ID.match(i) for i in items):
            raise TracksError('line %d: not a track: "%s" (write "<n>. <name>: <id>, <id> (after <track>, ...)")' % (n, line.strip()))
        name = m.group("name").strip()
        if name.lower() in seen_names:
            raise TracksError("line %d: a second track named %s" % (n, name))
        for i in items:
            if items.count(i) > 1:
                raise TracksError("line %d: %s names %s twice" % (n, name, i))
            if i in seen_items:
                raise TracksError("line %d: %s names %s, already in track %s" % (n, name, i, seen_items[i]))
        seen_names[name.lower()] = len(tracks)
        seen_items.update({i: name for i in items})
        after = split_entries(m.group("after")) if m.group("after") else []
        tracks.append({"rank": int(line.strip().split(".")[0]), "name": name, "items": items, "after": after})
        lines.append(n)
    for t, n in zip(tracks, lines):
        for a in t["after"]:
            if TRACK_ID.match(a):
                if a not in seen_items:
                    raise TracksError("line %d: %s comes after %s, which no track names" % (n, t["name"], a))
                target = seen_items[a]
            elif a.lower() in seen_names:
                target = tracks[seen_names[a.lower()]]["name"]
            else:
                raise TracksError("line %d: %s comes after %s, which is no track here" % (n, t["name"], a))
            if target.lower() == t["name"].lower():
                raise TracksError("line %d: %s comes after itself" % (n, t["name"]))
    deps = track_deps(tracks)
    state, stack = {}, []
    def walk(i):
        state[i] = 1
        stack.append(i)
        for d in deps[i]:
            if state.get(d) == 1:
                cycle = stack[stack.index(d):] + [d]
                raise TracksError("the tracks wait on each other in a loop: " + " -> ".join(tracks[c]["name"] for c in cycle))
            if d not in state:
                walk(d)
        stack.pop()
        state[i] = 2
    for i in range(len(tracks)):
        if i not in state:
            walk(i)
    return tracks

def track_deps(tracks):
    """For each track index, the sorted indexes of the tracks it comes after."""
    by_name = {t["name"].lower(): i for i, t in enumerate(tracks)}
    by_item = {x: i for i, t in enumerate(tracks) for x in t["items"]}
    return [sorted({by_item[a] if TRACK_ID.match(a) else by_name[a.lower()] for a in t["after"]}) for t in tracks]

def track_waves(tracks):
    """Tracks grouped into waves: a track with nothing before it is in wave 1, any other one wave after its latest predecessor."""
    deps, depth = track_deps(tracks), {}
    def level(i):
        if i not in depth:
            depth[i] = 1 + max((level(d) for d in deps[i]), default=-1)
        return depth[i]
    waves = {}
    for i, t in enumerate(tracks):
        waves.setdefault(level(i), []).append(t)
    return [waves[k] for k in sorted(waves)]

def derived_blocks(tracks, skip=()):
    """{(blocker, blocked)}: each item blocks the next in its track; an `after` track's last item, or the `after` id, blocks this track's first.
    The ids in `skip` (unknown, Canceled, not a feature) are bridged over, so in `A, X, B` with X skipped A blocks B: the order never breaks through them."""
    by_name = {t["name"].lower(): t for t in tracks}
    before = {}
    for t in tracks:
        for b, k in zip(t["items"], t["items"][1:]):
            before.setdefault(k, set()).add(b)
        for a in t["after"]:
            before.setdefault(t["items"][0], set()).add(a if TRACK_ID.match(a) else by_name[a.lower()]["items"][-1])
    out = set()
    for k, bs in before.items():
        if k in skip:
            continue
        todo, seen = list(bs), set()
        while todo:
            b = todo.pop()
            if b not in seen:
                seen.add(b)
                if b in skip:
                    todo += before.get(b, ())
                else:
                    out.add((b, k))
    return out

def blocks_diff(derived, current, plan_ids):
    """(added, removed) relations to reach `derived`; `current` maps a blocked id to its blocker ids, and only pairs between plan_ids count."""
    now = {(b, k) for k, bs in current.items() for b in bs if b in plan_ids and k in plan_ids}
    def order(pair):
        return (int(pair[1].rsplit("-", 1)[1]), int(pair[0].rsplit("-", 1)[1]))
    return sorted(derived - now, key=order), sorted(now - derived, key=order)

def base_built(state, labels):
    """A base feature is built once it is finished, or In Review (verified)."""
    return fc_tasks.blocker_finished(state, labels) or state.lower() == "in review"

PRODUCT_SECTIONS = ("## Tracks", "## Why this order", "## Waves", "## Start today", "## Proposed fixes", "## History")
PRODUCT_DATA = ("## Waves", "## Start today", "## Proposed fixes")

def issue_num(ident):
    """The number in an issue identifier: NEB-12 is 12."""
    return int(ident.rsplit("-", 1)[1])

def _is_feature(f):
    return any(l.lower() == "feature" for l in f["labels"])

def _canceled(f):
    return f["state"].lower() in fc_tasks.CANCELED

def unusable_ids(tracks, features):
    """The track ids that get no blocks: unknown to Linear, Canceled, or not a feature."""
    return {i for t in tracks for i in t["items"] if i not in features or _canceled(features[i]) or not _is_feature(features[i])}

def plan_blocks(tracks, features):
    """The derived blocks over the usable items only: what Linear holds and what `order` waits on."""
    return derived_blocks(tracks, unusable_ids(tracks, features))

def _waits_on(item, blocks, features):
    """The feature's derived blockers that are not built yet, by number."""
    return sorted((b for b, k in blocks if k == item and not (b in features and base_built(features[b]["state"], features[b]["labels"]))),
                  key=issue_num)

def _ranked(tracks):
    """(wave, track) in build order: by wave, then list order, which is the priority (the numbers written do not count)."""
    return [(n, t) for n, w in enumerate(track_waves(tracks), 1) for t in w]

def order_entries(tracks, features):
    """What to build, in order: wave, then the track's place in the list, then its items, then a feature's stories in lane order."""
    blocks, out = plan_blocks(tracks, features), []
    for wave, t in _ranked(tracks):
        for item in t["items"]:
            f = features.get(item)
            if not f or not _is_feature(f) or _canceled(f) or fc_tasks.blocker_finished(f["state"], f["labels"]):
                continue
            waits = _waits_on(item, blocks, features)
            open_stories = [s for s in f.get("stories") or [] if not fc_tasks.blocker_finished(s["state"], s["labels"])]
            rows = [(s, "story") for s in lane_order(open_stories)] or [(f, "feature")]
            out += [{"id": s["id"], "title": s["title"], "state": s["state"], "kind": kind, "feature": item, "track": t["name"],
                     "wave": wave, "waits_on": list(waits)} for s, kind in rows]
    return out

def product_proposals(tracks, features):
    """Fixes to offer, never to apply: each says what is off and why."""
    blocks, started, rest = plan_blocks(tracks, features), [], []
    named = {i for t in tracks for i in t["items"]}
    for _, t in _ranked(tracks):
        for item in t["items"]:
            f = features.get(item)
            if f is None:
                rest.append(f"{item} is in track {t['name']} but Linear has no issue {item} in this project: drop it from the track.")
            elif _canceled(f):
                rest.append(f"{item} {f['title']} is Canceled but in track {t['name']}: drop it from the track, or reopen it.")
            elif not _is_feature(f):
                parent = f" ({f['parent']})" if f.get("parent") else ""
                rest.append(f"{item} {f['title']} in track {t['name']} is not a feature: name its feature{parent} instead, or label it Feature.")
            elif f["state"].lower() == "in progress" and (waits := _waits_on(item, blocks, features)):
                started.append(f"{item} {f['title']} is In Progress but waits on {', '.join(waits)} in the plan: "
                               "its started work goes on; hold it if it must wait.")
    unnamed = sorted((i for i, f in features.items() if i not in named and _is_feature(f) and not _canceled(f)
                      and not fc_tasks.blocker_finished(f["state"], f["labels"])), key=issue_num)
    return started + rest + [f"{i} {features[i]['title']} is an open feature the plan does not name: add it to a track, or cancel it."
                             for i in unnamed]

def product_skeleton():
    return "\n\n".join(f"{h}\n\n{PRODUCT_NEXT}" for h in PRODUCT_SECTIONS) + "\n"

def replace_product_section(body, heading, text):
    """replace_section, except that a product section the document lacks goes where the skeleton puts it, before the first later one there."""
    lines = body.split("\n")
    later = [section_span(lines, h) for h in PRODUCT_SECTIONS[PRODUCT_SECTIONS.index(heading) + 1:]] if heading in PRODUCT_SECTIONS else []
    later = [s[0] for s in later if s]
    section = replace_section("", heading, text).strip()
    if section_span(lines, heading) or not later or not section:
        return replace_section(body, heading, text)
    i = min(later)
    parts = [p for p in ("\n".join(lines[:i]).rstrip("\n"), section, "\n".join(lines[i:]).rstrip("\n")) if p]
    return "\n\n".join(parts) + "\n"

def _wave_item(item, features):
    f = features.get(item)
    if f is None:
        return f"{item} (not in Linear)"
    extra = f", [its plan]({f['plan_url']})" if f.get("plan_url") else ""
    return f"[{item}]({f['url']}) {f['title']} ({f['state']}{extra})"

def product_sections(tracks, features, ready, proposals):
    """The computed sections: Waves, Start today, Proposed fixes."""
    waves = []
    for n, group in enumerate(track_waves(tracks), 1):
        rows = []
        for t in group:
            after = f" (after {', '.join(t['after'])})" if t["after"] else ""
            rows.append(f"- {t['name']}{after}: " + " -> ".join(_wave_item(i, features) for i in t["items"]))
        waves.append(f"### Wave {n}\n\n" + "\n".join(rows))
    by_id = {e["id"]: e for e in order_entries(tracks, features)}
    starts = [by_id[i] for i in ready if i in by_id]
    return {"## Waves": "\n\n".join(waves) or "No tracks yet.",
            "## Start today": "\n".join(f"{n}. {e['id']} {e['title']} (feature {e['feature']}, track {e['track']})"
                                         for n, e in enumerate(starts, 1)) or "Nothing can start now.",
            "## Proposed fixes": "\n".join(f"{n}. {p}" for n, p in enumerate(proposals, 1)) or "None."}

def with_history(content, line):
    """Add a line to History: the first one replaces the placeholder, the rest follow."""
    lines = content.split("\n")
    span = section_span(lines, "## History")
    old = [l for l in lines[span[0] + 1:span[1]] if l.strip()] if span else []
    kept = [] if old == [PRODUCT_NEXT] else old
    return replace_section(content, "## History", "\n".join(["## History", ""] + kept + [line]))

def seed_tracks(features):
    """A `## Tracks` body guessed from the open features' feature-to-feature blocks."""
    openf = {i: f for i, f in features.items() if _is_feature(f) and not fc_tasks.blocker_finished(f["state"], f["labels"])}
    deps = {i: sorted({b["id"] for b in f["blockers"] if b["id"] in openf and b["id"] != i}, key=issue_num) for i, f in openf.items()}
    placed, tracks, home = set(), [], {}
    while len(placed) < len(openf):
        ready = [i for i in openf if i not in placed and all(d in placed for d in deps[i])]
        i = min(ready or [i for i in openf if i not in placed], key=issue_num)
        placed.add(i)
        host = home.get(deps[i][0]) if len(deps[i]) == 1 else None
        if host is not None and host["items"][-1] == deps[i][0]:
            host["items"].append(i)
        else:
            host = {"name": re.sub(r"\s+", " ", re.sub(r"[:,]", " -", re.sub(r"[()]", "", openf[i]["title"]))).strip() or i, "items": [i]}
            used = {t["name"].lower() for t in tracks}
            if host["name"].lower() in used:
                host["name"] += " " + i
            tracks.append(host)
        home[i] = host
    lines = []
    for n, t in enumerate(tracks, 1):
        after = []
        for d in sorted({d for i in t["items"][:1] for d in deps[i]}, key=issue_num):
            ref = home[d]["name"] if home[d]["items"][-1] == d else d
            if ref not in after and ref != t["name"]:
                after.append(ref)
        lines.append(f"{n}. {t['name']}: {', '.join(t['items'])}" + (f" (after {', '.join(after)})" if after else ""))
    return "\n".join(lines)
