"""Team labels by name, in either of the two shapes a Linear workspace keeps a type in: a standalone label (`Story`), or a child
of the single-select `Type` label group (`Type > Story`). Some teams have one shape, some both while they migrate.

A bare name a write needs resolves in this order: the `Type` group's child of that name, else the standalone label, else
(today's lookup, for a name no team keeps as a type) a child of any other group. A qualified name, `"Type > Story"`, is exactly
that child. The optional `types` map in `.compose/linear.json` names the label a type is written as (`{"task": "Type > Tech
Task"}`); there a bare name is exactly the standalone label, the way out for a team that wants it on purpose, and a null or
missing entry resolves by the order above. An issue's labels come back from Linear by their own name, `Story` in either shape,
so the readers of a type (`fc_layout`, the Ready marks, `fc_readiness`, ...) accept both by name."""
import json

import fc_core

TYPE_GROUP = "type"  # the label group a workspace keeps its types in, matched case-insensitively
TYPES = ("epic", "feature", "story", "task")  # the types compose writes and reads
SEP = ">"
# The team's labels as every resolving read asks for them: a child's group, and whether a group takes one child per issue.
TEAM_LABELS = "labels(first: 250) { nodes { id name isGroup groupType parent { id name } } }"
ISSUE_LABELS = "labels { nodes { id name parent { id name } } }"  # an issue's own, for the single-select drop


class Missing(LookupError):
    """No label answers the name; `create` is the name `compose linear labels --create` takes to make it."""
    def __init__(self, message, create):
        super().__init__(message)
        self.create = create


def split(name):
    """("Type", "Story") for "Type > Story", (None, "Story") for a bare name."""
    group, sep, child = name.partition(SEP)
    return (group.strip(), child.strip()) if sep else (None, name.strip())


def qualified(label):
    """A label as a person reads it: "Type > Story" for a group's child, else its name."""
    parent = label.get("parent")
    return f"{parent['name']} {SEP} {label['name']}" if parent else label["name"]


def _eq(a, b):
    return (a or "").lower() == (b or "").lower()


def _usable(nodes):
    return [l for l in nodes if not l.get("isGroup")]  # a group itself cannot be applied to an issue


def type_group(nodes):
    """The team's `Type` label group, or None."""
    return next((l for l in nodes if l.get("isGroup") and _eq(l["name"], TYPE_GROUP)), None)


def _in_type_group(label):
    return bool(label.get("parent")) and _eq(label["parent"]["name"], TYPE_GROUP)


def types_map(repo):
    """The optional `types` map of the repo's `.compose/linear.json`, keys lowercased: {} when the file or the map is absent.
    Exits naming the file when the map is not type names to label names or null."""
    path = fc_core.repo_link(repo)
    try:
        raw = json.loads(path.read_text()).get("types") if path.exists() else None
    except (OSError, ValueError, AttributeError):
        return {}  # an unreadable linear.json is project_link's to report, where it is needed
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(k, str) and (v is None or isinstance(v, str) and v.strip()) for k, v in raw.items()):
        raise SystemExit(f'types in {path} maps a type to a label name or null, as {{"task": "Type > Tech Task", "epic": null}}')
    return {k.strip().lower(): v for k, v in raw.items()}


def type_names(nodes, types=None):
    """The lowercased names that mean a type on this team: compose's own, the `Type` group's children, and the types map's."""
    names = set(TYPES) | {l["name"].lower() for l in _usable(nodes) if _in_type_group(l)}
    for key, target in (types or {}).items():
        names.add(key)
        if target:
            names.add(split(target)[1].lower())
    return names


def is_type(label, nodes, types=None):
    """Whether a label sets an issue's type: a child of the `Type` group, or a standalone label named as a type."""
    return _in_type_group(label) or (not label.get("parent") and label["name"].lower() in type_names(nodes, types))


def _exact(nodes, name):
    """The label `name` names exactly: a qualified name is that group's child, a bare one the standalone label."""
    group, child = split(name)
    return next((l for l in _usable(nodes) if _eq(l["name"], child) and (_eq((l.get("parent") or {}).get("name"), group) if group else not l.get("parent"))), None)


def duplicate(nodes, name):
    """The label a create of `name` would duplicate, or None: a qualified name's own child; for a bare name the standalone label
    or the `Type` group's child of that name, so a team on the group never gets a loose copy of a type next to it."""
    found = _exact(nodes, name)
    if found or split(name)[0]:
        return found
    return next((l for l in _usable(nodes) if _eq(l["name"], name.strip()) and _in_type_group(l)), None)


