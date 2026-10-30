# Config reference

`~/.compose/config.json` holds every setting `compose` reads: which models do which job, local model servers, how many agents run at once, and a few naming limits.
This page lists every key, what it means, what it takes, and what happens when it is wrong.
[workspaces.md](workspaces.md) covers the commands around it, and [verify.md](verify.md) covers the per-project `verify.json`.

## The file

- Path: `~/.compose/config.json`.
  Set the `COMPOSE_CONFIG` environment variable to use another file.
- It is created with mode 600.
- It never holds a key.
  Every key lives in `auth.json` (see [Files beside it](#files-beside-it)), and the first command after an upgrade moves any key it finds in `config.json` or `.env`.
- Every command reads it when it starts, so an edit takes effect on the next command.
- A missing file is an empty config, and every key below then has its default.

`compose workspace` edits the `workspace` and `workspaces` keys and the keys in `auth.json`.
Everything else you edit by hand.

## A full example

```json
{
  "workspace": "work",
  "models": {
    "implementer": "pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast",
    "fallback": "claude:claude-sonnet-5-5",
    "fallback_after": 2,
    "test_reader": "claude:claude-haiku-5-5",
    "fix": "pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast",
    "crew": {
      "planner": {
        "model": "pi:ollama/qwen3-coder:30b",
        "effort": "high",
        "fallback": [
          { "model": "claude:claude-opus-5-5", "effort": "high" }
        ]
      },
      "default": "claude:claude-sonnet-5-5"
    }
  },
  "providers": {
    "ollama": {
      "base_url": "http://localhost:11434/v1",
      "models": ["qwen3-coder:30b"]
    }
  },
  "env": {
    "SOME_VAR": "value"
  },
  "parallel": 20,
  "claude_parallel": 3,
  "branch_prefix": "compose/",
  "max_stack_prs": 10,
  "workspaces": {
    "work": {},
    "lab": {
      "providers": {
        "ollama": {
          "base_url": "http://gpu-box:11434/v1"
        }
      }
    }
  }
}
```

## Keys

| Key | Takes | Default | Means |
|---|---|---|---|
| `workspace` | a workspace name | `default` | The active workspace. |
| `workspaces` | a map of name to settings | none | Per-workspace overrides, see [Workspaces](#workspaces). |
| `models.implementer` | a model id | GLM 5.3 Flash on OpenRouter | The model that implements each task. |
| `models.fallback` | a model id or `null` | `null` | The model a task moves to after `fallback_after` failed attempts. |
| `models.fallback_after` | a whole number | `2` | Failed attempts on the task's own model before the fallback takes over. |
| `models.test_reader` | a `claude:` or `codex:` model id, or `null` | `claude:claude-haiku-5-5` | Reads a failed test run and writes the fix brief. `null` sends the raw output. |
| `models.review` | a `claude:` model id, or an OpenRouter model id | unset: the `.env`'s `TEXT_MODEL` | Reviews the diff in `verify` and `compose review`, and confirms each finding. A `claude:` id runs Claude Code on the subscription, with no tools and none of your customizations. |
| `models.readiness` | a `claude:` model id, or `{ "model", "effort" }` | `claude:claude-opus-5-5`, effort `high` | Judges a technical story (labeled deep-dive on it, its feature or its epic, or one Jev rates technical) in `compose readiness`, after Jev. Runs Claude Code on the subscription, with no tools and none of your customizations. |
| `models.fix` | a model id | the implementer | The model of verify's fix rounds. A project's `verify.json` `fix_model` beats it. |
| `models.crew` | a map of role to entry | none | The model of each teammate role, see [Crew models](#crew-models). |
| `models.tiers` | a map of `cheap`, `standard`, `frontier` to a list of entries | see [Model tiers](#model-tiers-prices-and-the-frontier-budget) | The models of each tier, in order. A tier you leave out keeps its default list. |
| `prices` | a map of model id to 2 or 4 numbers | the built-in list | Dollars per million tokens, see [Model tiers](#model-tiers-prices-and-the-frontier-budget). |
| `harnesses` | a map of harness name to settings | none | Per-harness overrides for a teammate session, see [Harnesses](#harnesses). |
| `providers` | a map of name to server | none | Local OpenAI-compatible servers, see [Providers](#providers). |
| `env` | a map of name to value | none | Environment variables set for the CLI and everything it starts. |
| `parallel` | a whole number of 1 or more | `run` and `resume` use 3, `testcrews` starts all at once | How many agents start at once. |
| `claude_parallel` | a whole number of 1 or more | `3` | The cap on Claude Code sessions among those agents. |
| `branch_prefix` | a string | `compose/` | The start of every new project's branch. |
| `max_stack_prs` | a whole number | `10` | The most PRs one stack opens; adjacent layers fold past it. |
| `notify.ntfy` | an `http://` or `https://` topic URL | none (off) | Sends a phone notification for a gate only you can clear, see [Phone notifications](#phone-notifications). |

A command's own flag always wins over the config.
`--parallel N` beats `parallel`, and `crew start --model` beats `models.crew`.

## Model ids

A model id names the harness that runs it.

| Id | Runs in | Used by |
|---|---|---|
| `pi:<provider/model>` | pi | implementers, fallbacks, fix rounds, teammates |
| `pi:<local provider>/<model>` | pi, on a local server | the same, see [Providers](#providers) |
| `claude:<model-id>` | Claude Code with that model | implementers, `test_reader`, teammates |
| `claude` | Claude Code with its default model | implementers, teammates |
| `codex:<model>` | Codex with that model | implementers, `test_reader`, teammates |
| `codex` | Codex with its default model | implementers, teammates |
| `<local provider>/<model>` | OpenCode, on a local server | implementers, fallbacks, fix rounds |
| anything else | OpenCode | implementers, fallbacks, fix rounds |

- `test_reader` takes a `claude:` or `codex:` id and runs headless with read-only tools.
- Teammate sessions (`models.crew`) run in Claude Code, pi or a harness from [harnesses.md](harnesses.md), so an OpenCode id there stops `crew start` with a line saying so.
- `run` and `resume` send every model they may call one tiny prompt before they clone anything.
  A wrong id or preset stops the run there with the provider's message.
- Crew models are probed when a teammate starts, and a good answer is cached for a day in `model-probe.json`.
  `compose models --probe` clears the cache and asks again.

## Models and the fallback

- `fallback_after` counts misses, errors and timeouts.
  A task stays on `implementer` until it has that many failed attempts, then moves to `fallback` in a fresh session that carries the failure.
- A rate limit, or a provider too busy to start the turn (a 429 or a 5xx before any token), moves the task at once.
  With no fallback it gets a fresh session after a wait and the attempt is not counted.
- `bench` never falls back.
- `fix` is read once at startup and set as `COMPOSE_FIX_MODEL`.

## Crew models

`models.crew` picks each teammate's model by role.

- An entry is a model id, or `{"model": ..., "effort": "high", "fallback": ...}`.
- `effort` sets Claude Code's `--effort`, or pi's `--thinking` for a `pi:` model.
- `fallback` is one entry or an ordered list.
  `crew start` probes the model, then each fallback in turn, and takes the first that answers.
  When none answers it stops before opening a tab and names each model with its error.
- Two names are not roles: `default` is used by any role with no entry, and `small` is a name to pass to `crew start --model small`.
- A role is looked up in this order: its own entry, then the entry named by its first word (`design-003` finds `design`), then a built-in default (the designer is Opus 5.5 at high effort), then `planner` for the architect, then `default`, then bare `claude`.
- The team lead is `orchestrator`, else `default`.
  It delegates, so it never uses `planner`.

## Model tiers, prices and the frontier budget

Every model belongs to one of three tiers: `cheap`, `standard` and `frontier`.
Each role starts on the highest tier it may use: `small` and `test_reader` on cheap, `explore`, `research`, `perf`, `toolsmith`, `groundwork`, `orchestrator`, the implementer and the code-writing `tests` and `fix` roles on standard, and `planner`, `architect`, `designer` and `designer-plan` on frontier.
An implementer with no `model:` of its own starts on cheap, or on standard when its route's depth is `DEEP`.
A cheap model that fails `fallback_after` attempts moves to the first standard model.
`compose tier` pins a tier for a story or the whole project, and a task's `tier:` key pins one task ([cli.md](cli.md), [tasks.md](tasks.md#model-tier)).

`models.tiers` replaces the list of a tier, and an entry is a model id or `{"model": ..., "effort": "high"}` as in [Crew models](#crew-models).
The first entry of a tier is the model its implementers use, and a teammate whose `models.crew` chain holds models above its tier drops them.
A teammate that starts on a lower model than its entry keeps the entry's effort, and `crew start` and the teammate's record name the tier and what lowered it (the role's ceiling, or a tier pin or the frontier budget).
A role name with a number after its name, such as `groundwork2` or `groundwork-2`, takes the entry and ceiling of `groundwork`.
The default lists are:

| Tier | Models |
|---|---|
| `cheap` | `claude:claude-haiku-5-5`, `pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast` |
| `standard` | `claude:claude-sonnet-5-5` |
| `frontier` | `claude:claude-opus-5-5`, `claude:claude-fable-5-1`, `pi:openai/gpt-6-astra`, each at high effort |

```json
"models": { "tiers": { "frontier": [{ "model": "claude:claude-opus-5-5", "effort": "high" }] } }
```

A Codex session with no `harnesses.codex` block also puts every entry of these lists on a `claude` or `claude:<id>` model on bare `codex`, with its effort kept, and a `codex` block still replaces that default whole, see [harnesses.md](<harnesses.md>).

`prices` gives the list price of a model in dollars per million tokens: two numbers (input, output) or four (input, output, cache write, cache read).
Your entries are added to the built-in ones and win over them.
`compose status` uses them for the spend of each tier, as reference dollars at list prices for a Claude Code call and as real dollars for any other model.
A model with no price shows `+?` beside its tier.

```json
"prices": { "pi:ollama/qwen3-coder:30b": [0, 0], "claude:claude-opus-5-5": [4, 20, 5, 0.2] }
```

The frontier spend of a project is held to a limit, 25 dollars unless `compose budget --project <slug> frontier <dollars>` sets another.
The limit lives in the project's `results/budget.json` as `{"frontier": 25}`.
A project that has spent the limit starts new frontier work on standard and reports `needs-user` to the team lead, until the limit is raised.
A file that is not an object, or a `frontier` that is negative or not a number, stops the command with a line that names the file.

## Harnesses

A harness is the program that runs a model, and [harnesses.md](harnesses.md) lists them.
A `harnesses.<name>` block is merged over the workspace's settings one level deep for a session of that harness.
Each block needs `models.crew.default`.
Claude Code needs no block, and Codex has a built-in one that a `codex` block replaces, see [harnesses.md](<harnesses.md>).
The `harnesses` key itself is gone from the merged settings.

```json
{
  "models": {
    "crew": { "default": "claude:claude-sonnet-5-5" }
  },
  "harnesses": {
    "codex": {
      "models": {
        "crew": { "default": "codex" }
      }
    }
  }
}
```

A Codex session reads its crew models from the `codex` block, else from the default made from the top level, which puts every Claude model on bare `codex`, and every other key from the workspace.

## Providers

`providers` declares OpenAI-compatible servers, such as Ollama, LM Studio, or vLLM, that can run a model for free.

```json
"providers": {"ollama": {"base_url": "http://localhost:11434/v1", "models": ["qwen3-coder:30b"]}}
```

| Field | Takes | Means |
|---|---|---|
| (the map key) | lowercase letters, digits, `-` and `_`, starting with a letter or digit | The provider's name. `openrouter`, `openrouter-fast`, `codex`, `gemini` and `grok` are reserved. |
| `base_url` | a URL starting with `http://` or `https://` | Required. The server's OpenAI API root, such as `http://localhost:11434/v1`. |
| `models` | a list of model ids | Optional. When set, a model of this provider that is not in the list stops every command. When unset, any id is accepted. |

Using one:

- `pi:<provider>/<model>` runs the model in pi.
  `compose` writes `~/.compose/pi-providers.js` from this map and gives it to every pi it starts, and rewrites it when the map changes.
  pi's own `models.json` is never touched.
- A bare `<provider>/<model>` runs it in OpenCode.
- Any model setting can name one: `models.implementer`, `fallback`, `fix`, and every `models.crew` entry (those must use the `pi:` form).
  `test_reader` cannot, since it takes a `claude:` or `codex:` id.
- Claude Code never runs a local model.
- A local call costs `$0.0000` and shows up in the cost line as a row under the provider's name, with its calls and tokens.

A server that needs a key gets one per workspace:

```bash
pbpaste | compose workspace key work --provider ollama
```

The provider must be declared first.
The key goes into `auth.json`, and `compose` hands it to the harness as `<NAME>_API_KEY` (the name in capitals, every other character as `_`, so `lm-studio` gives `LM_STUDIO_API_KEY`).
A workspace with no key of its own for a provider uses `default`'s.
A server with no key needs no command, and nothing is sent in an `Authorization` header.

Before any probe or run, each local provider the command will call must answer `GET <base_url>/models` within 5 seconds.
Any HTTP answer counts, even an error status, since it shows the server is up.
When it does not answer, nothing is probed, cloned or run, and the line says:

```text
local provider ollama at http://localhost:11434/v1 is not answering: start the server, then run again
```

`compose doctor` shows one row per declared provider.

## Workspaces

A workspace is one set of keys plus optional overrides.
`default` is the top level of the file itself, so it always exists and is active when `workspace` is unset.
A name under `workspaces` holds the same keys as the top level, and the active one is merged over the top level one level deep:

- For a plain value (`parallel`, `claude_parallel`, `branch_prefix`, `max_stack_prs`), the workspace's value replaces the top-level one.
- For `models`, `env`, `providers` and `notify`, the workspace's entries replace the top-level entries of the same name, and the top-level entries it does not name stay.
- That is one level only.
  A workspace's `models.crew` replaces the whole top-level `models.crew`, and a workspace's `providers.ollama` replaces the whole top-level `providers.ollama`, so give it its own `base_url` and `models`.

A `workspace` naming an entry that is not under `workspaces` stops every command.
The key commands, the names and the billing rules are in [workspaces.md](workspaces.md).

## Env

`env` is a map of variable names to values.
Each is set in the CLI's environment, so pi, OpenCode, Claude Code and the tools they start inherit it.
Values are turned into strings.
A name ending in `_API_KEY` is treated as a secret and kept out of logs, but keys still belong in `auth.json`, not here.

## Parallel limits

- `parallel` is how many agents start at once.
  `run` and `resume` use the flag, else this, else 3 (`resume` falls back to the halted run's value).
  `testcrews` starts every writer at once when it is unset.
  `designcrews` never reads it, since Figma's write rate limit caps its builders.
- `claude_parallel` is a second cap for Claude Code sessions only, because parallel Claude writers slow a machine more than the same number of pi calls.
  pi and OpenCode tasks do not count against it.
  A task that only falls back to Claude mid-run is not counted either.
- A value that is not a whole number of 1 or more stops every command.

## Branch names and stack size

- `branch_prefix` starts every new project's branch.
  `init` reads it once and stores the full branch in the project's `verify.json`, so changing it later affects new projects only.
  An empty string names branches after the slug alone.
  Task, test and layer branches are that branch plus `-NNN`, `-tests-NNN` and `-layer-NN`.
- `max_stack_prs` caps the PRs one stack opens.
  The same key in a project's `verify.json` overrides it.

## Phone notifications

`notify` is off unless the file has it, and nothing turns it on for you:

```json
{"notify": {"ntfy": "https://ntfy.sh/<your-topic>"}}
```

With it, compose posts one line to that [ntfy](https://ntfy.sh) topic when a gate stops that only you can clear:

- A billing halt (the line says to top up, then `compose resume`).
- An agent blocked on a prompt only you can answer, such as a sign-in or an allow.
- A verified stack waiting for your push approval, while away or yolo is on with push off.
- A teammate's `needs-user` or `blocked` report that asks for a sign-in, a payment, an approval or a push, while away or yolo is on.

The message is the project slug as the title and a fixed one-line summary, so it holds no key, no code and no free text from a report.
It goes by `urllib` with a 10 second limit, and a topic that does not answer never stops a run.
A value that is not an `http://` or `https://` URL turns it off.
In a workspace, `notify` merges one level like `models`, so the workspace's `ntfy` replaces the top-level one.
Anyone who knows the topic name can read it, so pick a long random one.

## Files beside it

All of these live in `~/.compose/` (the folder of `config.json`).

| File | Holds | Edit it? |
|---|---|---|
| `config.json` | This page. | Yes. |
| `auth.json` | Every key, at mode 600 in a directory of mode 700: `{"workspaces": {"<name>": {"openrouter": ..., "linear": ..., "provider:<name>": ...}}, "typesafe": ...}`. | Through `compose workspace key`, never by hand. |
| `.env` | `TEXT_MODEL*` and `TYPESAFE_MODEL` settings for the fast model (the review when `models.review` is unset) and jev's text helper. The process environment wins over it. | Yes, for those settings. |
| `model-probe.json` | The day's cached crew model probes. | No. `compose models --probe` clears it. |
| `pi-providers.js` | The pi extension generated from `providers`. | No. It is rewritten when `providers` changes. |
| `<name>.key` | A 0600 key file for OpenCode: `openrouter.key`, or `<provider>.key` for a keyed local provider. | No. `compose` writes it. |

## When the config is wrong

A bad entry stops the command with one line that names the entry.

| Line | Fix |
|---|---|
| `active workspace '<name>' is not under "workspaces"` | Add the workspace, or set `workspace` to one that exists. |
| `provider names are lowercase letters, digits, - and _: '<name>'` | Rename the provider. |
| `providers.<name> is reserved: openrouter models already route there` | Pick another name. |
| `providers.<name> needs a base_url such as http://localhost:11434/v1` | Give it a `base_url` that starts with `http://` or `https://`. |
| `providers.<name>.models must be a list of model ids` | Make `models` a list of strings, or remove it. |
| `<model> is not in providers.<name>.models: <the list>` | Add the model to the list, or use one that is in it. |
| `parallel must be a whole number of 1 or more` | Same for `claude_parallel`. |
| `models.tiers.<tier>: tiers are cheap, standard, frontier` | Rename the tier. |
| `prices must be an object of model id to 2 or 4 numbers, not <value>` | Make `prices` a map of model id to a list of 2 or 4 numbers. |
| `prices.<model> must be 2 or 4 numbers` | Give that model 2 numbers (input, output) or 4 (input, output, cache write, cache read). |
| `providers.<name> is reserved: codex, gemini, grok name harnesses, not providers` | Pick another name. |
| `no "harnesses.<name>" block` | Add the block for that harness, which only `gemini` and `grok` need today, see [Harnesses](<#harnesses>). |
| `harnesses.<name>.models.crew has no "default"` | Add `models.crew.default` to the block. |
| `... teammate sessions run in Claude Code, pi, ...: use ...; OpenCode ids are for implementers only` | Use an id of a harness the line names under `models.crew`. |

## Checking it

- `compose workspace` lists the workspaces and shows the active models.
- `compose doctor` checks the keys by presence only and shows one row per local provider.
- `compose models --probe` clears the cached probes and asks every crew model again.
- `compose run` and `compose verify` print `workspace <name>` first, so check it before spending.
