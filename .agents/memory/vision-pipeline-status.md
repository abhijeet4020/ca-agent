---
name: vision-pipeline-status
description: "The vision route works standalone AND is now wired into the batch pipeline; diagnosis and implementation record"
metadata:
  node_type: memory
  type: project
---

Diagnosis on 2026-09-11 while investigating "vision pipeline is not working" with LM Studio up.

**Diagnosis: the vision *client* worked; the vision *pipeline* was not wired into batch runs.**

- LM Studio at `http://localhost:1234/v1` serves `google/gemma-3-4b`. `.env` loads correctly.
- `ca-agent vision-extract` already worked end to end (verified live on a JPEG and a PDF page).
- `VisionClient` was instantiated in exactly one place: `cli/__init__.py` (`vision-extract`).
  `pipeline/` never imported `ca_agent.vision`.
- `pipeline/executor.py::_vision_image()` returned `PARTIAL` with "the paid pass has not run" and
  zero outputs even when vision was enabled; `_pdf()` classified pages and recorded
  `pages_needing_vision` but never rasterised or sent them. So `run` produced no vision output
  however healthy the server was. This was the "build the vision pass" gap in README "Known gaps".

**Implemented (ADR-017), user approved "Full batch vision pass":**

- `executor._vision_image()` now prepares the image, calls the client, renders the combined
  Markdown to `vision/document.md`, and returns SUCCESS / FAILED.
- `executor._pdf_vision()` handles scanned/mixed PDFs: one combined Markdown in page order,
  native text for TEXT pages, vision for SCANNED, a MIXED page's native layer only below
  `pdf.native_text_duplicate_jaccard`. Failed page -> marked + document `partial`. No chunks and
  no Parquet for a vision PDF (req 3).
- The route is re-decided after classification: `ExecutionResult.effective_route` carries
  `PDF_VISION`, the orchestrator records it and looks up the prior outcome under both route keys
  (`_PROVISIONAL_ALTERNATIVES`), so a scanned PDF reuses instead of being re-charged.
- `vision/contract.py` gained `VisionPage`, `VisionDocument`, `render_vision_document`.
- `executor._vision_transport(settings)` is the single injection seam for tests (ADR-011).
- **Bound rasterised PDF pages with `prepare_image(..., max_edge_pixels=vision.max_image_edge_pixels)`.**
  A 250 dpi US Letter page rasterises to ~2125x2750 / 2 MB; LM Studio answered those with HTTP 502
  and read errors. Bounded to 1413x2000 / ~1.3 MB they extract cleanly. This was the real reason
  the PDF vision pass failed live.

**Verified:** 462 tests pass, ruff clean; live run over a real image slice (2 images ->
`vision_image` + `vision/document.md`) and a real 3-page scanned PDF (`PDF_VISION`, one combined
Markdown, 3 page headings, success).

**Still outstanding:** the plan's cross-file thread pool + token-bucket rate limiter. Page calls
are currently sequential; `vision.max_concurrency` is not yet used. Do that as its own change.

## Cost of a full run (measured 2026-09-12, before launching one)

- **~78 seconds per vision call** with `google/gemma-3-4b` on this machine. It does **not**
  improve with a smaller image: 2000px = 76.6s, 1200px = 83.3s, 1000px = 78.0s. The cost is
  output generation (a full JSON extraction, `max_output_tokens=4096`), not image tokens - so
  lowering `max_image_edge_pixels` buys nothing.
- Pending work from the README's own figures: ~2,280 images + 3,584 scanned PDF pages ~=
  **5,864 calls x 78s ~= 5.3 days** of continuous vision processing. PDF classification adds
  ~80 min (pdfplumber at ~1.4 files/s over 6,685 PDFs).
- **Run it category by category.** Manifests seal only at the very end of a run, so a Ctrl-C
  discards the whole run's reuse state and a rerun redoes everything. One `--category` per run
  seals that category's manifests, which is what makes a multi-day job resumable.
- **Do not write the output inside OneDrive.** `data/` defaults to the project folder, which is
  `C:\Users\solor\OneDrive\Desktop\...` - OneDrive-synced, and C: has only ~27 GB free against
  ~7 GB of output. Use `CAAGENT__PATHS__OUTPUT_ROOT=D:\ca-agent-data`; D: has ~631 GB and is not
  synced.
- The `data/silver` present on 2026-09-12 (986 MB, 5,382 `record.json`, **1** sealed manifest) is
  an *interrupted* run. Unsealed work is invisible to reuse, so it is dead weight and will be
  reprocessed. The earlier unexplained progressive deletion of `data/silver` still has no cause;
  the Defender scan recommended in `session-handoff.md` has not been confirmed as done.