def resolve(nodes, name, types=None, team="the team"):
    """(the label `name` means for a write, a note to print or None). Raises Missing when the team has none."""
    group, child = split(name)
    if group:
        found = _exact(nodes, name)
        if not found:
            raise Missing(f"team {team} has no label '{group} {SEP} {child}'", f"{group} {SEP} {child}")
        return found, None
    target = (types or {}).get(child.lower())
    if target:
        found = _exact(nodes, target)
        if not found:
            raise Missing(f"team {team} has no label '{target.strip()}', which types.{child.lower()} in .compose/linear.json names for {child}",
                          target.strip())
        return found, (f"label {child}: {qualified(found)} (types.{child.lower()} in .compose/linear.json)" if not _eq(qualified(found), child) else None)
    named = [l for l in _usable(nodes) if _eq(l["name"], child)]
    grouped = next((l for l in named if _in_type_group(l)), None)
    if grouped:
        return grouped, f"label {child}: {qualified(grouped)}"
    found = next((l for l in named if not l.get("parent")), None) or next(iter(named), None)
    if not found:
        raise Missing(f"team {team} has no label '{child}'", child)
    return found, None


def drops(nodes, name, types=None, team="the team"):
    """The labels a --drop-label of `name` takes off: a qualified or mapped name exactly that one, a bare name every label it could
    mean (the `Type` child and the standalone label both, so an issue labeled before its team moved to the group loses it too).
    Linear ignores dropping a label the issue lacks. Raises Missing when the team has none."""
    group, child = split(name)
    if group or (types or {}).get(child.lower()):
        return [resolve(nodes, name, types, team)[0]]
    named = [l for l in _usable(nodes) if _eq(l["name"], child)]
    if not named:
        raise Missing(f"team {team} has no label '{child}'", child)
    return named


def single_select(nodes, have, added, types=None):
    """The labels on the issue (`have`) that adding `added` takes off in the same update, so it never ends with two of one kind:
    the issue's other child of a single-select group `added` is in, and, when the team has a `Type` group and `added` is a type,
    every other type label on the issue (the old `Type` child, and the standalone label of the old type or of the same type)."""
    typed = type_group(nodes) is not None and is_type(added, nodes, types)
    parent = added.get("parent")
    group = next((l for l in nodes if parent and l["id"] == parent["id"]), {})
    single = parent and group.get("groupType") != "multiSelect"
    return [l for l in have if l["id"] != added["id"]
            and ((single and (l.get("parent") or {}).get("id") == parent["id"]) or (typed and is_type(l, nodes, types)))]


def changes(nodes, have, added, dropped, types=None, team="the team"):
    """({"addedLabelIds": [...], "removedLabelIds": [...]}, notes) for an update that adds the labels `added` (resolved) and drops
    the names `dropped` (drops) from an issue carrying `have`: each added label also takes off what single-select allows only
    one of, in the same update, with a note for each. Raises Missing for a dropped name the team lacks."""
    removed, notes = [l for name in dropped for l in drops(nodes, name, types, team)], []
    for label in added:
        for old in single_select(nodes, have, label, types):
            if old["id"] not in {r["id"] for r in removed}:
                notes.append(f"drops {qualified(old)}: the issue takes one {'type' if is_type(label, nodes, types) else old['parent']['name']}")
                removed.append(old)
    keep, out = {l["id"] for l in added}, {}
    for key, labels in (("removedLabelIds", [l for l in removed if l["id"] not in keep]), ("addedLabelIds", added)):
        if labels:
            out[key] = list(dict.fromkeys(l["id"] for l in labels))
    return out, notes


def list_filter(names, types=None):
    """The IssueLabelFilter for `compose linear list --label`: a bare name matches its label in either shape (Linear returns a
    `Type` child by its own name), a qualified name, or one the types map names, only that group's child."""
    bare, exact = [], []
    for name in names:
        group, child = split(name if SEP in name else (types or {}).get(name.strip().lower()) or name)
        if group:
            exact.append({"and": [{"name": {"eqIgnoreCase": child}}, {"parent": {"name": {"eqIgnoreCase": group}}}]})
        else:
            bare.append(child)
    return {"or": ([{"name": {"in": bare}}] if bare else []) + exact} if exact else {"name": {"in": bare}}
