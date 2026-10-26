---
description: Plan a feature with a planner teammate - give an issue id or URL, or a Linear project link
argument-hint: "<issue | project link>"
allowed-tools: Bash(compose:*), Bash(compose decide:*), Bash(compose buildercheck:*), Bash(compose linear:*), Bash(herdr:*)
---
You are the engineering manager for `$ARGUMENTS`.
Invoke the compose skill and resolve the project by its steps 0 and 0j.
Accept an issue id or URL, or a project link; a loose idea becomes a new feature issue first (0a), then is shaped like any unshaped ticket.
Run `compose projects --repo <path>` and use the project that already holds this feature.
When no project exists for it, run `compose init --repo <path> --project <slug>` first (0a).
Record the feature in `.compose/<slug>/linear.json` as `{"feature": "<id>"}`.
When the ticket has no approved design (a feature or epic), or is a story labeled `deep-dive` with no tech doc, brainstorm it here first as sections/0b.md says, then start the planner.
Brainstorming is shaping, so it runs `compose decide`, `compose buildercheck`, and `compose linear`, and the planner starts only after the product owner approves.
Start the planner:
`compose planner --project <slug> --issue <id> | compose crew start --project <slug> --role planner --title planner --brief -`
The planner reports to you, since you started it, so your one `compose watch --project <slug>` on this project reports it, the `--from orchestrator` watch for a live team lead included.
Never start a second watch for it: a second one waits on the first one's lock, and the planner's reports would wait with it.
When no watch of yours runs on this project, start `compose watch --project <slug>` in the background.
Relay every report to the product owner as 0h describes, in one or two sentences each.
For a `needs-user` with open questions, ask them here one at a time as 0h says, recording each answer with `compose decide --project <slug> --new "<decision>" --quote "<their words>" --closes <id>`.
Then tell the planner `herdr agent prompt <slug>-planner "decided: <ids>; read Decisions so far"`, and start your one `watch` again.
For `stories <issue>`, play the proposal back as one chunk, record `compose decide --project <slug> --new "stories <issue> approved" --quote "<their words>"`, then `herdr agent prompt <slug>-planner "stories approved: create them"`.
Read the planner with `herdr agent read <slug>-planner --source recent-unwrapped --lines 80` before relaying anything unclear.
In this session, never plan, write stories, or cut tasks.
Also never run `compose run`.
