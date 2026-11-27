"""A workspace's own names for the Linear states compose works by. compose reads and writes six states by name: Backlog, Todo, Planning,
In Progress, In Review and Done. A team that calls them something else is named in the config, per workspace:

    "linear_states": {"Todo": "Ready for Dev", "In Progress": "Dev In Progress", "In Review": "PR In Review", "Done": "Staged"}

Every answer from Linear passes through `read` (fc_linearapi.linear_gql), which gives each state compose's name and keeps the team's under
`team_name`, so the rest of the CLI knows one set of names. A value may be a list: compose moves an issue to the first name the team has,
and reads every name in it as that state. A name no entry holds keeps its own name, and a team that has compose's name needs no entry."""
import sys
import fc_config

# compose's states and the Linear type each one is read as: a team may keep "Staged" among its started states and still have it read as Done.
TYPES = {"Backlog": "backlog", "Todo": "unstarted", "Planning": "unstarted", "In Progress": "started", "In Review": "started", "Done": "completed"}
_OURS = {c.lower(): c for c in TYPES}

def table():
    """{compose's name: [the team's names, the one compose writes first]} from the config, checked: an unknown state, a name that is no
    text, and a name standing for two states each stop the command with the line to fix."""
    raw, out, seen = fc_config.LINEAR_STATES, {}, {}
    where = f"{fc_config.CONFIG_PATH}: linear_states"
    if not isinstance(raw, dict):
        sys.exit(f"{where} must be an object of {{compose's state: the team's name, or a list of names}}, not {raw!r}")
    for key, value in raw.items():
        ours = _OURS.get(str(key).lower())
        if not ours:
            sys.exit(f"{where}.{key}: compose has no such state; it has {', '.join(TYPES)} (a canceled state is known by its type)")
        names = value if isinstance(value, list) else [value]
        if not names or any(not isinstance(n, str) or not n.strip() for n in names):
            sys.exit(f"{where}.{key} must be the team's name for {ours}, or a list of names, not {value!r}")
        for n in (n.strip() for n in names):
            other = seen.get(n.lower()) or _OURS.get(n.lower())
            if other and other != ours:
                sys.exit(f"{where}.{key}: {n!r} already stands for {other}; a team's state is one of compose's states, not two")
            if n.lower() not in seen:
                seen[n.lower()] = ours
                out.setdefault(ours, []).append(n)
    return out

def canonical(name):
    """compose's name for a state the team, a flag or a piped payload names; any other name comes back as it is."""
    key = (name or "").strip().lower()
    for ours, names in table().items() if key else ():
        if key in (n.lower() for n in names):
            return ours
    return name

def names(name):
    """Every name Linear may hold the state under, compose's own first: what a filter by state has to match."""
    ours = canonical(name)
    return list(dict.fromkeys([ours, *table().get(_OURS.get((ours or "").lower()), [])]))

def _rename(node, by):
    ours = by.get((node.get("name") or "").lower())
    if ours and node["name"] != ours:
        node["team_name"], node["name"] = node["name"], ours
        if "type" in node:
            node["type"] = TYPES[ours]

def _team(nodes, tab):
    """A team's states as compose finds them by name: for each state the config names, the first of its names the team has carries
    compose's name, and a state of the team's that already had that name is left out, so a lookup by name finds the one compose writes."""
    out = list(nodes)
    for ours, own in tab.items():
        written = next((n for name in own for n in out if (n.get("name") or "").lower() == name.lower()), None)
        if written is None or written["name"].lower() == ours.lower():
            continue
        out = [n for n in out if n is written or (n.get("name") or "").lower() != ours.lower()]
        _rename(written, {written["name"].lower(): ours})
    return out

def _walk(value, tab, by):
    if isinstance(value, list):
        for v in value:
            _walk(v, tab, by)
    elif isinstance(value, dict):
        for key, v in value.items():
            if key == "state" and isinstance(v, dict):
                _rename(v, by)
            elif key == "states" and isinstance(v, dict) and isinstance(v.get("nodes"), list):
                v["nodes"] = _team(v["nodes"], tab)
            else:
                _walk(v, tab, by)

def read(data):
    """An answer from Linear with every issue's `state` and every team's `states` under compose's names; unchanged with no `linear_states`."""
    tab = table()
    if tab:
        _walk(data, tab, {n.lower(): ours for ours, own in tab.items() for n in own})
    return data

def shown(node, name=None):
    """The name the team's board shows for a state compose read: what a line a person reads says."""
    return (node or {}).get("team_name") or name or (node or {}).get("name") or ""
