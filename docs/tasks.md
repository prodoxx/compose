# Task issues

Each task is a Linear issue with a brief the implementer follows and a block the CLI reads.
This page shows that format and what `sync` does with it.

A task is a Linear issue under the project's feature, directly or under a story.
Its title is the task's title.
Its description is the implementer's brief and ends with the block the CLI reads.
A complete description looks like this:

````markdown
## Goal
One paragraph. What to build, where, exact export names.

## Acceptance criteria
- One bullet per behavior. Each is checked by the test.

## Test plan
- How to run: the `test:` command below.
- Covered: which behaviors the test exercises, in words.
- Holdout: the test has cases not listed here.

## Constraints
- No new dependencies beyond what the goal names. Do not edit protected files.

```compose
id: 002
files: extension/snapshot.js
protected: tests/snapshot.test.js
test: node --test 'tests/**/*.test.js'
```
````

The `## Test plan` may end with an optional `scenarios` fence, one scenario per acceptance criterion:

````markdown
```scenarios
id: 1
scenario: A snapshot of an empty page is empty
given: a page with no nodes
when: snapshot runs
then: it returns an empty list
negative_cases: a null page
test_type: unit
oracle: the returned list
```
````

- A line `id: <n>` starts a scenario, and the ids run from 1 to the number of criteria, in order.
- The keys are `id`, `scenario`, `given`, `when`, `then`, `test_type` and `oracle`, all required, and `negative_cases`, optional.
  A line that is not `key: value` joins the previous value after one space.
- The test writer writes a case per scenario and per negative case, then adds holdout cases none of them lists.
  The regression writer's brief carries the block too.
- `spec-check` fails a task whose scenario lacks a required key, or whose ids do not match its criteria.
  A task without the block is read exactly as before.

Three more rules apply to the block and the issue around it:

- The `after` field comes from the issue's "blocked by" links to other tasks of the feature.
  Each linked task becomes the layer this task stacks on.
- Any other "blocked by" link (a design story, another feature's issue) does not stack.
  `run` holds the task until that blocker is Done or Canceled, or In Review with the Design label, as of the last `sync`.
  A blocker in any other state is let go when git shows its code in the trunk the run starts on: the task branch of the blocker, or the top layer of the project that builds it once all its tasks passed, is an ancestor of that trunk.
  `run` prints the unblocked ids with that proof in its first lines, and the blockers still held with the reason.
- The `model:` field picks another implementer for one task.
  Leave it unset unless the product owner decided otherwise.
- Only an issue with the block counts as a task.
  Stories, for example, have none.

## Model tier

The optional `tier:` line pins one task to `cheap`, `standard`, `frontier` or `no-frontier`.
It beats the pin of the task's story and of the project (`compose tier`).
It picks the model of an implementer only while the block has no `model:` line, because `model:` names the model itself.
Any other value stops the task from loading with `<id>: tier must be cheap, standard, frontier or no-frontier, not <value>`.

```compose
id: 003
files: src/cart.js
test: node --test tests/cart.test.js
tier: standard
```

## Design tasks

A design task is the same issue with `kind: design` and a `nodes:` line in place of the `files:` line.
The `nodes:` line lists the page and frames the builder draws, like `Checkout/Cart, Checkout/Pay`.
The optional `figma:` line is a file key or url for a task whose file is not the project's stored one.
The `test:` command is `compose figma judge --project <slug> --only NNN`.

````markdown
```compose
id: 006
kind: design
nodes: Checkout/Cart, Checkout/Pay
figma: AbCdEf123456
test: compose figma judge --project my-feature --only 006
```
````

Three rules apply to design tasks.
Two design tasks never share a page.
A design task never runs, stacks, or is judged from files.
The builder saves the node tree as text under `results/`, and the judge reads it.

## Right-sized

A task is ready when it meets all four of these:

- It touches one or two files.
- It has one test command, and it runs only the task's own test (its file, or a name filter), never the whole suite.
- It has at most five criteria.
- Its test fails on HEAD.

If you cannot write the test, it is not a task yet.

A test that pins a line width or exact text puts that text in the task's brief, wrapped line by line as the test expects.
A model cannot count columns, so a brief that only states the limit fails every attempt.

## Sync

`compose sync` reads the feature, its stories, and their tasks from Linear with the workspace's key (read-only, descriptions whole) and stores them.
`compose sync --stdin` takes JSON of this shape instead, for scripts and tests:

```json
{"feature": {"id": "ENG-123", "title": "...", "description": "<the feature's description, plan included>"},
 "issues": [{"id": "ENG-124", "title": "...", "description": "<brief + compose block>", "status": "Todo", "blockedBy": ["ENG-125"]}]}
```

`sync` refuses a feature other than the one `linear.json` names, a block without `id` or `test`, and an id used twice.
`run`, `verify`, `resume`, `bench`, and `tasks` read what was last synced, so the team lead syncs right before each.

The CLI writes to Linear only to keep states true, and only when the active workspace has a Linear key.
The team lead writes everything else on Linear with `compose linear`.

`.compose/<project>/` holds only the CLI's files.
Those are `linear.json`, `verify.json`, `results/`, the wayfinder's Lavish page, and the Frame0 design file.
Nothing about the work is written to a file in the repo.
