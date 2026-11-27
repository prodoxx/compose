# Workspaces

Setting up a workspace takes a few commands, one config file, and one key file.
When you finish, every OpenRouter call bills the account you chose, and each teammate role runs the model you picked for it.

A workspace is one OpenRouter account and its settings.
It sets which key every call bills to, plus optional models and env.
`default` always exists.

## Commands

```bash
compose workspace                         # list (* = active, keys as their last 4 characters) and the active models
compose workspace personal                # switch; run and verify print `workspace <name>` first
pbpaste | compose workspace add client-x  # create one with the key on the clipboard
pbpaste | compose workspace key client-x  # set or rotate its key; add --linear for its Linear key
pbpaste | compose workspace key default --typesafe  # the TypeSafe key, one for every workspace
pbpaste | compose workspace key client-x --provider lmstudio  # the key of a local provider, see Local models
compose workspace remove client-x         # delete; removing the active one switches to default
```

From Claude Code, run `/compose:workspace`, `/compose:workspace personal`, or `/compose:workspace add client-x`.
The slash command never asks for a key in chat.
Type `! pbpaste | compose workspace key <name>` instead.

- Keys come from piped stdin or a hidden prompt, and never from an argument.
  They are stored in `~/.compose/auth.json`, not in the config.
- A workspace added without a key stops every command while it is active, so nothing bills another account.
- Names are letters, digits, `-`, and `_`.
  The names `add`, `key`, `remove`, and `default` are reserved.

## Keys

`~/.compose/auth.json` holds every key, at mode 600 in a directory of mode 700.
Its shape is `{"workspaces": {"default": {"openrouter": ..., "linear": ..., "provider:<name>": ...}, "<name>": {...}}, "typesafe": ...}`.
A workspace without its own OpenRouter key bills `default`'s, unless it was added with an empty key.
The first `compose` command after an upgrade moves any key still in `config.json` or `~/.compose/.env` into `auth.json` and prints one line per key.
Keys reach pi and jev through the environment, and OpenCode through a key file `compose` writes from `auth.json`, never through a command line.

## Config

The commands edit `~/.compose/config.json` (mode 600), which holds no keys.
[config.md](config.md) lists every key in it.
`~/.compose/repos.json` holds a map per workspace, `{"<workspace>": {"<owner>/<name>": "<main checkout path>"}}`: where each repo of a Linear project is checked out on this machine.
The active workspace is merged over the top level:

```json
{
  "workspace": "work",
  "models": {
    "implementer": "pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast",
    "fallback": null,
    "fallback_after": 2,
    "test_reader": "claude:claude-haiku-5-5",
    "fix": "<default: implementer>",
    "crew": {
      "planner": {
        "model": "pi:openai/gpt-6-astra",
        "effort": "high",
        "fallback": [
          { "model": "claude:claude-opus-5-5", "effort": "high" }
        ]
      },
      "default": "claude:claude-sonnet-5-5",
      "small": "claude:claude-haiku-5-5"
    }
  },
  "env": {
    "SOME_VAR": "<any env var the CLI and its tools read>"
  },
  "parallel": 20,
  "claude_parallel": 3,
  "branch_prefix": "compose/",
  "max_stack_prs": 10,
  "workspaces": {
    "work": {},
    "personal": {
      "models": {
        "implementer": "pi:openrouter-fast/z-ai/glm-5.3-flash@preset/glm-5-3-flash-fast"
      }
    }
  }
}
```

The workspace key reaches pi, OpenCode, an OpenRouter review model, and jev, so every OpenRouter call bills that account.
OpenRouter presets belong to an account, so create `glm-5-3-flash-fast` in each workspace's account.
`run` and `resume` send each model one tiny prompt first.
If an id or preset is wrong, the run stops with the provider's message.

`fallback_after` is how many failed attempts a task gets on its own model before `fallback` takes over.
A rate limit, or a provider too busy to start the turn (a 429 or a 5xx before any token), switches at once.
`test_reader` reads each failed test run and writes the implementer's fix brief.
It takes a `claude:` or `codex:` model and runs headless with read-only tools.
A `claude:` reader bills to the Claude Code subscription, never an API key or OpenRouter: every headless Claude call the CLI makes drops `ANTHROPIC_*` (key, auth token, base URL, model overrides) from its environment.
A `codex:` reader bills to the ChatGPT subscription, booked at `$0`.
`null` sends the raw test output alone.
It is not probed first: when it fails, the raw output goes back instead.

A model id names its harness:

| Id | Runs |
|---|---|
| `pi:<provider/model>` | pi |
| `claude:<model-id>` | Claude Code with that model |
| `claude` | Claude Code with its default model |
| `codex:<model>` | Codex with that model, see [harnesses.md](harnesses.md) |
| `codex` | Codex with its default model |
| anything else | OpenCode |

