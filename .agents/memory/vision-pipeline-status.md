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

## Serving gemma-3-4b on a 4 GB GPU (guidance given 2026-09-12, UPDATED for Gemma 4 E2B)

**Correction:** the model on the server laptop is **Gemma 4 E2B** (not Gemma 3 4B). Q4_K_M file
= 3.46 GB. The architecture is very different and far more efficient.

**Gemma 4 E2B architecture (from config.json):**
- 35 text layers, 8 attention heads, **1 KV head** (extreme GQA)
- head_dim=256, hidden_size=1536
- sliding_window=512 (local layers only cache 512 tokens)
- layer_types: 7 global (full attention) + 28 local (sliding window)
- num_kv_shared_layers=20 (20 layers share KV with previous layer → only 15 effective)
- 128K max context but KV cache is tiny

**KV cache per token (fp16, no quantization):**
- Per layer per token: 1 × 256 × 2 (K+V) × 2 bytes = 1024 bytes
- Effective layers: ~15 (35 - 20 shared)
- At C=4000: ~18 MB total KV cache
- At C=8192: ~31 MB total KV cache
- **Going from 4000 → 8192 costs only ~13 MB — it's free**

**VRAM breakdown (Q4_K_M, full GPU offload):**
- Model weights: ~3.46 GB (the GGUF file)
- mmproj (vision tower ~150M): ~300 MB
- KV cache at 8192: ~31 MB
- Compute buffers (flash attention, etc.): ~200 MB
- **Total: ~3.99 GB — just fits 4 GB**

**Recommended: context window = 8192**
Pipeline needs: prompt_tokens (~1272 for one PDF page) + max_output_tokens (4096) = 5368.
Context 4000 is too small (5368 > 4000 → HTTP 400 on large pages).
8192 gives 5368 + ~2824 headroom, and the KV cost is only 31 MB.

**Critical: the user's 2.1 GB VRAM at context 4000 means partial GPU offload, not full.**
E2B Q4_K_M should fit entirely on a 4 GB GPU at ~4.0 GB. The user needs to set
`-ngl 99` (or `-ngl 999`) to offload all layers. At 2.1 GB, roughly 60% of layers are on
GPU — that's why PDF pages are slow (CPU fallback for vision processing).

## Truncated-JSON retry fix (2026-09-12)

**Symptom:** `pune plot .pdf` (Marathi land record, 2 scanned pages) returned "error for page 1"
through the pipeline even though `vision-extract` on the same model/server worked fine.

**Root cause:** Gemma 3 at `temperature=0` is non-deterministic (batching/memory alignment).
On dense Marathi text it sometimes enters a **repetition loop** — e.g. repeating `[भू-जिल्हा]`
hundreds of times in `visible_text` — until it hits `max_tokens=4096`, producing truncated
JSON with `finish_reason=length`. Measured: 2/5 attempts succeed (`finish_reason=stop`, ~1946
tokens), 3/5 fail with `finish_reason=length` and 4096 completion tokens.

The pipeline's `VisionClient._parse()` returned a non-retryable failure on `ContractError`
immediately — it never retried a 200-OK response with a broken body. So the first truncated
attempt was a permanent failure for that page.

**Fix:** `VisionClient._parse()` now returns `(result, finish_reason)`. `extract()` retries
when `finish_reason == "length"` (token-limit truncation) because a fresh attempt has a real
chance of avoiding the repetition loop. Other parse errors (`finish_reason=stop` with bad
JSON) are still non-retryable — that is the model's own fault and retrying wastes money.

Two new tests: `test_truncated_json_from_token_limit_is_retried` and
`test_a_parse_error_without_length_is_not_retried`. All 464 tests pass, ruff clean.

**`.env` updated:** `MAX_ATTEMPTS=3` (was 5 default), `REQUEST_TIMEOUT_SECONDS=180` (was 120).
4096 max_output_tokens stays — successful extractions use ~1946 tokens so 4096 is the right
ceiling; the issue was not the ceiling but the lack of retry.

**Live-verified:** `pune plot .pdf` now returns `status=success` with both pages extracted and
no limitations. The retry consumed one extra ~100s call on page 1 (first attempt truncated,
second succeeded) — acceptable for a local server where the alternative was total failure.


