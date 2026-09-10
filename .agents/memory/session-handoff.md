# Session Handoff — SPEC-01 Phase 1 (Silver layer)

**Updated:** 2026-09-10
**Branch:** `master` at `0ca9681`, pushed
**Read first:** `.agents/plans/PHASE-01-silver-layer-plan.md`, then `docs/design/ADR.md` and
`docs/design/sourcemap.md`

This is the project memory folder named in `.agents/workingrules.md` line 14. It is written for
any coding agent picking this work up, not just the one that paused it. Sibling notes in this
folder: [project-overview](project-overview.md), [spec-01-data-pipeline](spec-01-data-pipeline.md),
[working-rules](working-rules.md), [feedback-long-running-scripts](feedback-long-running-scripts.md).

**All plans and agent-facing notes live under `.agents/` — plans in `.agents/plans/`, memory in
`.agents/memory/`.** Nothing goes to an external tool-specific memory directory.

---

## Where the work stopped

Phase 1 implements the **Silver layer** of SPEC-01. Steps 0–6 of 13 are done and corpus-validated.

| Step | Scope | State |
|---|---|---|
| 0 | Bootstrap: uv packaging, environment templates, pytest harness, TP-01 test plan | Validated |
| 1 | Core domain types, layered config, per-section reuse fingerprints | Validated |
| 2 | Atomic storage, run-ordinal versioning, sealed manifests, reuse decision | Validated |
| 3 | Discovery, scope resolution, streaming SHA-256, scope-isolated dedup | Validated |
| 4 | Signature-first format detection and the routing table | Validated |
| 5 | Archive expansion: depth-3 recursion, bomb guards, path sanitation | Validated |
| 6 | Tabular → Parquet with round-trip typing | Validated |
| 7 | Text extraction and chunking | **In progress** |
| 8 | JSON/XML structural routing | Not started |
| 9 | PDF text-vs-scanned classification | Not started |
| 10 | Vision extraction route | Not started |
| 11 | Per-file format documents | Not started |
| 12 | Pipeline orchestration and full CLI | Not started |
| 13 | Integration tests and the full corpus run | Not started |

"Validated" means the step was run over all 16,596 real corpus files, not just unit-tested.

---

## Step 6 result (2026-09-10)

16,596 files scanned → 3,001 routed tabular → **2,970 converted, 27 explicit `CORRUPT_FILE`
records, 0 unhandled exceptions**. 13,694 Parquet files from 14,830 tables (1,136 empty, recorded
not dropped). 254,593 typed columns vs 1,365,629 kept as text — the round-trip typing rule is
deliberately conservative, which is what protects PAN/GSTIN/account identifiers.

Three reader bugs the corpus found, all fixed (ADR-013):

- **`openpyxl.load_workbook` validates the filename extension before reading content**, so genuine
  OOXML workbooks saved under a stale `.xls`/`.xlk` name were all rejected with a misleading "old
  .xls format" error. This reintroduced the very extension dependency signature-first detection
  exists to remove. Fixed by passing `BytesIO` instead of the path.
- **`lxml.etree.XMLSyntaxError`** (undefined `x15` namespace prefix in GST-portal exports) is a
  `SyntaxError`, not a `ValueError`, so it escaped the reader's exception map.
- **`xlrd.compdoc.CompDocError`** is a plain `Exception` subclass, distinct from the `XLRDError`
  and `AssertionError` cases already handled.

The 27 remaining failures are genuinely malformed source files, not code gaps: 25 legacy `.xls`
with corrupt defined-name formulas (nearly all from one client, "Shahane & Company") and 2 `.xlsb`
with no valid workbook part. An explicit record is the correct outcome under req 5. Those 25 could
likely be recovered by re-saving them from Excel, if that client's registers matter analytically.

`TabularSettings.audit_uncached_formulas` (default `True`) now gates the formula audit's second
full workbook read; disable it for large batch runs.

---

## Resume here

**Step 7 — text extraction and chunking.** Document readers (`python-docx`, `striprtf`,
`lxml.html`, stdlib `email`, txt/log via `charset-normalizer`) plus chunk records carrying full
lineage, split with `langchain-text-splitters`. Embeddings are Gold-layer work and are explicitly
*not* in Phase 1; step 7 stops at chunk records.

Corpus counts for this route, measured during the step-6 run: 353 `word_ooxml`, 339 `plain_text`,
75 `html`, 50 `word_ole` (needs LibreOffice — see open items), 10 `rtf`.

Use the scratchpad for validation output and delete it afterwards. A full archive expansion is
about 2.3 GiB and a full tabular run writes several hundred MB.

---

## How to work on this project

Every step follows the same loop, and the loop is what has been finding the real defects:

> write tests first → implement → **run it over the whole real corpus** → fix what the corpus
> breaks → add a regression test → update `docs/design/sourcemap.md` and `docs/design/ADR.md`

Running against all 16,596 files at each step has repeatedly caught things no synthetic fixture
would have:

- **261 files named `*.pdf` are Java serialized object streams** (magic `AC ED 00 05`) written by
  the Income Tax e-filing utility. Recorded as `JAVA_SERIALIZED` and **never deserialised**.
