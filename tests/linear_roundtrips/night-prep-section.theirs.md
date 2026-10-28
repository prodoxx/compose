## NEB-2191: See the agent steps

Layer: story. Prepared 2026-10-29 02:52 by night prep (claude-opus-5-5): a likely opening, not a script.

Code involved:

* app/src/routes/index.tsx
* app/src/routes/\__root.tsx
* app/src/routes/\_authed.tsx
* app/src/routes/\_authed/subscribe.tsx
* app/src/client/blueprints/home/Home.tsx

What is built:

* app/ runs on TanStack Start (@tanstack/react-start) with Better Auth (NEB-2181). Routes: / (Home), /signup and /subscribe behind \_authed, plus \__root.
* The picker takes images only (accept="image/\*") & several at once; see [https://www.figma.com/design/abc_def?node-id=133-730](<https://www.figma.com/design/abc_def?node-id=133-730>) and <Outlet /> in `_authed.tsx`.
* Use `shadcn add trace` (with its collapsible); cost is $5 \* 2 and a_b is fine.

What is still open:

* Nothing in app/src is a collapsible.

Questions:

1. Should Fix look as Figma 126:323 draws it (an enabled link in primary #7A1F2B), or disabled?
   Recommendation (a guess): Guess: A, as drawn. Task 027 (NEB-2304) styles it once. - B needs a disabled look.
2. How long does 'Thinking...' run? \[x\] is not a task.
   Recommendation (a guess): about 2s - a fixed wait.
