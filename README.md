# ca-agent — Financial Agentic Application

An agentic AI system that helps chartered accountants with financial analysis, report generation
and data gathering over roughly two decades of real client records.

The corpus is **16,596 files across 94 client scopes** — income tax returns, GST filings,
financial statements, audit papers, bank statements, Tally backups and scanned documents. Almost
none of it is machine-readable today. This project turns it into validated, versioned,
fully-traceable data that agents can query.

---

## Table of contents

- [Architecture](#architecture)
- [Current status](#current-status)
- [Prerequisites](#prerequisites)
- [Initial setup](#initial-setup)
- [Running the pipeline](#running-the-pipeline)
- [Testing](#testing)
- [Project layout](#project-layout)
- [Key design decisions](#key-design-decisions)
- [What the real corpus taught us](#what-the-real-corpus-taught-us)
- [Documentation map](#documentation-map)

---

## Architecture

A medallion data pipeline feeding a LangGraph agent layer and a Streamlit front end.

| Layer | Location | Contents |
|---|---|---|
| **Bronze** | `raw_data/` | The client corpus, **exactly as it is**. Opened read-only, never copied, never written to. No code path writes here. |
| **Silver** | `data/silver/` | Everything deterministic and re-derivable without a model: extracted archive members, Parquet datasets, extracted text, traceable chunks, vision-extracted Markdown, per-file format documents, processing records and versioned manifests. |
| **Gold** | `data/gold/` | Embeddings and per-client FAISS indexes. **Later phase.** |

### Application layers

Dependencies are acyclic, higher layers may depend on lower ones, and **modules in the same layer
may not depend on each other**. This is enforced by `tests/unit/test_architecture.py`, which parses
the real import graph — not just documented.

| Layer | Module | Responsibility |
|---|---|---|
| L0 | `core/` | Frozen domain types; the closed status/error/route/format vocabulary. No I/O. |
| L1 | `config/` | Layered settings and the per-section reuse fingerprints. |
| L1 | `storage/` | Atomic publication: temp-then-replace, create-exclusive, append-only journals. |
| L2 | `catalog/` | Corpus walk, scope resolution, streaming SHA-256, scope-isolated dedup. |
| L2 | `versioning/` | Version allocation, sealed manifests, active resolution, reuse policy. |
| L2 | `journal/` | Append-only processing and exception records. |
| L3 | `readers/` | Format detection and per-format extraction. Returns observations, never writes. |
| L3 | `vision/` | OpenAI-compatible vision transport, image preprocessing, PDF rasterisation. |
| L3 | `chunking/` | Splitting extracted text into traceable chunks. |
| L3 | `docgen/` | Renders the per-file format documents. |
| L4 | `pipeline/` | Route selection, scheduling, resume, record assembly. |
| L5 | `cli/` | Argument parsing, progress, run summaries. |

**Tech stack:** Python 3.10.9, `uv`, pytest, pyarrow, pydantic v2. FastAPI, Streamlit and
LangGraph arrive in later phases and are deliberately not dependencies yet.

---

## Current status

**Phase 1 (Silver layer) is in progress.** Steps 0–5 of 13 are complete and validated against the
full real corpus.

| Step | Scope | State |
|---|---|---|
| 0 | Bootstrap: packaging, environment scripts, test harness | Done |
| 1 | Core domain types, configuration, reuse fingerprinting | Done |
| 2 | Atomic storage, versioning, sealed manifests, reuse policy | Done |
| 3 | Discovery, scope resolution, hashing, client-scoped dedup | Done |
| 4 | Signature-based format detection and the routing table | Done |
| 5 | Archive extraction with depth limits and bomb guards | Done |
| 6 | Tabular → Parquet | In progress |
| 7 | Text extraction and chunking | Pending |
| 8 | JSON/XML structural routing | Pending |
| 9 | PDF text-vs-scanned classification | Pending |
| 10 | Vision extraction route | Pending |
| 11 | Per-file format documents | Pending |
| 12 | Pipeline orchestration and full CLI | Pending |
| 13 | Integration tests and the full corpus run | Pending |

---

## Prerequisites

- **Python 3.10.9** (pinned in `.python-version`; the project refuses 3.11+)
- **[uv](https://docs.astral.sh/uv/)** for dependency management
- **Windows** is the primary development platform; the code is POSIX-clean
- **Disk headroom.** Archive expansion alone produces ~2.3 GiB before any other output.
- *Optional:* **LibreOffice** (`soffice`) to read 50 legacy `.doc` files
- *Optional:* **unrar** to expand 24 `.rar` archives

Without the optional tools those files are recorded as `NO_COMPATIBLE_READER` — visible in their
format documents, never silently skipped.

---

## Initial setup

### 1. Install dependencies

```bash
uv sync --all-groups
```

This creates `.venv/` and installs both runtime and dev dependencies from the committed `uv.lock`.

### 2. Create the environment script

The environment script is machine-specific and gitignored. Copy it from the committed template:

```powershell
# Windows
copy scripts\environment.bat.template environment.bat
```

```bash
# Unix
cp scripts/environment.sh.template environment.sh && chmod +x environment.sh
```

It sets `PYTHONPATH`, the venv path, config locations, and **`PYTHONUTF8=1`** — that last one is
not optional, because client paths contain Devanagari and `&`/`(` characters that the default
Windows ANSI codepage cannot represent.

### 3. Supply credentials

```bash
cp .env.example .env
```

Then edit `.env`. Only the vision API needs a credential, and only from Step 10 onward:

```ini
CAAGENT__VISION__BASE_URL=https://api.openai.com/v1
CAAGENT__VISION__MODEL=gpt-4o-mini
CAAGENT__VISION__API_KEY=sk-...

# Optional external tools; leave blank to record affected files as unreadable
CAAGENT__EXTERNAL_TOOLS__SOFFICE_PATH=
CAAGENT__EXTERNAL_TOOLS__UNRAR_PATH=
```

`.env` is gitignored. Credentials are never written to config files, never logged, and never
included in a reuse fingerprint — rotating a key does not invalidate any output.

### 4. Place the corpus

Put the client data at `raw_data/`, organised as `raw_data/<category>/<client>/...`. It is
gitignored and the pipeline never writes to it.

### 5. Verify

```bash
.\environment.bat && uv run pytest -q
.\environment.bat && uv run python src\main.py config hash
```

---

## Running the pipeline

Always chain the environment script first. On Unix use `source ./environment.sh && ...`.

### Inspect configuration

```bash
.\environment.bat && uv run python src\main.py config show    # full effective settings
.\environment.bat && uv run python src\main.py config hash    # per-section reuse fingerprints
```

Fingerprints matter: changing a value in `config/pipeline.toml` changes that section's hash, which
invalidates exactly the outputs that section produced and nothing else.

### Discover the corpus

`discover` walks and hashes the corpus. It converts nothing, writes no outputs, and makes no paid
API calls — it is safe to run at any time.

```bash
# Walk only: counts, categories, extensions
.\environment.bat && uv run python src\main.py discover --no-hash

# Walk plus content hashing and duplicate analysis
.\environment.bat && uv run python src\main.py discover

# Detect the real format and route of every file
.\environment.bat && uv run python src\main.py discover --classify --no-hash

# Narrow to one category, or cap the file count
.\environment.bat && uv run python src\main.py discover --category "Cooperative Audits" --limit 50
```

### Useful flags

| Flag | Effect |
|---|---|
| `--category NAME` | Restrict to one top-level category |
| `--limit N` | Stop after N files |
| `--no-hash` | Walk without reading file contents (fast) |
| `--classify` | Report detected format families and selected routes |
| `--config PATH` | Use an alternate `pipeline.toml` |
| `--verbose` | Debug logging |

Subcommands `run`, `retry`, `status`, `report` and `verify` arrive with Step 12.

---

## Testing

```bash
.\environment.bat && uv run pytest -q                                  # all tests
.\environment.bat && uv run pytest tests/unit -q                       # unit only
.\environment.bat && uv run pytest -m integration -q                   # integration only
.\environment.bat && uv run pytest --cov=src/ca_agent --cov-report=term-missing
.\environment.bat && uv run ruff check src tests                       # lint
```

**No test touches the network.** An autouse fixture in `tests/conftest.py` makes any real socket
connection raise, so this is enforced by the harness rather than by reviewer discipline. The vision
client takes an injected `httpx` transport; tests supply `MockTransport`.

**No test uses real client data.** Confidential files can never be fixtures, so
`tests/testdata/builders/` constructs genuine small files of each format at run time — real OOXML
packages, real OLE containers, real PDFs, real encrypted archives.

The test plan lives at `tests/plans/TP-01-silver-pipeline.md` and maps every SPEC-01 acceptance
criterion to the tests covering it.

---

## Project layout

```
ca-agent/
├── raw_data/                    Bronze: the corpus, read-only (gitignored)
├── data/                        Silver and Gold outputs (gitignored)
├── config/
│   └── pipeline.toml            Committed defaults; every value feeds a reuse fingerprint
├── scripts/
│   └── environment.bat.template Copy to environment.bat at the repo root
├── src/
│   ├── main.py                  Entry point shim
│   └── ca_agent/                Application package (see layer table above)
├── tests/
│   ├── unit/                    Per-module tests
│   ├── integration/             Cross-module and durability tests
│   ├── plans/TP-01-*.md         Test plan with acceptance-criteria traceability
│   └── testdata/builders/       Synthetic fixture generators
└── docs/
    ├── specifications/          SPEC-01 data pipeline (+ 02, 03 pending)
    ├── design/                  ARCHITECTURE, ADR, packagedesign, sourcemap
    └── raw_data_docs/           Pre-built inventory of the corpus
```

`docs/design/sourcemap.md` lists every source file and its purpose — start there rather than
reading the tree.

---

## Key design decisions

Full rationale is in `docs/design/ADR.md`. The ones that shape everyday work:

**Nothing is ever overwritten or deleted.** Each run allocates one integer ordinal; every version
directory it writes is named from it, so parallel writers cannot collide and "never overwrite" holds
by construction. Active-version resolution scans for the highest *sealed* manifest — there is
deliberately no mutable "latest" pointer, since that would itself be mutable prior state and a
torn-read hazard. A crashed run leaves the previous version active, and its abandoned directories
are ignored rather than removed.

**Format is decided by content, never by extension.** The extension is recorded as evidence only.
On the real corpus, 195 files contradict their own extension.

**pandas is excluded from the tabular read path.** It coerces `"0012345"` to `12345`, which would
destroy PAN, GSTIN, TAN and account identifiers across the whole corpus. Cells are read individually
and written with `pyarrow`; a column becomes typed only if every value round-trips back to its
original text.

**Reuse fingerprints are per config section.** A single global hash would invalidate all 16,596
files whenever any unrelated setting moved.

**Client scope is `(category, client)`, never the client name alone.** Several clients appear in two
categories with near-identical files, and SPEC-01 forbids merging those scopes.

**Nothing is ever executed.** No `subprocess` on a source file, no `pickle`, no `eval`, no macro
engine. Macro-enabled workbooks are read for stored values only.

---

## What the real corpus taught us

Every step is validated against all 16,596 files, which repeatedly found problems that synthetic
tests could not.

- **261 files named `*.pdf` are not PDFs.** They begin with `AC ED 00 05` — Java serialized object
  streams written by the Income Tax e-filing utility. They are recorded as such and **never
  deserialised**, since that is a code-execution vector.
- **Extensions are unreliable at scale.** Only 253 of 330 `.xls` files are genuinely legacy BIFF;
  all 411 `.db` files are Windows `Thumbs.db` artifacts, not Tally databases.
- **Archives hide a large share of the work.** 684 archives expand to 4,201 members including
  **1,617 more images and 937 more PDFs**, taking the vision workload from ~2,300 to ~3,900 images.
- **Nested archives are real** — 242 members sit at depth 2, so a single-level policy would lose
  data. The configured limit is depth 3.
- **129 archive members are password-protected.** They are recorded and skipped; no password is
  ever requested, guessed or brute-forced.
- **A zip member's filename contains embedded newlines**, which Windows cannot write. The on-disk
  name is sanitised while the original is kept as lineage.
- Content hashing found **1,331 duplicate source paths** within client scopes across 10.4 GiB.

---

## Documentation map

| Document | Purpose |
|---|---|
| `AGENTS.md` | Instructions for coding agents; folder map |
| `.agents/workingrules.md` | Hard process rules — plan first, tests first, Purpose comments |
| `docs/specifications/SPEC-01-data_pipeline.md` | The data pipeline specification |
| `docs/design/ARCHITECTURE.md` | Architecture rules and tech stack |
| `docs/design/ADR.md` | Architecture decision records |
| `docs/design/packagedesign.md` | Module dependency graph |
| `docs/design/sourcemap.md` | Every source file and its purpose |
| `docs/devenv.md` | Developer onboarding checklist |
| `docs/raw_data_docs/index_sourcemap.md` | Corpus inventory index (read this, not all 94 client maps) |
| `tests/plans/TP-01-silver-pipeline.md` | Test plan and acceptance-criteria traceability |

---

*Project scaffolded from the Thinking Craftsman agentic engineering template by Nitin Bhide.*