- **OLE directory sectors are not at a fixed offset.** A prefix-scan implementation misclassified
  **175 genuine `.xls` and `.doc` files** as unknown until `olefile` was used to parse properly.
- **A zip member name contains embedded newlines**, which Windows cannot write — and the cleanup
  path then crashed on the same unexpressible name.
- **`xlrd` raises a bare `AssertionError`** on malformed defined-name formulas.
- **`csv.Error`** on embedded newlines in unquoted fields.
- **`openpyxl` rejects a valid workbook on its filename alone**, silently defeating signature-first
  detection for every misnamed spreadsheet in the corpus.

Do not mark a step done until it has run over the real corpus. Corpus validation scripts go in the
scratchpad and **must print live progress** — see
[feedback-long-running-scripts](feedback-long-running-scripts.md); the user runs them in their own
terminal and a silent run looks hung and gets killed.

---

## Process rules that bind this work

From `.agents/workingrules.md` — these are hard constraints, not suggestions:

- Produce a plan and get explicit confirmation **before** changing anything.
- Write tests before code; all tests must pass afterwards.
- **Do not modify `docs/` unless explicitly told.** The user has authorised exactly three:
  `docs/design/sourcemap.md`, `docs/design/packagedesign.md`, `docs/design/ADR.md`.
  `AGENTS.md`, `docs/implementation_plan.md`, `docs/specifications/specindex.md` and
  `docs/devenv.md` were **not** authorised.
- Every new source file needs a 4–5 line `Purpose:` comment and a `sourcemap.md` entry. Both are
  enforced by `tests/unit/test_architecture.py`.
- Chain `environment.bat` / `environment.sh` before any command.
- Apply the Thinking Craftsman guidelines: catch specific exception types (never bare `Exception`),
  never swallow an exception, keep configuration separate from runtime logic, minimise and justify
  every dependency.

`tests/unit/test_architecture.py` also enforces the ARCHITECTURE.md layering rules by parsing the
real import graph — same-layer imports, higher-layer imports and cycles all fail the build.

---

## Decisions already made — do not relitigate

Full text in `docs/design/ADR.md` (ADR-001 … ADR-013). The load-bearing ones:

- **Bronze is `raw_data/` in place**, read-only. No code path writes there.
- **Phase 1 = Silver.** Gold (embeddings + FAISS) is a later phase.
- **No mutable "latest" pointer.** Active version = highest *sealed* manifest ordinal.
- **Reuse fingerprints are per config section**, so one changed setting cannot invalidate
  everything.
- **Format is decided by content, never extension** — 195 corpus files contradict their own.
- **pandas is excluded from the tabular read path** because it coerces `"0012345"` to `12345`.
- **PyMuPDF is rejected** as AGPL-3.0; use pypdf + pdfplumber + pypdfium2.
- **Nested archives recurse to depth 3** under size, member-count and ratio caps.
- **Owner-password-only PDFs are opened with an empty password**, not reported as locked.
- Package is `src/ca_agent/` (importable) though `AGENTS.md` says `src/ca-agent/`; recorded as a
  deliberate deviation in ADR-001 because an `AGENTS.md` edit was not authorised.

---

## Open items awaiting the user

1. **External tools.** 50 legacy `.doc` and 24 `.rar` files route to `NO_COMPATIBLE_READER` unless
   `CAAGENT__EXTERNAL_TOOLS__SOFFICE_PATH` and `UNRAR_PATH` are set in `.env`. The 50 `.doc` are
   relevant to step 7 specifically.
2. **Vision spend, before step 10.** The corpus holds roughly **3,900 images**, not the 2,298 the
   file census suggested — archives hide another 1,617 images and 937 PDFs. The user chose to run
   vision fully in Phase 1; the number is worth re-confirming before it costs money.
3. **The 261 Java-serialized files** contain real ITR data but cannot be read safely. Re-exporting
   them from the e-filing utility is the only route if they matter.

## Vision route reference material (for steps 9–10)

`sample_vision_script.py` in the repo root (untracked) is the **user's own reference
implementation**: LM Studio / OpenAI-compatible client, base64 image payloads, per-page PDF
rasterization, a Markdown-contract prompt. The user asked that it inform the vision pipeline and
said changing its logic is allowed.

**Conflict flagged and accepted:** it uses PyMuPDF (`fitz`), rejected as AGPL-3.0 by ADR-010, and
the `openai` SDK, rejected by ADR-011. The real implementation follows the ADRs (`pypdfium2` +
`httpx`) and treats the script as a design reference only.

---

## Corpus facts worth keeping

- 16,596 files, 3,305 directories, 94 client scopes across 14 populated categories.
- Client scope is `(category, client)` — several clients appear in two categories.
- `Mauli Hospital Tally Back up/` has no client level; the category directory *is* the scope.
- Content hashing: 15,265 distinct items, 1,331 within-scope duplicate paths, 10.4 GiB.
- Detection: zero unroutable files; unknown formats down from 451 to 14.
- Archives: 684 expand to 4,201 members, 2.3 GiB, nesting confirmed at depth 2, 129 locked members.
