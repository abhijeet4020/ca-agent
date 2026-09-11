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
- [Execution commands](#execution-commands)
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
| **Bronze** | `raw_data/` | The client corpus, **exactly as it is**. Opened read-only, never copied, never written to. No code path writes here. There is deliberately no `bronze/` folder — a copy would be 11 GB of duplication and a second thing to keep in sync. |
| **Silver** | `data/silver/` | Everything deterministic and re-derivable without a model: extracted archive members, Parquet datasets, extracted text, traceable chunks, per-file format documents, processing records and versioned manifests. |
| **Gold** | `data/gold/` | Embeddings and one FAISS index per client scope, never shared between clients. |

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
| L3 | `gold/` | Embedding, per-client FAISS indexes, and search over them. |
| L4 | `pipeline/` | Route selection, execution, record assembly, run orchestration. |
| L5 | `cli/` | Argument parsing, progress, run summaries. |
| — | `streamlit_app.py` | Search interface over the Gold layer. |

**Tech stack:** Python 3.10.9, `uv`, pytest, pyarrow, pydantic v2, pdfplumber, pypdfium2, httpx.
The `gold` extra adds faiss-cpu and sentence-transformers. Streamlit is the front end. LangGraph
arrives with the agent layer (SPEC-02) and is deliberately not a dependency yet.

---

## Current status

**Phase 1 (Silver) is complete and has been run over the full corpus. Phase 2 (Gold) is built and
indexing.** A Streamlit search interface is live. 443 tests pass, ruff clean.

> **Resuming work?** Read `.agents/memory/session-handoff.md` first, then `docs/design/ADR.md`
> (16 decision records) and `docs/design/sourcemap.md` (every source file and its purpose).

| Step | Scope | State |
|---|---|---|
| 0 | Bootstrap: packaging, environment scripts, test harness | Validated |
| 1 | Core domain types, configuration, reuse fingerprinting | Validated |
| 2 | Atomic storage, versioning, sealed manifests, reuse policy | Validated |
| 3 | Discovery, scope resolution, hashing, client-scoped dedup | Validated |
| 4 | Signature-based format detection and the routing table | Validated |
| 5 | Archive extraction with depth limits and bomb guards | Validated |
| 6 | Tabular → Parquet | Validated |
| 7 | Text extraction and chunking | Validated |
| 8 | JSON/XML structural routing | Validated |
| 9 | PDF text-vs-scanned classification | Validated |
| 10 | Vision extraction route | Built; no live API call made yet |
| 11 | Per-file format documents | Validated |
| 12 | Pipeline orchestration and full CLI | Validated |
| 13 | Integration tests and the full corpus run | Validated |
| — | **Gold layer**: embeddings, per-client FAISS, search | Built; full-corpus index in progress |
| — | **Streamlit search UI** | Built |
| 14 | Dedicated Tally XML voucher route (ADR-015) | Not started |

"Validated" means the step was run over all 16,534 discovered files, not merely unit-tested.

### Full corpus run — `r000001`

```
discovered            16,534        chunks produced      490,338
excluded (bundle)         62        pages needing vision   3,584
processed             16,327        archive members        4,141
duplicates in scope    4,348        unscoped paths             0

success       9,744    no_reader          1,884    failed       36
partial       3,724    excluded_non_data    601    locked      338
skipped_dup   4,348
```

**Every discovered file is accounted for** — the acceptance criterion that matters most. Only 36
genuine failures (0.2%), and just 2 unexpected exceptions, both caught by the per-file boundary and
recorded rather than aborting the run.

`partial` is mostly the vision backlog: 3,595 of those 3,724 are files classified as needing vision
with no call made. They are pending work, not failures.

### Known gaps

| What | Files | Recoverable |
|---|---|---|
| Images awaiting the batch vision pass | 2,280 | Yes — build the vision pass |
| Locked PDFs (213 are AIS/TIS) | 338 | Yes — populate `config/credentials.local.toml` (ADR-016) |
| Tally binaries (`.1800/.900/.tsf`) | 1,617 | No — re-export from Tally |
| Java-serialized `*.pdf` from the e-filing utility | 261 | No — re-export from the utility |
| Legacy `.doc` | 50 | Yes — set `SOFFICE_PATH` |
| `.rar` archives | 24 | Yes — set `UNRAR_PATH` |

Roughly **28% of files currently yield no extracted content**; about 16 points of that is
recoverable with configuration or the vision pass.

**Quality caveat:** six Tally XML exports produce ~370,000 of the 490,338 chunks, and most of it is
Tally's own serialisation boilerplate rather than client data. Step 14 (ADR-015) addresses this;
until then it dominates the vector index.

---

## Prerequisites

- **Python 3.10.9** (pinned in `.python-version`; the project refuses 3.11+)
- **[uv](https://docs.astral.sh/uv/)** for dependency management
- **Windows** is the primary development platform; the code is POSIX-clean
- **Disk headroom.** A full run writes ~5 GB of Silver output plus ~2.3 GiB of expanded archives; Gold adds a few hundred MB.
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

The Gold layer and the web interface need two extras, kept optional because
`sentence-transformers` pulls in torch (~2.5 GB) and nothing in Silver needs it:

```bash
uv pip install -e ".[gold]"    # faiss-cpu, numpy, sentence-transformers
uv pip install streamlit       # the search interface
```

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

`.env` is gitignored, and `.env.example` holds variable *names* only — **never put a real key in
it, it is committed**.

Optionally, `config/credentials.local.toml` (also gitignored, documented by
`config/credentials.example.toml`) maps `category/client` to a PAN and date of birth, which opens
the 213 locked AIS/TIS filings. Nothing is ever guessed or brute-forced; only values you supply are
tried. See ADR-016.

Credentials are never written to config files, never logged, and never included in a reuse
fingerprint — rotating a key does not invalidate any output.

### 4. Place the corpus

Put the client data at `raw_data/`, organised as `raw_data/<category>/<client>/...`. It is
gitignored and the pipeline never writes to it.

### 5. Verify

```bash
.\environment.bat && uv run pytest -q
.\environment.bat && uv run python src\main.py config hash
```

---

## Execution commands

**Every command needs the environment script first.** It sets `PYTHONPATH`, the venv, and
`PYTHONUTF8=1`, and loads `.env`. On Unix use `source ./environment.sh` instead.

```powershell
cd D:\ca-agent
.\environment.bat
```

### Command summary

| Command | Purpose | Costs money? |
|---|---|---|
| `search "question"` | Ask the indexed corpus a question | No |
| `streamlit run streamlit_app.py` | The same search, in a browser | No |
| `run` | `raw_data/` → `data/silver/` | No |
| `gold` | `data/silver/` → `data/gold/` | No |
| `discover` | Walk and hash; convert nothing | No |
| `config show` / `config hash` | Effective settings; reuse fingerprints | No |
| `vision-extract` | One image or PDF page through the vision API | **Yes** |

### Search the corpus

```powershell
uv run python src\main.py search "depreciation on plant and machinery"
uv run python src\main.py search "GST input tax credit" --client "SHAHANE"
uv run python src\main.py search "TDS deducted on interest" -k 10
uv run python src\main.py search "partner remuneration" --category "LLP" --full
```

| Flag | Effect |
|---|---|
| `-k N`, `--top N` | Number of results (default 5) |
| `--client TEXT` | Limit to clients whose name contains TEXT |
| `--category TEXT` | Limit to one category |
| `--scope ID` | Limit to one exact scope id |
| `--full` | Print whole passages rather than extracts |

Every result names the client, the source document and the page, so an answer can be checked
against the original filing.

### The web interface

```powershell
uv run streamlit run streamlit_app.py
```

Opens at `http://localhost:8501`. Query box, client and category filters, corpus summary, and
results with citations. Requires the `gold` extra and at least one built index.

### Build the data

```powershell
# Bronze -> Silver. Several hours for the full corpus; writes ~5 GB.
uv run python src\main.py run

# Silver -> Gold. Embeds every chunk; needs the gold extra installed.
uv run python src\main.py gold
```

| `run` flag | Effect |
|---|---|
| `--category NAME` | Restrict to one top-level category |
| `--limit N` | Stop after N discovered files |
| `--dry-run` | Decide everything, write nothing |
| `--force` | Reprocess even unchanged content (req 8) |
| `--no-retry-locked` | Leave previously locked files alone |

| `gold` flag | Effect |
|---|---|
| `--scope ID` | Build one scope only |
| `--limit N` | Stop after N scopes |
| `--model NAME` | Override the configured embedding model |

**Reruns are cheap.** A second `run` reuses everything already settled and reprocesses only what
the retry policy names — locked files that might now be unlocked, and partial results. **A crash
is not**: manifests seal at the end of a run, so an interrupted run is discarded and redone.

**Do not run two `run` commands at once.** The run ordinal is claimed when manifests seal, so
concurrent runs would allocate the same version and overwrite each other.

### Inspect without processing

```powershell
uv run python src\main.py discover --no-hash                  # walk only: counts, extensions
uv run python src\main.py discover                            # + content hashing and duplicates
uv run python src\main.py discover --classify --no-hash       # + detected format and route
uv run python src\main.py discover --category "LLP" --limit 50

uv run python src\main.py config show                         # full effective settings
uv run python src\main.py config hash                         # per-section reuse fingerprints
```

Fingerprints matter: changing a value in `config/pipeline.toml` changes that section's hash, which
invalidates exactly the outputs that section produced and nothing else.

### Vision extraction — the only command that spends money

```powershell
uv run python src\main.py vision-extract "path\to\scan.pdf" --page 1
uv run python src\main.py vision-extract "path\to\photo.jpg" --format json
uv run python src\main.py vision-extract "path\to\scan.pdf" --output result.md
```

One file per invocation, deliberately, so the cost of a call is visible before anything runs in
bulk. The model is asked for a JSON schema and the reply is validated against it, so a failed
extraction is never reported as a success.

**A local model server costs nothing and needs no key.** Point `.env` at it and leave
`CAAGENT__VISION__API_KEY` unset — no `Authorization` header is sent:

```ini
CAAGENT__VISION__BASE_URL=http://localhost:1234/v1
CAAGENT__VISION__MODEL=google/gemma-3-4b
```

A key is required only for a non-local endpoint, where it still fails fast at load rather than
hours into a run. Set `CAAGENT__VISION__STRICT_SCHEMA=true` for OpenAI; LM Studio rejects that
flag with HTTP 400, which is why it is off by default.

Add `--help` to any command for its full options. Global flags `--config PATH` and `--verbose`
work everywhere.

---

## Testing

```bash
.\environment.bat && uv run pytest -q                                  # all tests
.\environment.bat && uv run pytest tests/unit -q                       # unit only
.\environment.bat && uv run pytest tests/integration -q                # integration only
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
│   ├── pipeline.toml            Committed defaults; every value feeds a reuse fingerprint
│   └── credentials.example.toml Template for the gitignored credential file (ADR-016)
├── scripts/
│   └── environment.bat.template Copy to environment.bat at the repo root
├── streamlit_app.py             Search interface (Streamlit)
├── src/
│   ├── main.py                  Entry point shim
│   └── ca_agent/                Application package (see layer table above)
├── tests/
│   ├── unit/                    Per-module tests
│   ├── integration/             Cross-module and durability tests
│   ├── plans/TP-01-*.md         Test plan with acceptance-criteria traceability
│   └── testdata/builders/       Synthetic fixture generators
├── docs/
│   ├── specifications/          SPEC-01 data pipeline (+ 02, 03 pending)
│   ├── design/                  ARCHITECTURE, ADR, packagedesign, sourcemap
│   └── raw_data_docs/           Pre-built inventory of the corpus
└── .agents/
    ├── memory/                  Session handoff and durable project notes
    ├── plans/                   The approved Phase 1 plan
    └── reference/               User-supplied reference material
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

**An encryption marker is not a locked file.** 200 PDFs carry an encryption dictionary but open
with an empty password; treating the marker alone as "locked" would have discarded that many ITR-V
and TIS filings. Only a document the empty password cannot open is `PASSWORD_PROTECTED_FILE`
(ADR-008, amended by ADR-016).

**One bad file costs one record, not the run.** Readers map the exceptions they know about; a
per-file boundary catches the ones nobody predicted, records `UNEXPECTED_EXCEPTION` with a
traceback, and continues. This exists because its absence killed a full-corpus run at 8,274 files.

**A vector index that cannot be trusted is worse than none.** FAISS returns row numbers, so the
vector-to-chunk mapping is written as an explicit ordered sidecar and verified on reload — a
mapping off by one row answers confidently with the wrong client's text. Indexes are never shared
between clients, and a chunk whose recorded scope disagrees with its directory is refused.

**The model is asked for a strict JSON schema, not prose.** The reply is validated whatever the
endpoint promised, so "did this extraction work" is checkable. An unreadable value keeps its
`[UNREADABLE]` marker rather than becoming a plausible number — an invented amount in an audit file
is the worst output this pipeline could produce.

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
- Content hashing found **4,348 duplicate source paths** within client scopes.
- **openpyxl validates the filename extension before reading content**, so genuine OOXML workbooks
  saved under a stale `.xls` name were all rejected — reintroducing the very extension dependency
  signature-first detection exists to remove (ADR-013).
- **One client folder is an entire unpacked copy of the e-filing utility** — jQuery, a Java
  keystore, every ISIN on the exchange, every bank IFSC code. It was 19% of all extractable text
  and is now excluded as non-data (ADR-014).
- **A document heading repeats 250 times** in one bank statement, so a chunk citing only its
  heading and an offset named no single place until `unit_sequence` was added.
- **Three separate libraries raised an exception that was a sibling, not a subclass, of the one
  being caught** — lxml, xlrd, and pdfminer. The third killed a full-corpus run at the halfway
  point, which is why there is now a per-file boundary that records the unexpected and continues.

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