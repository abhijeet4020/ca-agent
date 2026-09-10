# Phase 1 — Silver Layer Implementation (SPEC-01 Data Pipeline)

## Context

`d:\ca-agent` is a Financial Agentic application for chartered accountants. Its value depends entirely on agents being able to read ~20 years of real client records — but today that data is 16,596 loose files across 15 category folders, in formats ranging from Excel and ITR JSON to scanned PDFs and Tally binaries. Nothing is machine-readable, and `src/` is empty.

`docs/specifications/SPEC-01-data_pipeline.md` defines a medallion pipeline to fix that. This plan implements **Phase 1 = the Silver layer**: turning the untouched raw corpus into validated, versioned, fully-traceable derived artifacts that a later Gold phase can embed and index.

The spec's hardest requirement is not any single format — it is requirement 8. Nothing may ever be modified, overwritten, or deleted; every rerun must publish new immutable versions while preserving all history, and retrieval must resolve to the latest successful version for a given processing configuration. That contract shapes the whole design, so it gets built and tested *before* any reader.

## Decisions locked in this session

| Decision | Choice |
|---|---|
| Bronze | `raw_data/` exactly as-is. Never copied, never written to. The source tree *is* Bronze. |
| Phase 1 scope | **Silver** — archive extraction, Parquet, text extraction, chunking, vision Markdown, format docs, processing records, manifests, versioning, incremental rerun |
| Gold (later phase) | Embeddings + per-category/client FAISS. Chunking is Phase 1; embedding is not |
| Embeddings provider | Local sentence-transformers, when Gold arrives. Config slot reserved now, unused |
| Vision route | **In Phase 1, run fully** against a configurable OpenAI-compatible API |
| Nested archives | Recurse to **depth 3** + expanded-size cap + member cap; exceeding a cap emits an explicit record |
| Tally binaries | `NO_COMPATIBLE_READER` + format doc. No extraction attempt |
| Owner-password PDFs | Open with empty password and extract normally; only a genuine refusal is `PASSWORD_PROTECTED_FILE` |
| `docs/` edits allowed | **Only** `sourcemap.md`, `packagedesign.md` (new), `ADR.md` |

**One answer needs confirming.** On external tools you selected LibreOffice *and* unrar *and* "neither — record as unreadable", which conflict. I have read that as: **support both, config-gated and optional** — if `soffice` / `unrar` is present and enabled, use it; if absent, fall back to a `NO_COMPATIBLE_READER` record. That satisfies all three selections and keeps `uv sync` alone sufficient to run. Say so if you meant something else.

## Findings from probing the real data

The design agents read actual bytes, not just the inventory. Five findings overturn what the file extensions suggest, and the plan depends on them:

1. **All 411 `.db` files are `Thumbs.db`.** There is no real Tally `.db` in the corpus. They are non-data artifacts, not a reader gap.
2. **Extensions lie at scale.** 61 of 66 `.xlk` are actually OOXML, not legacy BIFF. Of 60 sampled `.xls`, 11 are OOXML and 7 are plain text — a ~30% misfile rate. Extension-based routing would silently corrupt this bucket.
3. **Many ITR-V/TIS PDFs carry `/Encrypt` but are owner-password-only** and open with an empty password. Naive locked-detection would have discarded hundreds of the most analytically valuable filings.
4. **Extensionless files (28) and mangled-extension files sniff cleanly** — 12 are PDFs, 7 are ITR JSON, 2 HTML, 2 ZIP.
5. **`~$`-prefixed Excel owner-lock stubs** (38 bytes of junk) sit inside the `.xlsx` count and must be excluded as non-data.

**Consequence: format detection is signature-first, never extension-first.** Extension becomes evidence recorded in the format doc, not a routing decision.

## Architecture

### Package naming — a deviation to record

`AGENTS.md` mandates `src/ca-agent/`, but a hyphen is not a valid Python identifier, so that directory can never be imported. Since you did not approve an `AGENTS.md` edit, the code uses **`src/ca_agent/`** with distribution name `ca-agent` (standard PEP 503 practice) and the deviation is recorded in `docs/design/ADR.md`, which is approved. Same for tests: the repo already has `tests/`, so Phase 1 uses `tests/{unit,integration,plans,testdata}` rather than creating a second `test/` tree — also recorded in ADR.

