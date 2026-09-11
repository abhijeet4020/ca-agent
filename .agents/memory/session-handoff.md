# Session Handoff — SPEC-01 Phase 1 (Silver layer)

**Updated:** 2026-09-10
**Branch:** `master`, pushed. 419 tests pass, ruff clean.
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
| 9 | PDF text-vs-scanned classification | **Implemented; full corpus run outstanding** |
| 10 | Vision extraction route | Implemented; no live API call yet |
| 11 | Per-file format documents | Validated |
| 12 | Pipeline orchestration and full CLI | Implemented; smoke-run on 250 real files |
| 13 | Integration tests and the full corpus run | **Tests pass; the full run is outstanding** |
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

## Step 9 — implemented, full corpus run outstanding

`readers/pdf.py` classifies each page from measured non-whitespace characters and image
coverage, extracts native text for TEXT pages as `UnitType.PAGE` units, and answers
`requires_vision()` without making any call. All thresholds come from `PdfSettings`. Uses
`pypdf` for encryption, `pdfplumber` for text - **never PyMuPDF** (ADR-010, AGPL).

**A 700-file sample says the vision route is far cheaper than the plan assumed: 97.7% of pages
carry native text, and only 2.2% (57 of 2,548) need a vision call.** The corpus is mostly
digital filings, not scans. Confirm with the full run before step 10 spends anything.

Two things the sample established, both verified independently across all 6,685 PDFs:

- **200 PDFs are owner-restricted only** and open with an empty password. ADR-008 is doing real
  work; a naive implementation would have discarded 200 ITR-V and TIS filings.
- **352 PDFs are genuinely locked**, and 213 of those are AIS/TIS. That produced ADR-016.

Also worth measuring in the full run: a `MIXED` page with **no detectable image** still routes
to vision, which is the spec's literal fallback but buys a call with nothing to OCR. The sample
saw 6 of 57. If the full run shows that share is large, it is worth raising.

**Run it with:** `uv run python -u <scratchpad>/corpus_validate_pdf.py` (optional file-limit
argument). Expect roughly 1.5-2.5 hours - pdfplumber is the slowest thing in the pipeline at
about 1.4 PDFs/second. It writes no output files.

## Step 10 — implemented, never yet pointed at a real endpoint

Three modules in `vision/`, 24 unit tests, all against `httpx.MockTransport`.

- **`contract.py` is the important one.** The model is asked for a strict JSON schema, not
  prose, and the reply is validated here *whatever the endpoint promised* - not every
  OpenAI-compatible server honours `response_format`. That makes "did this extraction work" a
  checkable question. Validated output is then rendered to the Markdown req 3 stores, so JSON
  is the wire format and never the artifact. Two rules are load-bearing: an unreadable value
  keeps its `[UNREADABLE]` marker rather than becoming a plausible number, and a schema-valid
  but wholly empty reply is a **failure**, so a blank page and a failed read stay distinct.
- **`client.py`** retries only faults that can clear, never a rejected request (400/401/403/413
  are one attempt), honours `Retry-After`, bounds attempts, and jitters backoff. Takes an
  injected httpx client (ADR-011) so backoff is asserted from a recorded sleep sequence.
- **`preprocess.py`** bounds the longest edge, converts bmp/gif, rasterises with pypdfium2.

**`ca-agent vision-extract <file>`** does one image or PDF page per invocation and prints the
Markdown or the raw JSON (`--format json`). One file at a time on purpose, so a call's cost is
visible before the batch makes thousands. Verified end to end against a stub HTTP server:
correct schema request, `temperature=0`, bearer auth, image downscaled.

API details come from `.env` (`CAAGENT__VISION__BASE_URL`, `__MODEL`, `__API_KEY`), loaded by
`environment.bat` into the environment where `load_settings` picks them up.
`.env.example` documents both a hosted endpoint and a local LM Studio one.

**What has not happened: a single real API call.** Point `vision-extract` at one corpus scan
first and read the output before letting any batch loose.

