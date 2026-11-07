# States

Three state machines run a feature through compose: the feature itself, each task's result, and each task's Linear issue.
This page draws all three and names the command or event behind every move.
[how-it-works.md](how-it-works.md) tells the same story in prose.

## The feature

A feature is one compose project, one worktree and one gh stack.
It starts as a brief and ends when its stack merges.

```mermaid
stateDiagram-v2
    [*] --> Brief: you hand compose a Linear link or an idea
    Brief --> Explore: the repo has code
    Brief --> Intent: the repo is empty
    Explore --> Intent: codebase brief on the feature
    Intent --> Shaping: you ask, needs-shaping label, a policy names its files, or a guess is hard to reverse
    Intent --> Planning: Intent, Constraints and My calls written in one pass
    Shaping --> Planning: you approve the design
    Planning --> Stories: the feature has no stories yet
    Stories --> Tasks: stories created on your go
    Planning --> Tasks: the stories exist
    Tasks --> CutReview: the planner wrote the task issues
    CutReview --> Planning: the team lead rejects the cut
    CutReview --> Ready: the team lead accepts, you say go
    Ready --> Running: compose run
    Running --> LayerReview: a layer passed
    LayerReview --> Planning: re-split and re-run
    LayerReview --> Running: more layers to build
    LayerReview --> Stacked: every layer accepted, one gh stack built
    Stacked --> Verifying: compose verify
    Verifying --> Fixing: confirmed findings, fix rounds left
    Fixing --> Verifying: fix committed, stages rerun
    Verifying --> Escalated: fix rounds spent or only tool errors
    Escalated --> Planning: the team lead changes the plan
    Verifying --> Verified: every stage passed, feature In Review
    Verified --> Merged: gh stack merge, then compose clean
    Merged --> [*]: every story Done, feature Done

    state ChangingCourse {
        [*] --> Evidence: a task misses, a contradiction, verify fails, or you say the plan changed
        Evidence --> Screened: Jev marks each decision HOLDS, BROKEN or UNCLEAR
        Screened --> Held: compose hold on the tasks a flagged line touches
        Held --> Replanned: compose change start ... done
        Replanned --> [*]: tasks released, run continues
        Replanned --> Reverted: compose change undo C2
        Reverted --> [*]
    }
    Running --> ChangingCourse
    Verifying --> ChangingCourse
    ChangingCourse --> Running
```

Nothing is pushed until `gh stack merge`, and the CLI never marks the feature Done on its own: that needs the project branch merged.

## A task

Each task is one stack layer, built by one implementer in its own clone.
Its state lives in `results/<id>.json` and is what `compose status` shows.

```mermaid
stateDiagram-v2
    [*] --> pending: compose sync
    pending --> held: compose hold, or a blocker outside the feature is not finished
    held --> pending: released by compose hold, or the blocker is Done, Canceled, or its code is on trunk
    pending --> merged: Linear says Done before the run (built elsewhere)
    pending --> running: compose run dispatches it (In Progress on Linear)

    state running {
        [*] --> RedCheck: test runs on the base before any change
        RedCheck --> implementing: RED
        implementing --> Testing: one implementer turn, edits only
        Testing --> implementing: test RED, fix brief from the test reader, attempts left
        Testing --> [*]: GREEN and within limits
    }

    running --> broken_base: the test fails on the base itself
    running --> already_green: the test passes before any change
    running --> unchained: the chain it stacks on no longer rebases
    running --> paused: compose hold mid-turn, work kept on a ref
    paused --> pending: the next pass reads the hold
    running --> interrupted: the run died or Ctrl-C
    interrupted --> running: compose resume
    running --> payment_required: a provider ran out of credit
    payment_required --> running: compose resume
    running --> passed: GREEN, formatted, committed on the layer branch
    running --> unproven: GREEN on a FAST route, but its regression test is not proof yet
    running --> contradiction: the implementer reports the brief contradicts the code
    running --> failed: attempts spent, test still RED
    running --> rejected_size: GREEN but over the line limit
    running --> rejected_docs: GREEN but new documents written
    running --> rejected_scope: GREEN but files outside the task touched
    running --> rejected_regression: GREEN but another test broke, or the base cannot be known
    running --> rejected_tampered: a protected file changed

    pending --> hand_done: compose done, built by hand with a reason
    passed --> merged: its branch is in trunk, or gh stack merge landed it
    hand_done --> merged: the same
    unproven --> merged: the same

    failed --> pending: re-split by the planner, re-run
    contradiction --> pending: the same, after a reflect
    rejected_size --> pending: the same
    rejected_docs --> pending: the same
    rejected_scope --> pending: the same
    rejected_regression --> pending: the same
    rejected_tampered --> pending: the same

    merged --> [*]
    already_green --> [*]
    broken_base --> [*]: an environment problem, fix the base
    unchained --> [*]: fix the chain, run again
```

The final states the CLI writes are `passed`, `already_green`, `failed`, `contradiction`, `unproven`, `broken_base`, the five `rejected_*`, `payment_required`, `interrupted`, `hand_done`, `unchained` and `paused`.
`merged` and `held` are derived by `status` from the branch, trunk and `hold.json`, never written to a result.
A design task has no branch: In Review or Done on Linear counts as passed, and it is never merged.

## The Linear issue

The CLI writes to Linear only to keep states true, and each move goes through one state table.
A story takes the state of its tasks, and the feature the state of its stories.

```mermaid
stateDiagram-v2
    direction LR
    state "Todo / Backlog" as Todo
    [*] --> Todo: the planner creates the task issue
    Todo --> InProgress: dispatched
    InProgress --> InProgress: failed, the failure as a comment
    InProgress --> InReview: passed or hand_done, with a comment
    InReview --> Done: compose clean proves the layer merged
    Todo --> Canceled: a reconcile, with the reason as a comment
    InProgress --> Canceled: the same
    Done --> [*]
    Canceled --> [*]
```

The roll-up rules, per story and then per feature:

- Done when every child that is not Canceled is Done.
- In Review when every such child is In Review or Done, but the feature reaches In Review only once verify passed.
- In Progress once any child started.
- Otherwise left as it is.

A blocker frees what it blocks when it is Done or Canceled, when it is In Review carrying the Design label, or when it is In Review under the same parent story.