### Module layers

`ARCHITECTURE.md` requires acyclic dependencies, higher layers depending on lower, and **no dependencies between modules in the same layer**. Entry point `src/main.py` is a thin shim to `ca_agent.cli:main()`.

| Layer | Module | Owns |
|---|---|---|
| L0 | `core/` | Frozen domain types, status/error enums, `WorkKey`. Pure stdlib, zero I/O |
| L1 | `config/` | TOML + env → validated frozen settings; per-section fingerprints |
| L1 | `storage/` | Atomic writes (`os.replace`), path builders, JSONL append, Parquet/Markdown sinks |
| L2 | `catalog/` | Discovery walk, `ScopeResolver`, streaming SHA-256, `ScopeDedupIndex` |
| L2 | `versioning/` | Version allocation, manifest write/seal, active resolution, reuse decision |
| L2 | `journal/` | Append-only processing and exception records |
| L3 | `readers/` | Format sniffing, tabular, text, JSON/XML, archive extraction |
| L3 | `vision/` | OpenAI-compatible client, image preprocessing, PDF rasterization |
| L3 | `chunking/` | Splitters + chunk metadata |
| L3 | `docgen/` | Renders `*.format.md` from observations + records |
| L4 | `pipeline/` | Routing policy, executor, scheduler, resume |
| L5 | `cli/` | Arg parsing, run modes, reporting |

The same-layer rule does real work here: `docgen` cannot call `readers`, which forces readers to **return** descriptive value objects instead of writing their own docs. Routing sits in L4 because it is the only place permitted to know all three L3 routes exist.

### Core types

All `@dataclass(frozen=True, slots=True)` in `core/`. Per Thinking Craftsman "Tell, Don't Ask", no accessor returns a live internal collection — callers get copies.

- **`ClientScope`** — `scope_id = slug(category)__slug(client)__sha1(category+NUL+client)[:12]`. Equality is by `scope_id`, **never by client name**.
- **`SourceRef`** — scope, relative path, `archive_chain: tuple[ArchiveRef, ...]` (≤3), size, mtime. Provides `companion_name()` → `report.xlsx.format.md`.
- **`WorkKey`** — `(scope_id, content_hash, route, route_fingerprint)`. The unit of scheduling, dedup and reuse.
- `ContentHash`, `ArchiveRef`, `FormatObservation`, `RouteDecision`, `ProcessingRecord`, `OutputVersion`, `Manifest`, `ProcessingStatus`, `ErrorCategory`.

### Client scope resolution — two traps

Scope is `(category, client)`, never client name alone: LIC Employees exists under both `Cooperative Audits` and `GST Proprietor` and must not share a dedup scope. And `Mauli Hospital Tally Back up/` has **no client level** — the category directory itself is the scope, so any `path.parts[1]` extractor breaks.

`ScopeResolver` is therefore built from an explicit scope table (93 `<category>/<client>` roots plus Mauli flagged `category_is_scope`), resolving by **longest matching prefix**. No match produces an `UNSCOPED_PATH` record — never a guess. Empty `GST Audit Clients` yields zero scopes, not an error.

## Output layout and the versioning contract

Root is `data/silver/` (add `data/` to `.gitignore`).

```
data/silver/
  runs/<run_id>/            run.json | worklist.jsonl | completed.jsonl   (append-only)
  scopes/<scope_id>/
    content/<hh>/<content_hash>/<version_id>/  outputs/{parquet,text,chunks,markdown}/ + record.json
    sources/<path_slug>/<version_id>/          <name>.format.md + lineage.json
    manifests/manifest-<ordinal:06d>.json
  extracted/<scope_id>/<archive_hash>/<version_id>/<member_path...>
  manifests/root-<ordinal:06d>.json
```

**Version identity.** At run start the orchestrator single-threadedly allocates `run_ordinal = max(existing)+1`; `version_id = f"r{ordinal:06d}"`. Every worker's output directory is then unique *by construction* — so "never overwrite" is enforced by the path scheme, not by discipline, and parallel writers cannot collide.

