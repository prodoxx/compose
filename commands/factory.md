---
description: Start the factory - it builds ready stories in delivery plan order at a day pace and offers what to plan next while it builds
argument-hint: "[stop [--now] [--hold <why>] | status | pace <n> | night [--until HH:MM] [--budget <dollars>] [--stop-when <words>] | day | digest]"
allowed-tools: Bash(compose:*), Bash(herdr:*)
---
Invoke the compose skill, resolve the repo by its steps 0 and 0j, and follow sections/0f.md.
When `$ARGUMENTS` is `stop`, `status`, or `pace <n>`, run `compose factory $ARGUMENTS` and show its output as is.
When `$ARGUMENTS` is `stop --hold <why>`, run `compose factory stop --hold "<why>"` with the why quoted as one argument, and show its output as is.
When `$ARGUMENTS` is `stop --now`, run `compose factory stop --now` and show its output as is.
When `$ARGUMENTS` starts with `night`, `day`, or `digest`, run `compose factory $ARGUMENTS` with any `--stop-when "<words>"` quoted as one argument, show its output as is, and follow sections/0n.md.
With no arguments, do not ask which project as 0j does for a bare project link: the factory works on the repo's Linear project from step 0, so run `compose factory start` and follow sections/0f.md.
In this session, never write code, stories, or tasks yourself.
