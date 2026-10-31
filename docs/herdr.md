# Running inside herdr

Inside [herdr](https://herdr.dev) you can see every agent in the fleet and steer any of them.
`compose` with no command, run on a terminal outside herdr, opens the herdr session `compose` for you: `herdr session attach compose`, which creates the session when it is missing.
It prints one hint line first, then you run `claude` in a pane and type `/compose`, or run `codex` and type `$compose`.
Each harness is described in [harnesses.md](harnesses.md).
`COMPOSE_NO_HERDR=1` turns that off, and inside herdr it keeps every command headless too: no space, tab, pane, or agent.
Every subcommand runs headless as before, and a team lead started outside herdr works in one session.

## Roles

You are the product owner.
The roles are adapted from [firstmate](https://github.com/kunchenguid/firstmate).

| Role | Who | Where |
|---|---|---|
| engineering manager | the session you started; creates the project, then only relays | the pane you started it in |
| team lead | runs the project from the approved design to a verified stack | tab `orchestrator`, agent `<slug>-orchestrator` |
| designer | continues design work in Figma from where it stopped | tab `designer`, agent `<slug>-designer` |
| design-NNN | one builder per design task, Claude only | tab `design NNN` |
| design-review | one screenshot per story, findings confirmed by quoting nodes | tab `design-review` |
| teammates | explore, builder check, architect (change of course), groundwork, test writers, helpers, implementers | one tab each in the project's space |
| planner | continues specing a ticket at step 1, 2, or 8 and cuts the task issues | tab `planner`, agent `<slug>-planner` |

herdr agent names stop at 32 characters, so a longer `<slug>-<role>` keeps its role and cuts the slug.
Project `clean-recut-branches` has team lead `clean-recut-branche-orchestrator`, and `init` says so when it creates such a project.
Use the name `compose crew list` shows in every `herdr agent` command; `watch --from` takes the role.

New panes get `PATH` and each harness row's `home_env` (`CODEX_HOME` for Codex) from the caller.
A pane on a sign-in screen gets no key: compose stops and names the pane and the screen.

herdr starts an agent by typing its command into the new pane's interactive shell, since `agent start` cannot launch the command as the pane's process.
A prompt that shell asks first (oh-my-zsh's `Would you like to update? [Y/n]`) takes the first letters of that command as its answer, the shell then runs `laude`, and the start times out.
New panes get `DISABLE_AUTO_UPDATE=true` and `DISABLE_UPDATE_PROMPT=true` to keep oh-my-zsh quiet.
A prompt still on screen before the start is declined with `n`, and a start that times out on one is declined once and started again.
A timeout that stays names what the pane waits on, such as `the new pane's shell is waiting on a prompt: '...'`, instead of a bare `timeout`.
An explicit `zstyle ':omz:update' mode prompt` in `~/.zshenv`, `~/.zprofile`, `~/.zshrc` or `~/.zlogin` outranks those variables, so `compose doctor` lists it as `MISSING: shell startup` with the line to change (`mode reminder` or `disabled`).

The engineering manager is the session you started, so compose sets neither its model nor its compaction.
It relays and supervises while planning goes to the planner on the strongest model, so Sonnet serves it well.
Cap its context with `"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"` in the `env` of `~/.claude/settings.json`, as compose caps every teammate at 150k.
That variable outranks `modelSettings.<model>.autoCompactWindow` and the `--autocompact` flag, and a session reads it only when it starts.

The engineering manager and the team lead write no code.
For the team lead the plugin enforces this.
The team lead's and the planner's tabs are both marked (`COMPOSE_ROLE=orchestrator` or `planner`) and denied edits by the plugin's PreToolUse hook (`compose guard`), except the CLI's JSON under `.compose/`.
The team lead starts the planner once you approve the design, or you start it with `/compose:planner <ticket>` from the engineering manager.

## Talking to the fleet

You talk to the engineering manager, and you can also type into any teammate's tab directly.
Teammates report up with `compose report` and pick a state from `working`, `needs-user`, `blocked`, `done`, `tool-bug`, and `contradiction`.
`compose toolsmith` only prints the toolsmith's brief.
The toolsmith it briefs files the tool-bug as an issue on prodoxx/compose with `compose tool-bug file`, which needs gh logged in to post, and on the maintainer's machine opens its fix from its own worktree as a PR with `compose tool-bug pr`; it never commits on main or in the installed copy.

A `contradiction` report always wakes the team lead as a teammate event.
It also screens the feature's decisions with Jev, which adds a `reflect=` event when it flags a decision line.

A `needs-user` or `blocked` report badges the pane `needs you`.
A `needs-user` that names a Lavish page is refused unless it carries the page's one working link, and questions already asked and not answered are not asked again.
The desktop notification pops only when such a report wakes the engineering manager's `watch`, so you are alerted once, for what the engineering manager brings you, and not by every teammate or the team lead.
The CLI sends it itself (macOS `osascript` run through `launchctl asuser` in the GUI session, since herdr panes sit in launchd's Background session where Notification Center drops notifications; elsewhere `notify-send`), not through herdr: herdr's own toast and sound fire for every agent that finishes or needs input, so set `[ui.toast] delivery = "off"` and `[ui.sound] enabled = false` in `~/.config/herdr/config.toml`; `compose doctor` warns while either is on.
Nothing steals focus.
When you want to jump to a tab, `compose crew focus --role <role>` takes you there.

## Teammate commands

These commands start, list, and stop teammates.

```bash
compose crew start --project <slug> --role orchestrator --title orchestrator --brief - < brief.txt
compose explore --project <slug> | compose crew start --project <slug> --role explore --title explore --brief -
compose architect --project <slug> --change C2 --extra - < notes.md | compose crew start --project <slug> --role architect --title architect --brief -
compose crew list --project <slug>
compose crew stop --project <slug> --role architect   # refused until it reported done; --force overrides
compose testcrews --project <slug>                    # one test-writing teammate per task
compose figma check --project <slug> --figma <url or key>
compose designer --project <slug> | compose crew start --project <slug> --role designer --title designer --brief -
compose designcrews --project <slug>   # one design-NNN builder per open design task
compose teardown --project <slug>                     # close finished tabs; the team lead's done runs it
```

A teammate that works in a repo of its own (`--work-repo`, or `--cwd` in a git repo) holds a treehouse slot of that repo.
`crew start` and `crew list` give back the slot of every teammate whose tab is gone, when treehouse shows that teammate as its holder and nothing in it is uncommitted; its branch stays.
A slot with uncommitted work stays leased, with one line naming the paths.

The config sets the model for each teammate role, as [workspaces.md](workspaces.md#crew-models) explains.

## Layout

A project opens as a herdr worktree space of its own, labelled `<slug>`, grouped under the space open on its repo, laid out like this.
`init` uses the repo's space it was started from, or any open space of the repo, and opens no other space.

| herdr | `compose` | Label |
|---|---|---|
| space | the project worktree, grouped under the repo's space | `<slug>` |
| tab | a shell in the project worktree | `shell` |
| tab | every implementer, one pane per task | `implementers` |
| pane | one task's interactive pi session | `NNN <title>`, agent `<slug>-NNN` |
| tab | the latest verify run, one pane per stage | `verify` |
| tab | the team lead | `orchestrator` |
| tab | one teammate session | its title, agent `<slug>-<role>` |

When no space of the project's repo is open, such as when compose runs from another repo's space, the project opens in the space `init` was started from instead, with no new space.
There every tab carries the project's name first (`<slug> shell`), so several projects share one space without touching each other's tabs.
That space is recorded in the project's `results/herdr.json`, so every later command finds it, and the project stays there while it is open.

Each task pane shows its state, such as `attempt 2/3` or `passed`, along with its model and cost.
The team lead steers a working or blocked implementer with `herdr agent prompt <slug>-NNN "..."`.
That prompt lands in the same pi session the CLI drives.
When herdr sees no work within 5 s of a CLI prompt (`agent_prompt_stalled`), the CLI keeps the pane and waits for the turn, or reads it from the pi session file when herdr never sees it working.
A prompt pi never records within 30 s is sent once more to the same session, and only a turn that still never starts ends as a timeout and costs an attempt.
`clean` closes the project's space along with its worktree, or in a shared space only the project's tabs.
While the team lead has not reported done, `clean` keeps both, and the team lead's `done` closes them once its turn ends.

## Supervision

`compose watch --project <slug>` sleeps until a task finishes, an agent blocks, a teammate reports, a run ends, or verify finishes.
It prints one `event ...` line and exits, so a waiting team lead spends no tokens.
`--from orchestrator` makes the engineering manager wake on the team lead's reports, on a tool-bug (to start a toolsmith) or a stall, and on any teammate it started itself.
`crew start` records who each teammate reports to (`reports_to` in `results/crew/<agent>.json`), so a planner or designer the engineering manager started needs no second watch.
It also records the session that started it (`started_by`, its herdr pane).
With two engineering manager sessions on one project, each session's watch wakes on no line of a teammate another live session started, unless `--from` names it.
A teammate whose starting session has ended is adopted by any engineering manager's watch.
One engineering manager session drives a project (`results/manager.json`, the herdr pane that first ran `crew start`, `crew stop`, or a continuous `watch`).
The same from another session while that pane still holds a live agent refuses, naming the pane; `watch --now` and `status --digest` stay read-only and `--takeover` moves the claim and the owner's teammates to the new session.
An owner whose pane is gone is replaced silently, and outside herdr nothing is claimed.
The watch spends few wake-ups: a task starting is not an event, a teammate's plain `working` report travels with the next wake (or alone after 5 minutes), a burst of non-urgent events is one wake after a 10 s settle, and the watch never times out.
For the product owner it adds `event tell='<line>'` on a state change only: verify failed and why, fix loop started, run ended or crashed, verify passed, done.
A teammate idle for a minute whose last report is `working` and whose pane ends on an API or network error raises `event stall=network` once, with the nudge to send when the network is back.
A run left silent with work pending raises a stall event after 240 s.
You can change that limit with `stall_seconds` in `verify.json` or with `watch --stall`.

Each watcher remembers what it last reported in `results/watch-<who>.json`.
The `<who>` is the watcher's `COMPOSE_ROLE` (`orchestrator` for the team lead), and `manager-<pane>` for an engineering manager session in herdr pane `<pane>` (`manager` outside herdr).
`watch --now` and `status` also say whether away or yolo is on and which session set it, since one session's `compose away` or `compose yolo` changes how every session on the project answers.
A watch started later still reports what happened while none ran, and `watch --now` marks the current state as seen.
That watch wakes only for what needs attention: a done, needs-user, blocked, contradiction or tool-bug report, a stall, a payment halt, a Mac sleep, a crash or a sign-in screen.
The `working` reports and other progress lines wait and are printed in the same wake as the next such event.
`--all` wakes on every line again, and `--attention` gives any other watch the same behavior.
A billing halt is an event too, shown as `event halt=payment` with the provider, the tasks left, the top-up link as `top_up=` and the exact command that continues as `resume=`.
The Mac sleeping is not a stall: when the wall clock jumps by 90 s or more between two polls (2 s apart), the watch prints one `event sleep=woke from=<time> to=<time> slept=<span>` naming the window.
A start refused on a low battery (`run`, `resume`, `verify`, `crew start`, `testcrews` on battery at 10% or less) is recorded in `~/.compose/live/battery-held`.
Once the Mac runs on AC again, within six hours, every watch prints one `event power=ac held=<time>`, which wakes the engineering manager's quiet watch too, so the refused start runs again without waiting for the product owner.
The silence alarms then wait one stall window after the wake, since nothing could move while the Mac slept.
A run that died within 15 minutes of the wake, whose log was still being written when the Mac went to sleep and which did not halt on a payment limit, is resumed by the watch itself: `event sleep=resumed`, once per run, with `compose resume` logging to `results/resume.log`.
Any other run that died after a wake raises `event sleep=run-lost` with the reason and the exact `compose resume --repo <repo> --project <slug>` command, and a run stopped with Ctrl-C is left alone.
A pane that keeps showing a retry loop or a limit banner for 60 s ("Retrying ... 401", "usage limit", "try again at", "rate limited", "402") raises `event stall=limit agent=<name> line='<the matched line>'`, once per spell, instead of waiting for the quiet alarm.
The watch reads each working teammate's pane at most every 30 s, and a spell ends when the pane stops matching.
A teammate herdr shows as working for 30 minutes with no report and no commit raises `event stall=silent agent=<name>`; set `agent_stall_seconds` in `verify.json` to change that.
The 4-minute quiet alarm for the team lead is unchanged, and time asleep does not count toward either.

The team lead never starts `watch` itself.
Its tab carries `COMPOSE_ROLE`, `COMPOSE_PROJECT`, and `COMPOSE_REPO`, and the plugin's Stop hook (`compose rewake`) parks a watch whenever one of its turns ends.
The watch's event wakes the idle session (Claude Code's `asyncRewake`), and the next turn end parks the next watch.
A Codex session has no background wake, so it runs `compose watch ... --timeout 240` in the foreground.
A lock keeps one watch per watcher.
A second hook exits at once, and a second `watch` of the same session is refused, naming the first one's pid.
The hook does nothing in any other session, and its watch ends with the session that parked it.
Every Claude Code teammate compacts at 150k tokens, since every turn re-sends the whole session and a long one pays for its size on each of them.
The window is `CLAUDE_CODE_AUTO_COMPACT_WINDOW` in the session's `--settings` env, because that variable outranks the `--autocompact` flag and would let a value in your own settings win.
After a context compaction, the plugin's SessionStart hook (`compose reenter`) points any teammate back at its own brief, `results/crew/<agent>.brief.md`.
Every teammate's tab carries its name in `COMPOSE_TEAMMATE` for that, since `COMPOSE_ROLE` is `crew` for all but the team lead and the planner.

Restarting the team lead in its tab loses nothing, because its first turn end reports whatever happened while it was down.
A `run` or `verify` it started keeps going meanwhile.
Those work in their own session, so a closed tab or a crashed session never ends them.
The engineering manager, and a team lead outside herdr, still run `watch` in the background themselves.