A Claude implementer and the test reader start lean, with `--safe-mode --strict-mcp-config`: no user CLAUDE.md, skills, plugins, hooks, or MCP servers, about 8K input tokens a turn instead of about 200K.
That flag drops the repo's own `CLAUDE.md`, `.claude/CLAUDE.md`, and `AGENTS.md` too, so the CLI puts them back in the system prompt; `@` imports inside them are not followed.

## Parallel

`parallel` is how many agents start at once.
Set it at the top level, or in one workspace to override it there.

| Command | Uses |
|---|---|
| `run` | `--parallel`, else `parallel`, else 3; Claude Code tasks also stop at `claude_parallel` |
| `resume` | `--parallel`, else `parallel`, else the halted run's value; Claude Code tasks also stop at `claude_parallel` |
| `testcrews` | `parallel` when set, else every writer at once; Claude Code writers also stop at `claude_parallel` |

`testcrews` prints the tasks past the cap, with the command that starts them.

`claude_parallel` (default 3) is a second cap, for Claude Code sessions only: `run` and `resume` start at most that many Claude Code tasks at once, and `testcrews` at most that many Claude Code test writers.
It exists because parallel Claude writers can make the machine crawl; pi and OpenCode tasks do not count against it and fill the remaining `parallel` slots.
A Claude task that waits for a slot says so once, and starts as soon as another Claude task ends.
Set it at the top level, or in one workspace, like `parallel`; a value that is not a whole number of 1 or more stops every command.
A task that only falls back to Claude mid-run, after its pi model failed, is not counted.
`designcrews` never reads `parallel`: it starts `--max`, else `design_concurrency` in verify.json, else 3 builders, to stay under Figma's per-minute write rate limit.
A value that is not a whole number of 1 or more stops every command.

## Branch names and stack size

`branch_prefix` (default `compose/`) starts every new project's branch.
`init` reads it once and stores the full branch in the project's verify.json `branch`.
An empty string gives branches named after the slug alone.
Task, test and layer branches are that branch plus `-NNN`, `-tests-NNN`, `-layer-NN`.
`max_stack_prs` (default 10) caps how many PRs one stack opens.
The same key in a project's verify.json overrides it.
Past it adjacent layers fold.
Both can be set at the top level or in one workspace, like `parallel`.

## Linear key

A workspace's Linear key (`compose workspace key <name> --linear`) is required: every `compose linear` command and the CLI's own Linear writes use it, and compose uses no Linear MCP.
It lets the CLI keep Linear's states true.
It moves tasks to In Progress and In Review, rolls up the stories and the feature, and moves the feature to In Review on verify PASS.
Without the key, `compose doctor` fails and `compose linear` stops with the line that stores one.
A Linear error never stops a run.

## Linear state names

compose reads and moves Linear issues by six state names: Backlog, Todo, Planning, In Progress, In Review and Done.
A team that names its states another way is described once, in the workspace that holds its Linear key:

```json
"workspaces": {
  "jobscan": {
    "linear_states": {
      "Todo": "Ready for Dev",
      "In Progress": "Dev In Progress",
      "In Review": "PR In Review",
      "Done": "Staged"
    }
  }
}
```

Each key is compose's state and each value is the team's name for it.
With this table a dispatched task moves to Dev In Progress, a passed one to PR In Review, and `compose clean` moves merged work to Staged.
The lines compose prints on a move name the state as the board shows it.
Everything else in compose, its docs and its agents keeps saying Todo, In Progress, In Review and Done, and `compose linear` prints and takes those names; `--state` takes the team's name too.

A value may be a list of names.
compose moves an issue to the first name in the list that the team has, and reads every name in the list as that state.
`"Done": ["Done", "Staged"]` reads a Staged issue as finished and still moves merged work to Done.

- A state of the team's own that already carries compose's name is read by it with no entry: with `"Done": "Staged"`, an issue a person moved on to Done is finished, and compose leaves it there.
- A state no entry names keeps its own name, and compose never moves an issue to it.
- A canceled state needs no entry: compose knows it by its type, whatever it is called.
- A team in the same workspace that has compose's own names is read and moved as before.
- Planning is found by name, and made when the team has none; name the team's own planning state under `"Planning"` to use that one.
- `compose workspace` lists the active workspace's table.

## Crew models

`models.crew` picks each teammate's model by role, then `default`.

- An entry is a model id, or `{"model": ..., "effort": "high"}`.
  The second form sets Claude Code's `--effort`, or pi's `--thinking` for a `pi:` model.
- An entry may carry a `fallback`, which is another entry or an ordered list.
- `crew start` probes the model it would start on, then each fallback in turn, once a day each.
  It takes the first one that answers and prints which one and why.
  When none answers, it stops before opening a tab and names each model with the error its probe got.
