# Terminal hosts: herdr, the Herdr app, Rex

compose shows its fleet in the terminal host the session runs in.
Every agent is a pane there, named so you can find it, and you can type into any of them.
Three hosts are supported, and compose tells them apart by the variables each sets in its terminals.

| Host | How compose knows | The session's id | The CLI compose drives |
|---|---|---|---|
| [herdr](https://herdr.dev/tui/), the TUI | `HERDR_ENV=1` with `HERDR_SOCKET_PATH` | `HERDR_PANE_ID` | `herdr` ([herdr.md](herdr.md)) |
| the [Herdr app](https://herdr.dev) | `HC_TERMINAL_ID` with `HC_TERMINAL_SOCKET` | `HC_TERMINAL_ID` | `hc` (herdr-core runs the terminals) and `herdr-app` (the window) |
| [Rex](https://www.superlogical.com/rex) | `REX_SESSION` with `REX_BLOCK` | `REX_BLOCK` | `rex` (in `REX_BIN_DIR`, which Rex sets, else on PATH) |

herdr is checked first: a herdr pane that runs inside the Herdr app or Rex is still steered through herdr.
`COMPOSE_HOST=<herdr|hc|rex>` names the host for a session whose variables were lost, and `COMPOSE_NO_HERDR=1` makes every host headless.
Outside every host, each command runs headless, as it always did.
`compose status` names the host in its engineering manager lines (`the Herdr app pane t7`, `Rex pane block:...`).

## One command set

No session needs the host's own CLI.
The commands below do the host's work and read the same in each:

| Command | What it does |
|---|---|
| `compose crew prompt --project <slug> --role <role> "<text>"` | types a prompt into a teammate, as the product owner would; `--agent <name>` reaches any live agent of the host (an implementer `<slug>-NNN`, a test writer) |
| `compose crew read --project <slug> --role <role> [--lines N]` | prints the last lines of its pane (60 by default) |
| `compose crew keys --project <slug> --role <role> esc` | presses keys in it, `esc` to stop its turn |
| `compose crew focus --project <slug> --role <role>` | takes you to its pane |
| `compose crew list --project <slug>` | every teammate with its last report and the status the host reports |

A refusal names the reason: `agent_blocked` when the agent waits at a dialog (answer it in its pane first), `agent_not_found` when nothing of that name is live.
Inside herdr you may still use `herdr agent prompt <name> "..."` and `herdr agent read <name>` directly; inside the Herdr app `hc send <name> "..."` and `hc read <name>`; inside Rex `rex send` and `rex capture`.
Read the host's own guide before that: `~/.claude/skills/herdr/SKILL.md`, `hc --skill`, or `rex skill show`.

## What each host shows

The layout is the same idea in each: a space per project, a tab per teammate, one `implementers` tab with a pane per running task, and a `verify` tab.

| compose | herdr | the Herdr app | Rex |
|---|---|---|---|
| the project's space | a worktree space grouped under the repo's space, or the space compose was started from | a space named `<parent> › <project>` after the space compose was started from, placed right after that space and its other projects, its terminals in the project worktree | a session labelled `<parent> › <project>` after the session compose was started from, placed the same way |
| a teammate's tab | a tab holding the agent, started by `herdr agent start` in its shell | a tab holding the agent's terminal (`hc run --name <agent>`), named for the teammate | a window named for the teammate, one block running the agent |
| an implementer's pane | a split of the `implementers` tab | a terminal placed in the `implementers` tab by its labels | a split of the `implementers` window |
| the agent's name | herdr's registered agent name | the hc terminal's name | the first part of the block's label: `<name> · <title> · <state>` |
| the agent's status | herdr's detection | what the program reports over OSC 7501 | what the program reports over OSC 7501 |
| state and cost on a pane | herdr's pane metadata (`attempt 2/3`, `cost=$0.01`) | labels on the terminal (`hc get <name>`: `compose.state`, `compose.cost`) | the block's label |
| the verify stages | one pane per stage, each reported by compose | one pane running `compose stages`, the stages read from the verify's hold file | the same |
| the engineering manager's pane | labelled `manager: <slug>, ...` | the terminal's `compose.manager` label | the block's label |

Claude Code, pi (1.3 and later) and Codex report their status over OSC 7501, so the Herdr app and Rex show a teammate `working`, `idle`, `blocked` or `done` as herdr does.
A program that reports nothing (a plain shell) has no status; compose then judges by its reports and its process.

The Herdr app and Rex list spaces flat, so the `<parent> › <project>` name and the placement stand in for herdr's worktree grouping; a project started from another project's space goes under that space's parent.
In the Herdr app the window is a view: agents started while it is closed keep running under hc and are reachable by name, and they have no space or tab until the window opens.
In Rex a block's label is the agent's identity, so a label you change by hand loses compose its agent; rename the window instead.

## `compose stages`

herdr draws every verify stage on a pane of its own.
The Herdr app and Rex show programs, so there compose opens one pane in the project's `verify` tab running `compose stages --repo <wt> --project <slug>`: it follows the verify, prints one line per stage change, and reports its own status over OSC 7501, so the host's agents list shows the verify working, blocked on a failed stage, or done.
It exits 1 when a stage failed, and the pane stays to read, as herdr's does.

## Bare `compose`

`compose` with no command, on a terminal outside every host, opens the host this machine has: the Herdr app when `hc` and `herdr-app` are installed, else the herdr session `compose`, else Rex.
It prints one hint line (`open Herdr, run claude in a tab and type /compose`), then you run `claude` in a pane there and type `/compose`.
When none is installed it prints herdr's install line and `or run compose setup`, and exits 1.

## Limits

- The Herdr app's agents list shows the terminal's title, not compose's state label; `hc get <name>` shows the labels.
- Rex's `rex wait` waits only for a program to exit, so compose polls a block's reported status while it waits for a turn, every 2 seconds.
- The compactor reads a pane with styling to tell a suggested prompt from typed text; the Herdr app and Rex answer plain text, so there a suggested prompt counts as typed and the compaction waits for the next quiet moment.
- `compose doctor`'s herdr rows (its config, its integrations) are herdr's; the Herdr app and Rex need no settings from compose.
