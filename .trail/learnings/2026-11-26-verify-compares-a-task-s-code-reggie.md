---
title: "verify compares a task's code and its rendered page with the Figma node it names (#496)"
author: Reggie
date: 2026-11-26
branch: main
cost: { minutes: 18, tokens: 403602 }
touches:
  - SKILL.md
  - bin/compose.py
  - bin/fc_accept.py
  - bin/fc_briefs.py
  - bin/fc_figma.py
  - bin/fc_implement.py
  - bin/fc_ledger.py
  - bin/fc_planner_brief.py
  - bin/fc_review.py
  - bin/fc_spec.py
---

## Decided
- `figma_nodes:` is now read after the build too. `fc_designmatch.task_docs` keeps the self-contained Framelink JSON `run` fetched in `results/figma/nodes/<key>-<node>.json`; `code_nodes` writes the clone's `.figma/` from the same call, so verify holds the build to the design the implementer was given and makes no fetch.
- Verify's design stage (`fc_designjudge.stage`, one line in `run_stages`) judges a match, not a violation: the fast model lists mismatches between the node's facts and the task's own files at the tip, a row must name an element id of the node, a value its fact line holds, and a line the code holds, then a second call confirms it. It gates regardless of `acceptance_gate`, because no test covers the look.
- Mismatch ids hash task + element + property (`fc_review.design_id`), not the model's words, so a dismissal survives a rerun. `fix_answers` counts a fix that rewrites the quoted code line.
- The rendered measurement is an e2e case's assert, `compose figma match --only <task>`, so it reuses the e2e stage's setup, ports, browser, retry and findings instead of a second browser lifecycle. A frame is matched to the built element that holds exactly its texts and carries most of its properties; one element may stand for nested frames.
- `spec-check` fails an open task with `figma_nodes:` whose criteria hold no hex and no px value (`fc_designmatch.MEASURED`); built tasks (In Review, Done) are left alone.

## Tried and ruled out
- Adding the design row to `jev_acceptance`'s criteria: `fc_ledger.verify_record` looks each row up in the task's own criteria (`texts.index`), a passing unit test would mark it `proven_by`, and the CONFIRM prompt hunts a violation. A separate stage with its own report key was simpler.
- A one-to-one frame-to-element assignment (each built element taken once): a design nests wrapper frames a build flattens into one div, and every outer frame then read as "no element".
- A `%`-formatted JS template: the script holds `%` itself; the root selector goes in by `replace("__ROOT__", ...)`.

## Watch out
- `fc_verify.py` is at 581 of its 600 lines; new stages belong in modules of their own.
- An e2e case keeps only the last 3000 characters of stdout, so `figma match` prints at most 20 rows with the summary last and writes the whole table to `results/design/<id>.match.txt`.
- `getComputedStyle` returns colours in the colour space they were written in (oklch with Tailwind 4); the measure script reads them back through a 1x1 canvas.
- Design ledger rows carry no criterion id, which the ledger reads as "all criteria": `jev_sufficiency` leaves the `design` phase out so it is never cited as proof of a criterion.
- Not run live: a whole `compose verify` on a real project, and a fetch of a real Figma node. The judge was run against the live fast model and `figma match` against a real headless Chromium, both on a fixture node in Framelink 0.13.2's shape.
