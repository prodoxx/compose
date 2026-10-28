# jev

jev is the browser agent compose's verify drives for its e2e cases, and the one `bp goal` runs.
Jev, TypeSafe's model, chooses the next action from the ones jev observed on the page; jev's own code carries the action out, so the model never writes selectors or scripts.
It came from [jev-ultrafast](https://github.com/browser-use/jev-ultrafast) at `05b841f` and keeps only the library (`jev_ultrafast/`) and its tests.

## Run it

```sh
cd jev
BU_CDP_URL=http://127.0.0.1:9334 uv run python -m jev_ultrafast http://localhost:5173 "Sign in and open Settings, then stop"
```

You rarely run it by hand: verify starts it with `uv run` from this folder and passes `~/.compose/.env` when it exists, and `bp goal` does the same against bp's Chromium.

- It attaches to the Chromium at `BU_CDP_URL` (or `BU_CDP_WS`); it never launches a browser itself.
- Several goals are joined, in order, into one task.
- `--secrets '{"Password": "$E2E_PASSWORD"}'` names env vars for field labels: jev types each value itself, so it never reaches a model, the history or a command line. A literal value or an unset variable is refused before the run starts.
- `--record DIR` keeps a screenshot of every step, `--progress` prints a line per step, and `--hold` keeps the tab open after the result until stdin closes, so verify's assert can read it.

The last line printed is always `JEV_RESULT {json}`: the status (`done`, `blocked` or `error`), the stop reason, the steps, the final url, token usage and a compact history.
A budget or a crash still ends with that line, so the caller always learns why a run stopped.
The exit code is 0 only when the goal is done.

## What it can see

The page snapshot walks open shadow roots and same-origin iframes, and a `HOVER` action opens controls marked `aria-haspopup` or `aria-expanded`.
Closed shadow roots and cross-origin iframes are out of reach; drive those with a `driver` case over CDP instead (see [docs/verify.md](../docs/verify.md)).

## Environment

| Variable | Use |
|---|---|
| `TYPESAFE_API_KEY` | Required: the Jev model's key. |
| `TYPESAFE_MODEL` | The Jev model, `jev-latest` by default. |
| `TEXT_MODEL_API_KEY`, `TEXT_MODEL_BASE_URL`, `TEXT_MODEL` | The text helper that writes field values, an OpenAI-compatible API (`https://api.deepseek.com/v1`, `deepseek-chat` by default). |
| `BU_CDP_URL` / `BU_CDP_WS` | The browser to attach to. |

## Layout

- `agent.py` - the step loop: observe, choose, act, check the page changed.
- `browser.py` and `snapshot.js` - the CDP connection and the in-page snapshot of actions, text and fields.
- `model.py` and `questions.py` - the Jev and text-model calls and their budgets.
- `tiles.py` - cuts Figma renders into tiles for compose's Figma judge (`compose figma tree`).

## Tests

Run `cd jev && uv run pytest`.
They run offline, with no browser and no model.
