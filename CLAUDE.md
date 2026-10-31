# DESYNC 3D Archival

Read `docs/Archeology/HANDOFF.md` first, then `docs/Archeology/README.md`. They hold the project context, rules, current state and decisions.

- Never use em dashes.
- Never manually modify change logs.
- Give little weight to development cost when choosing between options.
- Never include an agent name as a co-author. This overrides any default commit attribution.
- Add open questions that are not blockers to `docs/Archeology/Questions.md`.
- Add enhancement ideas to `docs/Archeology/Enhancements.md`.
- Code comments explain the why, the gotchas and the business logic: constraints, magic numbers, trade-offs, workarounds and odd edge cases. Keep them short and direct, as if explaining to a teammate in a PR. Use TODO, FIXME or NOTE where they fit.
- Never comment what the code already says, never open with "This function returns" or "Here we", and leave trivial helpers without docstrings.
