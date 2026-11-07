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
When the ticket has no `## Intent` (a feature or epic), write it here as sections/0b.md says, in one pass; the shaping conversation runs only when one of 0b's triggers holds, and then the planner starts after the product owner approves.
A story labeled `deep-dive` with no tech doc gets its deep dive here first.
Start the planner:
`compose planner --project <slug> --issue <id> | compose crew start --project <slug> --role planner --title planner --brief -`
The planner reports to you, since you started it, so its reports reach you through the watch the Stop hook parks, the `--from orchestrator` watch for a live team lead included.
Start no watch for it: the parked one already reports the planner.
Relay every report to the product owner as 0h describes, in one or two sentences each.
For a `needs-user` with open questions, ask them here one at a time as 0h says, recording each answer with `compose decide --project <slug> --issue <issue> --new "<decision>" --quote "<their words>" --closes <id>`.
Then tell the planner `compose crew prompt --project <slug> --role planner "decided: <ids>; read Decisions so far"`, and end your turn; the hook parks the next watch.
For a `working "stories <issue>: ..."` report, relay it in one line; an edit the product owner asks for goes to the planner as `compose crew prompt --project <slug> --role planner "<their words>"`.
Read the planner with `compose crew read --project <slug> --role planner --lines 80` before relaying anything unclear.
In this session, never plan, write stories, or cut tasks.
Also never run `compose run`.