## Steps 11-13 (2026-09-10)

**11 - format documents.** `docgen/` renders the req-4 companion document. docgen and readers
are the same layer, so the orchestrator translates whichever reader ran into a neutral
`FormatDocument`; that indirection is exactly what keeps readers free of document-writing code.
Absence is the explicit `unknown` string and empty sections are still rendered, because an
omitted section cannot be told from one nobody attempted.

**12 - orchestration.** `pipeline/executor.py` runs one file down its route;
`pipeline/orchestrator.py` is the run. The ordering *is* the immutability contract: one run
ordinal up front so every output directory is unique by construction, `record.json` written
last so a directory without one is an abandoned attempt, manifests sealed only at the end so a
crash leaves the previous run active. Archive members re-enter the same loop.

One design correction worth keeping: **every non-success status must carry an error explaining
it.** `ProcessingRecord` already enforced this and it was right to - a partial result that does
not say why is barely better than a silent skip. Three routes were putting the reason in a
warning instead.

**13 - integration tests.** 16 end-to-end tests over a miniature two-category corpus. The
load-bearing one hashes every file in the output tree before and after a rerun; Windows mtime
and ACLs are not a sound basis for a durability assertion.

**Verified on 250 real corpus files.** Every file accounted for, 5,264 chunks. A rerun reused
188 and reprocessed exactly the 56 locked-and-partial files the retry policy names - the reuse
contract behaving correctly on real data, not just fixtures.

## 2026-09-11: the Silver output was deleted by something unidentified

**The code is fine. The data is gone.** A full corpus run completed successfully (r000001:
16,534 discovered, 9,744 success, 36 failures, 490,338 chunks), Gold indexed it, and then
`data/silver` and `data/gold` were **progressively deleted while the build was running** -
measured dropping 7,418 -> 5,930 -> 4,025 -> 1,192 -> 0 records over a few minutes, ending with
the directory itself removed.

Ruled out: OneDrive (it syncs the C: user profile, not D:), disk space (215 GB free), the Gold
build (it was *failing* on missing files, not causing them - two scopes recorded
`[Errno 2] No such file or directory` reading chunk files mid-build), and the Streamlit process
(deletion continued after it was stopped).

**Not ruled out:** Windows Defender reports one threat detection on this machine - a fileless
PowerShell dropper fetching a remote script and piping it to `powershell -w hidden`. Defender
reports it blocked. There is **no evidence linking it to the deletion**, but it is the one
unexplained hostile artefact on the box and deserves a full scan before another 5 GB run.

`raw_data/` was never at risk and is intact - Bronze is opened read-only and no code path writes
there, which is ADR-003 earning its keep. Everything lost is regenerable by re-running.

**Before re-running:** consider a different `CAAGENT__PATHS__OUTPUT_ROOT`, and know that a
Ctrl-C discards a whole run because manifests seal only at the end. Incremental sealing is the
top robustness gap.

## Resume here

**The full corpus run is the remaining Phase 1 deliverable.**

    .\environment.bat
    uv run python -u src\main.py run

Expect several hours: PDFs dominate at roughly 1.4 files/second and there are 6,685 of them.
Output goes to `data/` by default (gitignored); set `CAAGENT__PATHS__OUTPUT_ROOT` to put it
elsewhere. 250 files produced 30 MB, so budget on the order of 2 GB for the corpus and check
free space first - a previous run silently hit `No space left on device` inside the reader.

The run is resumable by design: re-invoking it reuses everything already settled, so a
Ctrl-C costs only the file in flight.

Useful flags: `--limit N`, `--category "<name>"`, `--dry-run` (decide everything, write
nothing), `--force` (reprocess unchanged content).

Still outstanding beyond that: the **step-9 PDF corpus report** (vision budget), a **first real
vision call** before any batch, and **step 14** (dedicated Tally route, ADR-015).

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

`.agents/reference/sample_vision_script.py` is the **user's own reference implementation**: LM Studio / OpenAI-compatible client, base64 image payloads, per-page PDF
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
