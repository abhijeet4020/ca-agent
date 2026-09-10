---
name: spec-01-data-pipeline
description: "Key rules from the data pipeline spec governing raw_data processing (medallion architecture, immutability, dedup)"
metadata: 
  node_type: memory
  type: project
  originSessionId: c7eb5808-feab-4407-8707-b6fc973cb502
  modified: 2026-09-09T12:53:57.540Z
---

The data pipeline spec ([docs/specifications/SPEC-01-data_pipeline.md](../../docs/specifications/SPEC-01-data_pipeline.md)) governs how `raw_data/` (real CA client files) gets processed into bronze/silver/gold layers for agents to read. Part of [[project-overview]].

Key rules an implementation must respect:
- **Immutability**: never modify/overwrite/delete source files or previously generated outputs; reruns create new versioned outputs, never in-place edits.
- **Tabular → Parquet** (CSV/TSV/XLS/XLSX/XLSM/XLSB), one Parquet per worksheet/table; preserve types (incl. leading zeros), document conversion errors, no silent row drops.
- **Text → embeddings/FAISS**: per category/client FAISS index (never shared across clients, even same-named clients in different categories — dedup/index scope is always per-category/client folder). Scanned/mixed PDFs go a different route: combined per-PDF Markdown (page order, native text + vision-extracted content), no Parquet or embeddings for those.
- **Images**: separate script, calls an OpenAI-compatible vision API (configurable endpoint/model/creds), outputs structured Markdown.
- **Every file gets a per-file Markdown format-doc** (not a generic per-extension doc) — locked/unsupported/failed files get explicit status records (`PASSWORD_PROTECTED_FILE`, `NO_COMPATIBLE_READER`, etc.), never silently skipped.
- **ZIP handling**: extract without touching the archive; SHA-256 dedup within a client scope only; same-name-different-content both processed; locked members recorded and skipped.

**Why:** Spec was written through an interview process with an "Open Decision" left unresolved — recursive nested-archive extraction depth/size limits were raised but not confirmed, so no policy exists yet for that case.

**How to apply:** When SPEC-02 (agent dev) and SPEC-03 (frontend) eventually get written, they'll consume the gold-layer outputs this spec defines. Flag to the user if asked to implement pipeline code that nested-archive policy is still an open decision needing their input.
