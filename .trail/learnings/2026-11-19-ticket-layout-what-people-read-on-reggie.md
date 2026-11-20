---
title: "Ticket layout: what people read on top, the planning and decision log under a rule"
author: Reggie
date: 2026-11-19
branch: main
cost: { minutes: 9, tokens: 153360 }
touches:
  - bin/fc_change.py
  - bin/fc_decide.py
  - bin/fc_evidence.py
  - bin/fc_linear.py
  - bin/fc_linear_cmd.py
  - bin/fc_plan.py
  - bin/fc_planner_brief.py
  - bin/fc_readiness.py
  - bin/fc_reconcile.py
  - bin/fc_tasks.py
---

## Decided
- An epic, feature or story has two parts on Linear. On top is what product managers, designers and testers read: the opening, Intent, Goals, Non-goals, Anti-goals, Acceptance criteria. Below a `---` rule, under `## Planning and decision log` and a fixed note, sit Constraints, Decided by the product owner, My calls, Left to the builder, Decisions so far, Not yet specified, Open questions, Notes, Plan, Tech doc and Evidence. Reggie asked for this on 2026-11-19 so non-technical readers are not overwhelmed.
- The CLI owns the layout, not the agents: `fc_layout.laid_out(body, labels)` is pure and runs at every place compose writes such a body (linear create and update, decide's write_map, readiness questions, the Evidence refresh, the delivery plan link, reconcile's new tickets). A section of a name it does not know stays on the side it is on.
- The log's heading is an H2 and its sections stay H2, so every `--section "## Decisions so far"` and every reader keeps working unchanged.
- A task is never laid out: a body with a compose block is skipped, and so is a ticket without an Epic, Feature or Story label.
- Existing tickets are not rewritten in bulk; each takes the new layout the next time compose writes its body.

## Tried and ruled out
- Telling agents in sections/0w.md to keep the order by hand: `replace_section` appends a new section at the end and a dozen code paths write sections, so the order would drift.
- An H1 for the log with the working sections nested under it: `parse_criteria` and other `\n## ` regexes would run through the rule and the note.
- Linear's collapsible `+++` block for the log: nothing in compose parses it, and Reggie asked for a rule and a heading.

## Watch out
- Three readers had to learn the rule: `fc_plan.section_span` (via `log_start`), `fc_tasks.parse_criteria` and `fc_change.map_section` stop at a `---` line. Before that, the last acceptance criterion swallowed the `---`.
- `fc_readiness._without_open_questions` drops a log that holds only the note (`fc_layout.without_empty_log`); without it the readiness cache key changed after the check wrote its own questions.
- `laid_out` returns the body untouched when Linear would store both alike (`same_markdown`), so a write of an already laid out body stays a no-op.
- A new fc module goes into tests/modules/rank.txt above every module that imports it.
