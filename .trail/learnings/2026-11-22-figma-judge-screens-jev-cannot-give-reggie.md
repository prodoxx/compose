---
title: "Figma judge screens: Jev cannot give a reason, a vision call names the element; Jev rejects images outside 280-1568"
author: Reggie
date: 2026-11-22
branch: worktree-agent-af84f3bfe65ad6c54
cost: { minutes: 8, tokens: 94466 }
touches:
  - bin/fc_figmashots.py
  - bin/fc_vision.py
---

## Decided
- Jev (TypeSafe System One) has four question types: Choice, Score, Noul, Box. None returns free text or a reason (docs.typesafe.ai/primitives.md); Box returns only a bounding box, never a not-found. So a screens NOT_MET from Jev can never carry a reason, and no Jev question can fix #466.
- The reason comes from the fast model with image input: bin/fc_vision.look_json (OpenRouter chat with image_url data URLs; with a claude: TEXT_MODEL it runs claude_once with the Read tool in the screens folder). fc_models.fast_json is text only and fc_models.py sits at 598 of 600 lines, so the call lives in its own module (ranked after fc_models in tests/modules/rank.txt).
- It is asked only after Jev gave NOT_MET twice at the threshold, and a violation stands only when it answers violated=true with a non-empty element; no element leaves the tree verdict (UNCLEAR stays UNCLEAR). A vision call that fails blocks the screens as a judge error, never a pass.
- Jev rejects, never resizes, an image with a side under 280 or over 1568 (docs.typesafe.ai/images.md): a hand-added screen in screens.json went unchecked and 400-ed the whole request with no file name. fit_screens (judge_screens) reads the PNG/JPEG header with the stdlib, cuts such an image with jev's tiles tool as one tile into screens/fit/, leaves the file alone, and refuses a missing or non-image file by name.
## Watch out
- The OpenRouter image path was checked only against a stubbed http_json (no OpenRouter key on the dev machine); the claude: path was run live on a real PNG and read it correctly. Run one live figma judge on a screens violation to confirm the OpenRouter body is accepted.
- A TEXT_MODEL that takes no images fails the vision call with the provider's error, which blocks the judge.