**Atomic publish.** A worker writes all outputs into its unique version dir, then writes `record.json` **last** via tmp → `os.replace` (atomic on NTFS). A version dir without `record.json` is an abandoned attempt: ignored everywhere, never deleted. At run end the main thread writes each touched scope manifest then `root-<ordinal>.json`, each sealed with a body checksum via `O_CREAT|O_EXCL` + `os.replace`.

**Active resolution** globs `manifests/root-*.json` and takes the highest sealed ordinal. There is deliberately **no mutable `LATEST` pointer** — a pointer would be a mutation of prior state and a torn-read hazard. A crash mid-run leaves only unsealed temporaries and the previous manifest stays active.

**Config fingerprint is per-section, not global** — `tabular`, `text`, `chunking`, `vision`, `archive`, `embedding`. Requirement 8 invalidates "affected outputs", so changing `vision.temperature` must not invalidate 2,485 Parquet files. Each is `sha256(SCHEMA_VERSION ∥ name ∥ canonical_json(section))[:16]`, with pydantic `extra="forbid"`, materialised defaults, quantised floats and excluded secrets so the hash is stable. Each output records only the sections that produced it.

**Reuse decision** (`versioning/reuse.py`): no prior → process · fingerprint changed → reprocess · success + match → reuse (new source path adds a lineage entry only, no new content version) · `partial` → retry, prior stays history and never active · `locked`/`API_ERROR` → retry per policy · `NO_COMPATIBLE_READER` → retry only on fingerprint change.

## Processing routes

