# bp

One file, no dependencies.
`bp` drives a Chromium over raw CDP, with an unpacked Chrome extension loaded when you pass one, so an agent can explore or test a web app or extension the way a user would.
It opens pages, reads what an extension panel shows, clicks, types, uploads, screenshots, and records.
Verify's e2e stage uses it to launch its own browser (see [docs/verify.md](../docs/verify.md)).

## Install

```sh
ln -s "$PWD/bp/bin/bp.mjs" ~/.local/bin/bp      # from the compose checkout
```

It needs Node 22+ and Playwright's Chromium (`npx playwright install chromium`), because Chrome 154+ ignores `--load-extension`.
Set `BP_CHROME` to use another build.

## A typical session

```sh
export BP_PORT=9333 BP_EXT=/path/to/unpacked-extension
bp launch https://example-ats.com/job --viewport 390x844   # --viewport sets the window size for mobile shots; the profile persists at /tmp/bp-profile-9333, so logins survive a relaunch
bp list                                           # find the page id
bp hosts <id> --out shots/panel.png               # extension panel text and buttons with coordinates
bp clicktext <id> "^Autofill$"                    # or: bp click <id> 1008 301
bp form <id>                                      # which fields got filled
bp record example-ats.com shots/flow --interval 2000 &   # a screenshot on every change while a human walks a flow
```

`bp help` lists every command.
`bp goal <url> "<goal>"` runs compose's `jev/` against the same Chromium, the same agent verify's e2e cases use.
It prints a line per step and ends with a `JEV_RESULT {...}` line that holds the status, the stop reason, and the steps taken.
It exits 0 only when jev reached the goal, so a failed or blocked goal fails a script that runs it.
`--secrets '{"Password": "$E2E_PASSWORD"}'` lets it log in through a form: jev types that variable's value into the field with that label itself, so the value never reaches its model.
It keeps a `BU_NAME` you set, so the caller can end the browser-harness daemon it starts.
Everything else is plain CDP with no model: it does only what you type.

## Rules for agents

- Run `bp help` once. Every command prints what it did; read it before the next step.
- Get a page id from `bp list` and reuse it; URL substrings also work but can match several tabs.
- Run `bp hosts <id>` before clicking inside an extension panel. Panels live in shadow roots, and coordinates change with the viewport and scroll. Re-run `hosts` or `snap` after any navigation.
- Prefer `clicktext` over coordinates when the control has a stable label. Use `--host` on `snap` to limit it to one shadow root.
- Never let a flow click a final Submit on a real employer form. Check `bp form <id>` and a screenshot instead.
- Screenshots and notes go next to the work (`shots/`, `notes/`), with an `index.md` line per screenshot. `bp record` writes that index for you.
- `bp cookies <id> clear` logs the human out of that site. Say so in your report and ask them to log back in.
- `BP_PORT` has no default, and `launch` refuses a port that already has a browser, so you never attach to a browser someone else (the product owner) runs. Pick a port only you use.
- Use one Chromium per port, for example `BP_PORT=9334` for a second extension, so profiles and extensions do not mix.
- Do not hard-code coordinates in anything reusable. They are only valid for the current viewport.
- `bp goal` hands control to jev's model. State the stopping point in the goal text, and verify the result with `bp form` and a screenshot; a DONE from the model is not proof.
- If a command fails twice, stop and report what you saw instead of retrying.

## Tests

Run `cd bp && npm test`.
The browser test is skipped when Playwright's Chromium is not installed.
