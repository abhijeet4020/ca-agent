---
name: project-overview
description: "What ca-agent is, its current state, and where the real domain data lives"
metadata: 
  node_type: memory
  type: project
  originSessionId: c7eb5808-feab-4407-8707-b6fc973cb502
  modified: 2026-09-09T12:53:47.698Z
---

ca-agent is a "Financial Agentic application" for chartered accountants — helps with financial analysis, report generation, and data gathering. Scaffolded from Nitin Bhide's "Thinking Craftsman" agentic-engineering cookiecutter template (see [README.md](../../README.md), [[thinking-craftsman-workflow]]).

**Why:** The user (a CA / CA firm, per the tone of raw_data client files) is building an AI pipeline over ~20+ years of real client tax/audit/GST records to eventually power agentic analysis and report generation.

**Current state (as of 2026-09-09, single commit "Initial project setup"):**
- `src/` and `tests/` are empty (`.gitkeep` only) — no application code written yet.
- `docs/specifications/SPEC-01-data_pipeline.md` is fully written and detailed (medallion bronze/silver/gold pipeline spec — see [[spec-01-data-pipeline]]).
- `SPEC-02-agent_development.md` and `SPEC-03-frontend_development.md` are **empty stubs** — not yet authored.
- `docs/implementation_plan.md` has no phases defined yet (template placeholder only).
- `docs/design/sourcemap.md` and `docs/design/ADR.md`'s `packagedesign.md` are empty — no code/packages exist yet to document.
- `docs/raw_data_docs/index_sourcemap.md` + 94 per-client sourcemap docs already generated: an inventory (names/extensions only, not content) of `raw_data/` — 16,596 files, 3,305 subdirs, 15 top-level category directories (Business Clients, GST Company/Cooperative/Partnership/Proprietor/TDS-Govt, Income Tax Audit Clients, LLP, Cooperative Audits, Mauli Hospital Tally Backup, Partnership Firms, Private Limited Company, Salary Clients, Section 8 Company).

**How to apply:** Before proposing implementation work, check whether SPEC-02/SPEC-03 exist yet and whether the user has defined implementation_plan.md phases — per [[working-rules]], phase N+1 must not start until phase N is done and the user explicitly asks. Don't re-derive the raw_data inventory by walking the filesystem — read `docs/raw_data_docs/index_sourcemap.md` first, then only the specific client sourcemap needed (there's an explicit rule against reading all 94 at once).