**Detection** is three-stage: `puremagic` signature on first 4 KiB + ZIP EOCD tail → container probe (`zipfile.namelist()` distinguishes xlsx/docx/pptx; `olefile` stream list distinguishes xls/doc/**encrypted OOXML**) → text probe via `charset-normalizer` then JSON/XML/delimiter sniff. `puremagic` is chosen over `python-magic` because the latter needs a `libmagic` DLL that `uv` cannot install on Windows.

| Bucket | Count | Route |
|---|---:|---|
| xlsx/xlsm/xlsb/xls/csv/xlk | 3,203 | `TABULAR_PARQUET`, sub-reader by **sniffed** subtype |
| pdf | 6,946 | `PDF_TEXT` or `PDF_VISION` per classifier below |
| jpeg/jpg/png/bmp/gif | 2,298 | `VISION_IMAGE` |
| docx/txt/htm/html/rtf/eml/log/pptx | 729 | `TEXT_EXTRACT` |
| doc | 73 | `TEXT_EXTRACT` if LibreOffice enabled, else `NO_READER` |
| json/xml/xsd + mangled + sniffed extensionless | ~356 | `STRUCTURED` → fans to Parquet or text |
| zip/jar/7z/gz | 654 | `ARCHIVE` |
| rar | 24 | `ARCHIVE` if unrar enabled, else `NO_READER` |
| Tally .1800/.900/.tsf/.001 + .signature/.fvu/.cer/.pfx/.mdb/.xps/.emz | ~1,839 | `NO_READER` + `NO_COMPATIBLE_READER` |
| Thumbs.db (411), `._*`, `~$*`, css/js/lnk/thmx | ~500 | `NON_DATA` |
| exe/bat/sh | 7 | `NON_DATA`, `EXECUTABLE_NOT_PROCESSED` |

`NON_DATA` and `NO_READER` still produce a full processing record, content hash and `.format.md` — only the error category differs. Nothing is silently skipped, satisfying AC9. **Nothing is ever executed**: no `subprocess` on a source file, no `pickle`, no `eval`, no macro engine, and no `extractall` onto unsanitised member paths.

### Tabular → Parquet (req 1)

`openpyxl` (`read_only=True, data_only=True, keep_vba=False`) for xlsx/xlsm, `xlrd>=2` for xls, `pyxlsb` for xlsb, stdlib `csv` + `charset-normalizer` for delimited text.

**pandas is deliberately excluded from the read path** and `pyarrow` is used directly. pandas coerces `"0012345"` → `12345`, which would destroy PAN, GSTIN and account numbers across the entire corpus. Instead every cell is captured as `(raw_text, source_type)` and a column is emitted as typed **only if** every non-empty cell parses to one type *and* round-trips (`str(parsed) == raw_text.strip()`). Anything else stays `pa.string()` with `type_inference: rejected` recorded. Rows are never dropped; a failing cell is written as its raw string and logged.

Formula cells get two passes — `data_only=True` for cached values, `data_only=False` to confirm a `None` is a formula → `FORMULA_NO_CACHED_VALUE` warning with `sheet!cell`. Empty sheets are recorded with no Parquet file.

### PDF classification (req 2 vs 3) — the highest-stakes branch

`pypdf` (structure, encryption, page count) + `pdfplumber` (text with layout) + `pypdfium2` (rasterization). **PyMuPDF is deliberately rejected — it is AGPL-3.0** and this is proprietary software; pypdfium2 is BSD/Apache.

Per page: `TEXT` if ≥120 non-whitespace chars · `SCANNED` if <120 chars and image coverage ≥50% · `EMPTY` if <20 chars and coverage <5% (recorded, never embedded) · else `MIXED`, treated as scanned. All pages `TEXT` → `PDF_TEXT`; any `SCANNED`/`MIXED` → `PDF_VISION`. Thresholds are config, stamped into the fingerprint.

**Duplicate text is avoided structurally, not by diffing**: in a mixed PDF, `TEXT` pages emit native text with no vision call (cheaper and lossless), `SCANNED` pages emit vision output only. A `MIXED` page's native layer is appended in a labelled block only when token Jaccard vs the vision text < 0.5.

**Partial path**: failed pages emit an inline `> **PAGE 7 — EXTRACTION FAILED**` marker in page order, doc status is `partial`, the version is written and retained, but the manifest's active pointer only advances on `success` — so partial output is history-but-not-retrievable, exactly as req 3 demands.

### Vision route (req 3)

Separate script per the spec. `Pillow` preprocesses bmp/gif → PNG and downscales oversize images; `pypdfium2` rasterizes PDF pages at configurable DPI (default 250). Transport is **`httpx`, not the `openai` SDK** — an OpenAI-compatible endpoint is plain HTTP, and `httpx.MockTransport` makes retry/backoff deterministically testable with zero network.

The prompt fixes a Markdown contract (`## Visible Text`, `## Fields and Values`, `## Tables`, `## Uncertainties`) and instructs explicitly that unreadable values be marked `[UNREADABLE]` / `[UNCERTAIN: …]` and **never inferred**. `temperature=0`. Retry is bounded exponential backoff with jitter honouring `Retry-After`, on `429`/timeout/connection/5xx only — never on 400/401/403/413. A file is `ok` only when a response was received, parsed, and the required sections are non-empty; otherwise `RESPONSE_PARSE_ERROR`, status failed. Cost is bounded because hash-dedup runs first: at most one vision call per `(content, vision_fingerprint)`.

### Text extraction + chunking (req 2, Silver half)

`python-docx`, `striprtf`, `lxml.html`, stdlib `email`, stdlib + `charset-normalizer` for txt/log. Chunking via `langchain-text-splitters` (langchain is already in the approved stack).

Chunk records are JSONL beside the text output, written for Gold to consume verbatim: `chunk_id`, `client_scope_id`, `category`, `client`, `source_relpath`, `archive_id`, `member_path`, `content_sha256`, `unit_type` (`page|heading|sheet|field_path`), `unit_ref`, `seq`, `char_start/end`, `text`, `text_sha256`, `extraction_config_version`. No `embedding_model`/`dimension` yet — Gold adds those. Empty or whitespace-only chunks are never emitted (req 2's "do not embed empty text"); the count is recorded. Format docs carry `tables_detected` vs `tables_extracted` as separate counters, so detection is never reported as extraction.

### JSON/XML by observed structure (req 6)

Routed by structure, not extension. An array node is a tabular collection iff length ≥3, ≥90% object elements, mean pairwise key-set Jaccard ≥0.8, ≥70% scalar leaves, and inner depth ≤2. Those flatten to dotted field paths (`Schedule.Items.0.Amount`) with the **same round-trip typing rule** as tabular — ITR JSON is full of zero-padded PANs and TANs. Everything else renders as `field.path: value` lines in document order and goes to chunking with `unit_type=field_path`, so every chunk traces to its field path.

### Archives (req 7)

`zipfile`, `py7zr`, `gzip`+`tarfile`; `rarfile` config-gated. Extracted to `data/silver/extracted/...`; the source is opened read-only. Guards, all configurable and all emitting explicit records: depth 3, total expanded bytes, member count, and a per-member compression ratio (default 100:1) checked **while streaming** rather than trusting the declared size. Member paths are rejected for `..`, absolute/UNC/drive prefixes and Windows reserved names → `UNSAFE_MEMBER_PATH`. Encrypted members detected via `flag_bits & 0x1` / `needs_password()` → `ARCHIVE_MEMBER_LOCKED`, siblings continue. Members inherit archive lineage and re-enter the router.

### Error taxonomy (req 5)

Requirement 5 insists locked files stay distinct from every other failure. Statuses: `success`, `partial`, `locked`, `failed`, `no_reader`, `excluded_non_data`, `skipped_duplicate`.

Encryption detection differs per container and this is where naive code fails:

| Container | Test | Trap |
|---|---|---|
| ZIP/7z/rar | `flag_bits & 0x1`, `needs_password()` | `zipfile`'s `RuntimeError("encrypted")` is a *string* check — use the flag bit |
| **PDF** | `is_encrypted` → **then `decrypt("")`** | Non-zero return = owner-only → **readable, not locked**. Only `NOT_DECRYPTED` is locked. Governs hundreds of ITR-V PDFs |
| OOXML | `olefile` + `EncryptedPackage`, confirmed by `msoffcrypto` | A `.xlsx` that is a plain ZIP but won't open is corrupt, not locked |
| Legacy OLE | `msoffcrypto.is_encrypted()` | `XLRDError` also fires for BIFF4 and truncation — check encryption *first* |

`msoffcrypto-tool` is used **for detection only**; no password is ever supplied, requested, or guessed.

**Exception discipline** (Thinking Craftsman mandatory): each reader owns an `_EXCEPTION_MAP: dict[type[BaseException], ErrorCategory]` of concrete types, and route entry points catch only `tuple(_EXCEPTION_MAP)`. Anything unmapped propagates to the per-file worker boundary which records `UNEXPECTED_EXCEPTION` *with traceback* and logs it — the batch continues, but nothing is ever swallowed. **No bare `except Exception:` anywhere.**

## Configuration

`config/pipeline.toml` (committed) ← `config/pipeline.local.toml` (gitignored) ← env `CAAGENT__<SECTION>__<KEY>` ← CLI `--set`. Validated by **pydantic v2**, justified because it produces one artifact that both validates *and* emits the canonical JSON the fingerprint needs — the stability property req 8 depends on — and is already required by the later FastAPI phase.

`vision.api_key` is a `SecretStr` sourced only from env/`.env`, never TOML, never logged, excluded from the fingerprint. A committed `.env.example` documents the names; the loader raises at startup, not at first API call.

## Bootstrap deliverables

Nothing exists yet — no `pyproject.toml`, no `environment.bat`, no `.python-version`, and `.gitignore` is one line.

- **`pyproject.toml`** — uv, `requires-python = ">=3.10.9,<3.11"`, hatchling, `packages = ["src/ca_agent"]`, console script `ca-agent`, `[tool.pytest.ini_options]`. Runtime deps: `pydantic`, `pydantic-settings`, `tomli`, `pyarrow`, `openpyxl`, `xlrd>=2`, `pyxlsb`, `puremagic`, `olefile`, `msoffcrypto-tool`, `charset-normalizer`, `pypdf`, `pdfplumber`, `pypdfium2`, `python-docx`, `striprtf`, `lxml`, `pillow`, `py7zr`, `httpx`, `langchain-text-splitters`, `tqdm`. Optional extra: `rarfile`. Dev group: `pytest`, `pytest-cov`, `pytest-mock`, `pyzipper` (test-only, to *create* AES zip fixtures), `ruff`. **Excluded and why**: pandas (coerces leading zeros), PyMuPDF (AGPL), openai SDK (httpx is more testable), beautifulsoup4 (lxml suffices), fastapi/streamlit/langgraph/faiss/sentence-transformers (later phases).
- **`.python-version`** → `3.10.9`. **`uv.lock`** committed.
- **`environment.bat` / `environment.sh`** — set `REPO_ROOT`, `SRC_ROOT`, `TEST_ROOT`, `PYTHONPATH`, `UV_PROJECT_ENVIRONMENT`, `CAAGENT_CONFIG`, `.venv` on PATH, load `.env`, and **`PYTHONUTF8=1`** which is not optional here because client paths contain Devanagari and `&`/`(` characters. Gitignored per `devenv.md`, with a committed `scripts/environment.bat.template` so a fresh clone can regenerate it.
- **`.gitignore`** — add `data/`, `build/`, `environment.bat`, `environment.sh`, `.env`, `!.env.example`, `config/*.local.toml`, `.venv/`, `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `.coverage`, `.ruff_cache/`, `Thumbs.db`.

## Testing

**Resolving a rule conflict.** The Thinking Craftsman skill says "if there are no unit test guidelines, generate test descriptions and not code" — but that clause is conditional, `ARCHITECTURE.md` names pytest, and `workingrules.md` mandates runnable passing tests and says project rules win. So the clause does not fire. Phase 1 keeps its value anyway: **write `tests/plans/TP-01-silver-pipeline.md` first** using the skill's Test Description template (Test / Description / Inputs / Expected Output) with an AC→test traceability table, *then* implement pytest functions named after those IDs. This satisfies "write the tests before making any changes" with a reviewable artifact and keeps it out of `docs/`.

**Fixtures are generated, never committed.** Real client data is confidential and gitignored, so builders in `tests/testdata/builders/` synthesize into `tmp_path`: a multi-sheet xlsx with leading-zero PAN/GSTIN columns, a mixed-type column and an uncached formula; AES-encrypted zip (`pyzipper`); encrypted PDF; **owner-password-only PDF**; nested zip at depth 4 to trip the depth-3 cap; text PDF; scanned PDF (Pillow → image-only); mixed PDF; JSON bytes named `.xlsx`; `Thumbs.db`; fake Tally `.tsf`. Zip timestamps and PDF `/ID` are pinned so SHA-256 assertions are exact. Narrow exception: 2–3 tiny (<20 KB) `.xls`/`.xlsb` binaries committed with a provenance README, since writing true BIFF8/XLSB would need write-side dependencies not worth adding.

**Zero network, proven not asserted.** An autouse session fixture monkeypatches `socket.socket.connect` and `httpx.HTTPTransport.handle_request` to raise. `VisionClient` takes an injected `httpx.Client`; tests inject `MockTransport`. Backoff is tested by scripting `[429, 503, 200]` and recording a stubbed `sleep` — assert delays `== [0.5, 1.0]`, never assert on wall-clock timing.

Key named tests: `test_rerun_does_not_mutate_any_prior_version_artifact` (snapshot `{relpath: sha256}` across the whole output root before and after — hash comparison, because Windows mtime and ACLs are unreliable) · `test_active_version_resolves_to_newest_successful_version` · `test_partial_version_retained_in_history_but_absent_from_active_manifest` · `test_unchanged_content_and_fingerprint_skips_reader` (assert the reader mock was **not called**) · `test_vision_fingerprint_change_invalidates_only_vision_outputs` · `test_identical_bytes_in_two_categories_do_not_deduplicate` · `test_mauli_hospital_backup_root_is_one_client_scope` · `test_nested_zip_beyond_max_depth_records_explicit_failure_not_silent_skip` · `test_owner_password_pdf_is_extracted_not_marked_locked` · `test_leading_zero_identifier_column_stays_string_in_parquet`.

AC1–AC6 are unit-level; AC7, AC8, AC9, AC10, AC11, AC12 need a multi-client tree and are integration tests. Real `raw_data/` is smoke-only, run manually, never in pytest.

## Operations

`src/main.py` shims to `ca_agent.cli:main()`. Subcommands: `discover`, `run`, `retry`, `status`, `report`, `verify`, `config show|hash`. `run` flags: `--category`, `--client`, `--path-glob`, `--stage`, `--limit`, `--dry-run`, `--force`, `--workers`, `--run-id`.

Console logging is one line per file at INFO; the full record per file goes to `data/silver/runs/<run_id>/run.jsonl`. **Failures never drown the operator** — the end-of-run report prints a status × error-category matrix, per-category rollup, and the 20 most common failures, with full detail queryable from the JSONL.

Discovery, hashing and dedup run as one single-threaded-merge pass producing a sealed `worklist.jsonl`. Execution then uses a bounded thread pool for vision behind a token-bucket rate limiter and a process pool for Parquet work, with **one writer thread** appending completions so lines never interleave. Resume is idempotent: reuse is already keyed on `(scope, hash, fingerprint)`, so re-invoking `run` after a Ctrl-C naturally skips completed work.

## Implementation sequence

Each step is: test-plan entries → pytest → implementation → `sourcemap.md` entry. Steps 1–3 build the durability contract before any reader exists, which is deliberate — every later step depends on it being right.

| # | Step | Delivers |
|---|---|---|
| 0 | Bootstrap | pyproject, .python-version, environment scripts, .gitignore, package skeleton, pytest wiring, `TP-01` test plan |
| 1 | Core + config | Frozen domain types, enums, pydantic config, per-section fingerprinting |
| 2 | Storage + versioning | Atomic writes, version allocation, manifest seal/resolve, reuse decision |
| 3 | Catalog | Discovery, `ScopeResolver` (incl. Mauli), streaming SHA-256, scope-isolated dedup |
| 4 | Detection + router | Signature/container/text sniffing, full route decision table |
| 5 | Archives | zip/7z/gz, depth-3 recursion, all caps, path sanitation, locked members |
| 6 | Tabular → Parquet | All spreadsheet readers, round-trip typing, formula and empty-sheet handling |
| 7 | Text + chunking | Document readers, chunk records with full lineage metadata |
| 8 | JSON/XML | Structural routing, field-path flattening and lineage |
| 9 | PDF classifier | Per-page text/scanned/mixed, doc-level route, partial handling |
| 10 | Vision route | httpx client, preprocessing, rasterization, prompt contract, retry/backoff |
| 11 | Format docs | `*.format.md` renderer covering every source type in the req-4 table |
| 12 | Pipeline + CLI | Orchestration, scheduling, resume, logging, report |
| 13 | Integration + full run | AC7–AC12 integration tests, then the real 16,596-file run |

Steps 0–3 are the natural first review checkpoint: at that point discovery, hashing, scoping, dedup and the immutability contract are testable end-to-end with no readers at all.

## Verification

```
.\environment.bat && uv sync --all-groups
.\environment.bat && uv run pytest -q
.\environment.bat && uv run pytest --cov=src/ca_agent --cov-report=term-missing
.\environment.bat && uv run python src\main.py config hash
.\environment.bat && uv run python src\main.py discover
.\environment.bat && uv run python src\main.py run --category "Cooperative Audits" --limit 50 --dry-run
.\environment.bat && uv run python src\main.py run --category "Cooperative Audits" --limit 50
.\environment.bat && uv run python src\main.py run --category "Cooperative Audits" --limit 50   # expect 50 reused, 0 new versions
.\environment.bat && uv run python src\main.py verify --check-immutability
.\environment.bat && uv run python src\main.py report --run-id <id>
```

The third `run` is the real proof of requirement 8: identical invocation, zero new content versions, zero mutated artifacts.

## Open items needing your input during implementation

1. **External-tool answer conflict** (see top) — confirm the config-gated reading.
2. **Concrete cap values** for depth-3 expansion: total expanded bytes and member count per root archive. Proposed defaults 2 GB / 10,000 members.
3. **`.signature` (45) and `.fvu` (1)** — ITR digital-signature and TDS-FVU files. Data, or record-only? Proposed: record-only.
4. **`.css` (25) / `.js` (11)** — classified `NON_DATA`, though req 6's "every discovered file" could arguably mean chunking them. Proposed: `NON_DATA` with a record.
5. **Vision spend** — the classifier runs before any paid call, so `discover` can report exact image and scanned-page counts. Worth reading that number before launching step 13 even though you chose to run vision fully.