- The team lead's model is the `orchestrator` entry, else `default`.
  It delegates, so it never uses `planner`.
  The architect, which runs only for a change of course, uses `planner` when it has no entry.
  Groundwork and test-writing teammates use `groundwork`.
  The designer uses `designer`, Opus 5.5 at high effort when the entry is missing.
  Builders and `design-review` use `design`, else `default`.
  A role with no entry falls to the entry named by its first word (`design-003` to `design`).
- `crew start --model small` overrides the role.
  The value can be any name under `models.crew`, or a `claude:<model-id>`.

Teammate sessions run in Claude Code, pi or a harness from [harnesses.md](harnesses.md), so an OpenCode id under `models.crew` stops `crew start`.
A pi teammate session loads this repo as its skill but not Claude Code's plugin hooks, so the team lead's edit guard does not apply to it.

## Local models

A server that speaks the OpenAI chat API, such as Ollama, LM Studio, or vLLM, can run a model with no OpenRouter call and no cost.
Declare it under a top-level `providers` map in `~/.compose/config.json`, by name:

```json
{
  "providers": {
    "ollama": {
      "base_url": "http://localhost:11434/v1",
      "models": ["qwen3-coder:30b"]
    }
  },
  "models": {
    "implementer": "pi:ollama/qwen3-coder:30b"
  },
  "workspaces": {
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

- `base_url` is required and starts with `http://` or `https://`.
- `models` is optional.
  When it is set, a model of that provider that is not in the list stops every command with a line that names the list.
  When it is not set, any model id is accepted.
- A name is lowercase letters, digits, `-`, and `_`.
  `openrouter` and `openrouter-fast` are reserved.
- A workspace's `providers` entry replaces the top-level entry of the same name as a whole, so give it its own `base_url` and `models`.
  Providers it does not name stay as they are.
- A bad entry stops every command with a line that names the entry.

`pi:<provider>/<model>` runs in pi, and a bare `<provider>/<model>` runs in OpenCode.
Any role can name one: `models.implementer`, `fallback`, `fix`, and every `models.crew` entry (teammate sessions run in pi, so those use the `pi:` form).
A `pi:` model goes to pi through `~/.compose/pi-providers.js`, which `compose` writes from the `providers` map and rewrites when it changes.

A server that needs a key gets one per workspace:

```bash
pbpaste | compose workspace key client-x --provider ollama
```

The key comes from piped stdin or a hidden prompt, like the other key commands.
The provider must be declared first, at the top level or in that workspace.
The key goes into `auth.json` as `provider:<name>`.
A workspace without its own key for a provider uses `default`'s.
Every command exports it as `<NAME>_API_KEY`, with the name in capitals and every other character as `_` (`lm-studio` gives `LM_STUDIO_API_KEY`).
pi reads it from that variable, and OpenCode from a 0600 file `~/.compose/<name>.key` that `compose` writes.
The reach check sends it as a bearer token.
A server with no key needs no key command, and the reach check sends no `Authorization` header.

Before a probe or a run, each local provider it will call must answer `GET <base_url>/models` within 5 seconds.
Any HTTP answer counts, even an error status, since it shows the server is up.
When it does not answer, nothing is probed, cloned, or run, and the line says:

```text
local provider ollama at http://localhost:11434/v1 is not answering: start the server, then run again
```

`run` and `resume` put `preflight stopped; nothing was cloned or run.` in front of it.
`compose doctor` shows one row per declared provider, `local provider <name>`, with the fix `start the server at <base_url>`.

A local call costs `$0.0000`.
It appears in the cost line as a priced row under the provider's name, with its calls and tokens, for example `ollama $0.0000 (3 calls, 5120in/310out)`.

## Billing halts

Any provider can answer "payment required".
That includes OpenRouter, Cerebras or Groq through OpenCode, TypeSafe, and jev's own calls.
When one does, `run` and `verify` halt, and three things follow:

- No attempt is consumed and nothing is retried.
- Running tasks finish and no new task starts.
- The banner names the provider, quotes its message, and prints the top-up link and the exact resume command.
  The exit code is 3.

After topping up, run `compose resume --project <name>`.
Passed tasks keep their branches.
A halted verify reruns from its first stage.
`COMPOSE_FAKE_402=openrouter` simulates a halt for testing.

`run` and `resume` try not to reach a halt in the first place.
When a task runs on an OpenRouter model, they ask OpenRouter how much credit the key has left before anything is cloned.
They stop when that is less than the dearest `parallel` tasks are estimated to cost, with the numbers and the top-up link.
A task is estimated at the mean cost of that model's latest 20 finished tasks in the repo, else $0.25.
A key with no limit, a key that cannot be checked, and `--force` all start the run without a verdict.
