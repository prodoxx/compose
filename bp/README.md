# bp

`bp` drives a Chromium over raw CDP, so an agent can explore or test a web app the way a user would.
It is one Node file with no dependencies.
It opens pages, reads what they show, clicks, types, uploads, takes screenshots and records flows.
Verify's e2e stage uses it to launch its own browser (see [docs/verify.md](../docs/verify.md)).

## Install

```sh
ln -s "$PWD/bp/bin/bp.mjs" ~/.local/bin/bp      # from the compose checkout
```

It needs Node 22+ and Playwright's Chromium (`npx playwright install chromium`).
Set `BP_CHROME` to use another build.

## A typical session

```sh
export BP_PORT=9334                               # required: a port only you use
bp launch http://localhost:5173 --viewport 390x844   # the profile persists at /tmp/bp-profile-<port>, so logins survive a relaunch
bp list                                           # find the page id
bp snap <id> --out shots/home.png                 # interactive elements with coordinates
bp clicktext <id> "^Sign in$"                     # or: bp click <id> 1008 301
bp form <id>                                      # which fields are filled
bp record localhost:5173 shots/flow --interval 2000 &   # a screenshot on every change while a human walks a flow
bp kill                                           # stop the Chromium on this port
```

`bp help` lists every command.
Everything except `bp goal` is plain CDP with no model: it does only what you type.

## bp goal

`bp goal <url> "<goal>"` runs compose's [jev](../jev/README.md) against the same Chromium, the same agent verify's e2e cases use.
It prints a line per step and ends with a `JEV_RESULT {...}` line that holds the status, the stop reason and the steps taken.
It exits 0 only when jev reached the goal, so a failed or blocked goal fails the script that runs it.
`--secrets '{"Password": "$E2E_PASSWORD"}'` lets it log in through a form: jev types that variable's value into the field with that label itself, so the value never reaches its model.
It keeps a `BU_NAME` you set, so the caller can end the browser-harness daemon it starts.

## Rules for agents

- Run `bp help` once. Every command prints what it did; read it before the next step.
- `BP_PORT` has no default, and `launch` refuses a port that already has a browser, so you never attach to a browser someone else (the product owner) runs. Pick a port only you use, and use one Chromium per port.
- Get a page id from `bp list` and reuse it. URL substrings also work but can match several tabs.
- Run `bp snap <id>` (or `bp hosts <id>` for content inside shadow roots) before clicking, and again after any navigation: coordinates change with the viewport and scroll.
- Prefer `clicktext` over coordinates when the control has a stable label. Never hard-code coordinates in anything reusable.
- Never let a flow submit a real form with side effects (a payment, a sign-up, a message). Check `bp form <id>` and a screenshot instead.
- Screenshots and notes go next to the work (`shots/`, `notes/`), with an `index.md` line per screenshot. `bp record` writes that index for you.
- `bp cookies <id> clear` logs the user out of that site. Say so in your report.
- `bp goal` hands control to jev's model. State the stopping point in the goal text, and verify the result with `bp form` and a screenshot; a DONE from the model is not proof.
- If a command fails twice, stop and report what you saw instead of retrying.

## Tests

Run `cd bp && npm test`.
The browser test is skipped when Playwright's Chromium is not installed.
