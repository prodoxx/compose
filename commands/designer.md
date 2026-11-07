---
description: "Start the designer teammate for a ticket: continues design work in Figma from where it stopped"
argument-hint: "<Linear url or id> [--figma <file url or key>]"
allowed-tools: Bash(compose designer:*)
---
The ticket and options are `$ARGUMENTS`.

1. Invoke the compose skill as the engineering manager, and resolve the ticket's level and its feature (0, 0j).
2. Find the project for that feature with `compose projects`, else create it with `compose init --project <slug>`.
3. When `--figma` is given, run `compose figma check --project <slug> --figma <file>`.
4. The owner of the ticket's level starts the designer in its own pane.
   For a feature or story whose project has a live team lead (`compose crew list --project <slug>` shows `<slug>-orchestrator`), hand the start to it: `compose crew prompt --project <slug> --role orchestrator "start the designer for <ticket>: compose designer --project <slug> | compose crew start --project <slug> --role designer --title designer --brief -"`.
   For an epic, or when no team lead is live, start it yourself with that same pipeline: `compose designer --project <slug> | compose crew start --project <slug> --role designer --title designer --brief -`.
5. Relay its reports to the product owner as 0h describes; its reports reach you through the watch the Stop hook parks, and you start no watch for it.
   A designer you started reports to you, so the parked watch reports it whatever `--from` says, the `--from orchestrator` watch for a live team lead included.
   A designer the team lead started reports through the team lead, which the `--from orchestrator` watch already covers.
   Forward the product owner's answers with `compose crew prompt --project <slug> --role designer "<answer>"`.

Never design in this session: no Figma write and no design issue here.
The designer and its crew do that work in their own panes (SKILL.md 0d).
