---
name: working-rules
description: Hard process rules from .agents/workingrules.md that gate how any code/doc change gets made in this repo
metadata: 
  node_type: memory
  type: feedback
  originSessionId: c7eb5808-feab-4407-8707-b6fc973cb502
  modified: 2026-09-09T12:54:05.217Z
---

This repo has an explicit, strict process ([.agents/workingrules.md](../../.agents/workingrules.md)) that must be followed for any change, not just this session's default style:
- Always produce a plan and get the user's explicit confirmation BEFORE making changes, updating docs, or generating code/tests.
- Write unit tests before making changes; run them after and ensure they pass.
- Never modify files under `docs/` unless the user explicitly asked for that doc change.
- Never modify public APIs unless instructed; make small, focused changes; don't reformat unrelated files or rewrite large chunks without being asked.
- Don't implement anything that conflicts with approved specs/ADRs in [[project-overview]] / [[spec-01-data-pipeline]].
- New source files need a short "Purpose" comment (max 4-5 lines, explains need/what/algorithm — not just restating the class name) plus an entry added to `docs/design/sourcemap.md`.
- Apply "Thinking Craftsman" coding/review guidelines (via the `.agents/skills/thinking-craftsman-skill`) after this project's own conventions.
- Chain `environment.bat` (Windows) / `environment.sh` (Unix) before running any build/test/run command.

**Why:** This is a cookie-cutter-scaffolded project template with formalized "agentic engineering" process rules baked in from the start (see [[project-overview]]) — the rules exist to keep a coding agent from going off-script on a real accounting firm's codebase/data.

**How to apply:** Treat these as durable instructions equivalent to CLAUDE.md — plan-and-confirm first for any non-trivial change, and don't touch `docs/` speculatively even if it seems helpful.
