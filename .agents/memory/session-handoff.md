# Session Handoff — SPEC-01 Phase 1 (Silver layer)

**Updated:** 2026-09-10
**Branch:** `master`, pushed
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
| 7 | Text extraction and chunking | Validated |
| 8 | JSON/XML structural routing | Validated |
| 9 | PDF text-vs-scanned classification | Not started |
| 10 | Vision extraction route | Not started |
| 11 | Per-file format documents | Not started |
| 12 | Pipeline orchestration and full CLI | Not started |
| 13 | Integration tests and the full corpus run | Not started |
| 14 | Dedicated Tally XML voucher route (ADR-015, added 2026-09-10) | Not started |

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

## Step 7 result (2026-09-10)

16,596 files scanned, 62 excluded as an application bundle, 760 routed to text →
**760 extracted, 0 failures, 0 unhandled exceptions, 0 offset mismatches**, in 21 seconds.
2,089 units and 4.6M characters became 7,026 chunks; every chunk's character offsets were
verified against the real source text, not just in unit tests. 820 tables detected, 820
extracted. 15 documents contain no text at all and are recorded as such. Readers used:
python-docx 353, charset-normalizer 327, lxml.html 68, striprtf 10, lxml-pptx 2.

Design points worth not relitigating:

- `TextUnit` and `UnitType` live in `core/` because ARCHITECTURE.md puts `readers` and
  `chunking` in the same layer, so neither can import the other. That contract also lets step
  9's PDF pages and step 8's field paths feed the same chunker.
- docx body order is recovered by walking the XML body: python-docx exposes `paragraphs` and
  `tables` as separate sequences, and reading them in turn moves every table to the end.
- pptx slide text is parsed straight from the package XML. The corpus has two presentations,
  which does not justify adding python-pptx.
- Chunk offsets are resolved by locating each chunk back in its own unit, not accumulated, so
  the lineage claim is verified; an unlocatable chunk raises `ChunkingError` rather than
  recording a false offset.
- Chunk ids derive from scope, content hash, unit sequence, seq and the config fingerprint -
  not a counter - so reruns reproduce them (req 8) while identical bytes in two scopes stay
  distinct (req 7).
- Legacy `.doc` (50 files) still routes to `NO_COMPATIBLE_READER` pending the LibreOffice
  decision in open items.

**The bug the corpus run found:** `unit_ref` is a human citation, not an identifier, and repeats
heavily in real documents — one bank statement extracts to 784 units carrying only 167 distinct
refs, with the heading "Receipt" appearing 250 times. A chunk naming only a ref plus an offset
could not be resolved to one place, defeating the lineage guarantee. `ChunkRecord` now carries
`unit_sequence`, and the chunk id derives from it rather than from the ref. 510 offset
mismatches across 3 documents went to zero.

---

## Step 8 result (2026-09-10)

397 structured documents → **395 extracted, 2 genuine failures, 0 unhandled exceptions**.
12,548 Parquet tables over 54,436 rows; 4,280 field-path units; **130 zero-padded identifiers
preserved as strings and zero typed wrongly**, which is the round-trip rule holding on real ITR
data. The 2 failures are a `.log` file misdetected as JSON and one genuinely truncated ITR JSON.

Records reach Parquet through `write_records_as_parquet`, a public entry point added to the
tabular reader, rather than a second typing implementation that could drift. XML is parsed with
entity resolution, DTD loading and network access disabled - a client file is untrusted input.

Two fixes the corpus run forced:

- **Units were grouped only by top-level field path.** A Tally register has one root child
  holding the whole document, so one export produced a single `TextUnit` of **56.6 million
  characters**. Units are now bounded by `StructuredSettings.max_unit_characters` and carry a
  part suffix so each stays individually citable.
- **Tally writes raw control characters such as `&#4;`** into its exports, which no conforming
  parser accepts. A recorded recovery fallback (`recover_malformed_xml`) now salvages seven
  files of real ledger and item master data; failures fell from 9 to 2. A recovered parse always
  carries a warning so it is never mistaken for a clean one.

## Resume here

**Step 9 — PDF text-versus-scanned classification** (SPEC-01 req 2 vs req 3). The design is in
the approved plan: per page, `TEXT` if >= 120 non-whitespace chars, `SCANNED` if < 120 chars and
image coverage >= 50%, `EMPTY` if < 20 chars and coverage < 5%, else `MIXED` treated as scanned.
All pages `TEXT` -> `PDF_TEXT`; any `SCANNED`/`MIXED` -> `PDF_VISION`. `PdfSettings` already
holds every threshold. Use `pypdf` for structure and encryption, `pdfplumber` for text with
layout, `pypdfium2` for rasterisation - **never PyMuPDF** (ADR-010, AGPL). PDF pages become
`UnitType.PAGE` units and feed the step-7 chunker unchanged.

This is the corpus's biggest route by far: **6,685 PDFs**, plus the owner-password-only case in
ADR-008 that governs hundreds of ITR-V and TIS filings. Step 9 also decides how much step 10
costs, so run `discover` and report the scanned-page count before any paid vision call.

Step 14 (dedicated Tally route, ADR-015) is scheduled after the existing Phase 1 steps.

## ADR-014 came out of the step-7 smoke run

19 percent of all extractable text turned out not to be client data:
`Business Clients/SANDEEP KOTHAWALE/AY 2019-20/REVISED/ITR` is a whole unpacked copy of the
e-filing utility (jQuery, keystore, its own HTML, plus `ISIN_LIST.properties` at 3.3 MB and
`IFSC.txt` at 1.7 MB). `catalog/bundles.py` now excludes it as `NON_DATA` - detected by a Java
keystore plus a `.properties` file in the same subtree, never by folder name, because clients
genuinely have `ITR` folders and `.cer`/`.pfx` files. Exactly one directory corpus-wide, 62
files, 15.8 MB, no false positives. The user chose this over narrower alternatives.

Use the scratchpad for validation output and delete it afterwards. A full archive expansion is
about 2.3 GiB and a full tabular run writes several hundred MB; the text run writes nothing.

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
- **A document heading repeats 250 times**, so a chunk citing only its heading and an offset
  named no single place until `unit_sequence` was added.
- **A client folder holds an entire unpacked desktop application**, 19 percent of all
  extractable text (ADR-014).
- **A single XML rendered to 56.6 million characters in one unit**, because grouping by
  top-level field path is no bound at all when the root has one child.
- **Tally embeds raw control characters** that no conforming XML parser will accept.

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
