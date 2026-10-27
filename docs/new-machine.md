# Set up a new machine

One command takes a Mac to a working `compose` install:

```sh
curl -fsSL https://raw.githubusercontent.com/prodoxx/compose/main/bin/install.sh | sh
```

It runs on macOS only.
It clones `compose` into `~/.compose/src` (set `COMPOSE_INSTALL_DIR` for another place), or reuses the checkout your `~/.claude/skills/compose` link already points to.
It links `~/.local/bin/compose` and `~/.claude/skills/compose`, installs uv when it is missing, and starts `compose setup`.
It never edits your shell profile.
Setup stops when it finds a fast-coding install, and points to the migration at the end of this page.
[requirements.md](requirements.md) explains what each tool is for.

## What setup does

On a terminal, `compose setup` asks one question at a time: Quick start takes every default, Custom asks a yes or no for each missing step.
With `--yes`, or without a terminal, it runs the same steps with every default and never prompts.
A key you did not enter is skipped, and the ending lists it with the command that sets it later.
Setup never installs Claude Code.
Without `claude` on the PATH it runs every step that does not need it, skips the rest, and ends by printing the Claude Code install line and asking you to run `compose setup` again.

| Step group | Level | What setup does | Needs Claude Code |
|---|---|---|---|
| git, Node, gh, gh stack | required | git comes from `xcode-select --install` (setup only reports it); installs `node` and `gh` with Homebrew and the `gh stack` extension; asks you to run `gh auth login` | no |
| claude | required | reports it only, with its install line | no |
| herdr | required | installs herdr with its own installer and the herdr skill file | no |
| pi | required | installs pi with `npm install -g --ignore-scripts`, and declares its `openrouter-fast` provider in `~/.pi/agent/models.json` with no key in it | no |
| Links | required | links `compose` into `~/.local/bin` and the skill into `~/.claude/skills`; reports when `~/.local/bin` is not on your PATH | no |
| Codex skill link | default | links the skill into Codex's `skills/compose` under `CODEX_HOME`; ok when `codex` is not on the PATH | no |
| Codex commands | default | writes the `skills/compose-<name>` command skills under `CODEX_HOME`; ok when `codex` is not on the PATH | no |
| Codex hooks | default | writes `hooks.json` and the trust records in `config.toml` under `CODEX_HOME`; ok when `codex` is not on the PATH | no |
| Keys | required | saves the OpenRouter key, the TypeSafe key, and the Linear key into `~/.compose/auth.json` | no |
| i-have-adhd skill | required | adds the marketplace | yes |
| Homebrew, uv, jev | default | installs Homebrew when missing, uv, and the `jev/` environment | no |
| trail, treehouse | default | installs `@trailhq/trail-code` and treehouse | no |
| bp, Chromium | default | links `bp` into `~/.local/bin` and installs Playwright's Chromium | no |
| herdr config, herdr pi integration | default | writes herdr's config (below) and installs herdr's pi integration | no |
| Models and `.env` | default | adds the default model choices to `~/.compose/config.json` and the `TEXT_MODEL` lines to `~/.compose/.env`, never overwriting what is there | no |
| git rerere, Claude Code permissions | default | sets `rerere.enabled` globally when it is unset; shows the narrow Claude Code permission rules `compose` needs and writes them to `~/.claude/settings.json` only on your yes (never with `--yes` or `--dry-run`) | no |
| herdr claude integration | default | installs herdr's claude integration | yes |
| Framelink | opt-in, designer | adds the `framelink-figma` MCP server and your Figma token, for the designer only | yes |

Setup ends with a model probe against `models.implementer`, unless you skipped the OpenRouter key or ran with `--yes`.
It exits 1 when the probe fails, and prints how to create the OpenRouter preset `glm-5-3-flash-fast` and where to change the model (`models.implementer` in `~/.compose/config.json`).
`compose doctor` shows the same steps, with one `MISSING` line per gap.

With `--yes`, setup ends with `compose is ready`, or with `compose is installed but not ready:` and one line per missing required step.
Then it tells you the next move: `compose` outside herdr, or `claude` in a pane and `/compose` inside it.
With no Claude Code and Codex set up, setup still finishes and says `next: run codex in a pane and type $compose`.
[harnesses.md](harnesses.md) has the rest about harnesses.

## herdr settings

A machine without a herdr config gets the config the maintainer uses, from `bin/herdr.toml` in the checkout.
A machine with one keeps all of it, and setup sets only the two lines `compose` needs, so the only desktop alert is the one `compose` sends itself:

```toml
[ui.toast]
delivery = "off"

[ui.sound]
enabled = false
```

A config that is a symlink or not writable is left alone, and setup prints the two lines for you to set.
`compose doctor` prints an optional `MISSING: herdr config: <key>=<value>` line while either one is on.

## Before a long run

Run `compose doctor --project <slug>` before `compose run`.
It also reports, all at once and with the fix for each, what would stop or spoil the work:

- uncommitted changes in the project worktree (the tools' own files and the agent wiring do not count),
- an OpenRouter or Linear key that its service rejects, found with a free key-info call (`pbpaste | compose workspace key <workspace>` stores a new one),
- a Mac on battery, a load average far above the core count, and a docker container that keeps restarting, as warnings.

`run`, `resume`, and `verify` make the same checks at their start, so a run never stops later to ask about them.
While they work, compose holds a macOS `caffeinate -dims -w <pid>` assertion so the Mac and its display stay awake, and releases it when the process ends.
Closing the lid on battery still sleeps the Mac, which `caffeinate` cannot prevent: plug it in or leave the lid open. On battery at 10% or less, `run`, `resume`, `verify`, and `crew start` refuse to start and say so; `--force` starts anyway.
compose changes no power setting.
Offline, `COMPOSE_NO_PREFLIGHT=1` skips the key calls, the gh check, and the machine warnings.

## Each repo

A repo needs no setup ahead of time.
The first run in a repo asks for its Linear project and writes `.compose/linear.json` in the repo's main checkout, which stays out of git and serves every worktree of the repo.

## Update

Run the install command again: it pulls the checkout with `--ff-only`, relinks the CLI and the skill, and runs `compose setup`, which skips what is already done.
Or run `compose update`: it pulls with `--ff-only`, relinks what `compose` owns (the CLI, the skill, and `bp`), and runs `compose doctor`.
`compose update` stops when the checkout has local changes, and when its `main` has commits that `origin/main` lacks it lists them, marks the ones already upstream, and prints the `git` command to run first.
After an update, restart any long-running `compose watch`, because it keeps running the code it started with.

## Move a machine from fast-coding

A machine set up before the rename has `fast-coding` links and `~/.fast-coding/`.
Run the migration with the old checkout's path; it points the checkout at `prodoxx/compose`, pulls, moves `~/.fast-coding/` to `~/.compose/`, and swaps both links:

```sh
curl -fsSL https://raw.githubusercontent.com/prodoxx/compose/main/bin/migrate-to-compose.sh | bash -s -- ~/code/fast-coding
```

It is safe to run again, and it prints the few steps left by hand, starting with restarting Claude Code so `/compose:*` replaces `/fast-coding:*`.
