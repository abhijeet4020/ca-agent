# Index of source code for Financial Agentic application

## Index format is 
- `<relative filepath from src>` : purpose of the source file.

## How to use this file
- Use this file to determine which source code file to load based "purpose" of the source code file
- Do not load all source code documents every time.
- Update the index whenever you add a new source code file. 

## Sourcemap Index

### Entry point
- `src/main.py` : Top-level launcher required by ARCHITECTURE.md. Thin shim delegating to `ca_agent.cli.main()` so the script path and the installed console script share one implementation.

### Layer 0 - core (pure domain, no I/O)
- `src/ca_agent/core/enums.py` : Closed vocabulary of processing status, error category and route. Prevents any layer inventing an ad-hoc outcome string, which would defeat SPEC-01's "no silent skips" guarantee.
- `src/ca_agent/core/scope.py` : `ClientScope`, the (category, client) identity that SPEC-01 req 7 makes the unit of deduplication. Provides the slug-plus-hash `scope_id` that stops same-named clients in different categories colliding.
- `src/ca_agent/core/model.py` : Frozen value objects carrying lineage from raw bytes to derived artifacts - `ContentHash`, `ArchiveRef`, `SourceRef`, `WorkKey`, `ProcessingRecord`, `OutputVersion`. Includes the companion-document naming rule from SPEC-01 req 4.
- `src/ca_agent/core/text.py` : `TextUnit`, one addressable piece of a document with the reference an agent would cite it by. Lives in L0 because readers and chunking are the same layer and cannot import each other; this plus `UnitType` is the entire contract between them.

### Layer 1 - config and storage
- `src/ca_agent/config/settings.py` : Every processing tunable, layered TOML then environment then overrides, validated with extra-forbid. Credentials arrive only from the environment as `SecretStr`. Config is kept strictly separate from runtime logic.
- `src/ca_agent/config/credentials.py` : Turns credentials the firm holds for its own clients into candidate document passwords (ADR-016), keyed by category and client so one client's credential is never tried against another's files. Derives the Income Tax portal's documented AIS/TIS scheme and nothing else - no permutation, no brute force - and keeps every secret out of its own repr.

- `src/ca_agent/config/fingerprint.py` : Per-section canonical JSON and SHA-256 fingerprints that drive the SPEC-01 req 8 reuse decision. Sorted keys, materialised defaults and quantised floats make the hash stable across runs.
- `src/ca_agent/storage/atomic.py` : Crash-safe publication primitives. Temp-file-plus-replace for ordinary writes, create-exclusive for sealed manifests, so a published artifact can never be silently overwritten.
- `src/ca_agent/storage/paths.py` : Owns the Silver output layout. Version directories keyed by run ordinal are what make "never overwrite" hold by construction rather than by discipline.

### Layer 2 - catalog, versioning, journal
- `src/ca_agent/catalog/scope_resolver.py` : Longest-prefix resolution of a corpus path to its scope, plus scope-table discovery. Handles the Mauli Hospital backup, where the category directory is itself the client scope.
- `src/ca_agent/catalog/hashing.py` : Streaming SHA-256 over fixed blocks, computed before any conversion per SPEC-01 req 7. Never loads a whole file into memory.
- `src/ca_agent/catalog/dedup.py` : Per-scope content index. Refuses registrations from another scope, so cross-category deduplication is structurally impossible rather than merely discouraged.
- `src/ca_agent/catalog/discovery.py` : Walks the untouched Bronze corpus into `SourceRef` values, returning unscoped and unreadable paths separately so nothing is dropped without a record.
- `src/ca_agent/catalog/bundles.py` : Finds directories that are an unpacked desktop application rather than client records, by two co-occurring Java-application markers rather than by folder name. Lives in L2 because it is the only layer that sees directory structure; a single corpus folder holds the e-filing utility and accounts for 19 percent of all extractable text.
- `src/ca_agent/versioning/manifest.py` : Sealed, numbered, cumulative snapshots implementing "active retrieval resolves to the latest successful version". `publish_manifest` is the only supported publish path because it cannot drop history.
- `src/ca_agent/versioning/allocator.py` : Allocates the run ordinal once per run, giving every worker a collision-free output directory without locks or clock trust.
- `src/ca_agent/versioning/reuse.py` : Pure decision function over a prior outcome - reuse, retry, reprocess on config change, or force. Encodes the SPEC-01 req 8 incremental rules without touching the filesystem.

### Layer 3 - processing routes
- `src/ca_agent/readers/detection.py` : Signature-first format detection. Three stages - magic number, container inspection (a ZIP may be xlsx/docx/pptx/archive; an OLE container may be xls/doc/Thumbs.db/encrypted OOXML), then a decoded-text probe. The extension is recorded as evidence, never used to route. Running this over the corpus recovered 175 spreadsheets and documents that extension-based routing would have lost.

- `src/ca_agent/readers/archives.py` : Expands zip/7z/gzip into a separate area, never touching the source. Depth-limited recursion, a streaming compression-ratio guard (a declared member size cannot be trusted), member-path sanitation, and per-member failure records so a locked or malformed member never stops its siblings.

- `src/ca_agent/readers/tabular.py` : Converts spreadsheets and delimited text to Parquet, one output per populated worksheet. Captures every cell as both text and native value and types a column only when every value round-trips, which is what preserves zero-padded PAN, GSTIN and account identifiers. Reports uncached formula cells and empty sheets; rows are never dropped and values never coerced.

- `src/ca_agent/readers/pdf.py` : Classifies every PDF page as text, scanned, mixed or empty from measured characters and image coverage, and extracts native text for the pages that have it. Decides whether a document needs the paid vision route without ever calling it. Implements ADR-008's encryption distinction: an encryption dictionary is not a locked file, and only a document the empty password cannot open is `PASSWORD_PROTECTED_FILE`. A page that fails is marked and the document reported partial, so one bad page never costs the rest.

- `src/ca_agent/readers/structured.py` : Routes JSON and XML by observed structure (req 6). An array of at least three similar, flat, shallow objects is a register and becomes Parquet through the tabular reader's round-trip typing; everything else renders as `field.path: value` lines for the chunker. XML is parsed with entity resolution, DTD loading and network access disabled, with a recorded recovery fallback for the Tally exports that embed raw control characters. Units are size-bounded because a Tally register has one root child holding the whole document.

- `src/ca_agent/readers/text.py` : Extracts text from docx, pptx, rtf, html, email and plain text as ordered `TextUnit`s. Recovers docx body order from the XML because python-docx exposes paragraphs and tables as separate collections, strips script and style bodies from HTML, and counts tables detected separately from tables extracted. pptx slide text is read from the package XML rather than adding python-pptx for the corpus's two presentations. Legacy `.doc` is converted to docx by LibreOffice first - the one place the pipeline runs an external binary, hardened with a throwaway profile and a timeout so the document is data rather than something that can act.

- `src/ca_agent/chunking/records.py` : The chunk record the Gold layer consumes verbatim, plus the `ChunkSet` that carries what was deliberately not emitted. Every field exists so a retrieved chunk can be traced to one scope, path, archive member, unit and character range once the source document is out of hand.

- `src/ca_agent/chunking/splitter.py` : Splits text units into chunk records. Character offsets are resolved by locating each chunk back in its own unit so the lineage claim is verified rather than assumed, and chunk ids are derived from scope, content, unit and configuration so a rerun over unchanged content reproduces them exactly.

- `src/ca_agent/vision/contract.py` : What the model is asked for and the proof that it complied. A strict JSON schema is the wire format, so "did this extraction work" is checkable rather than a judgement about prose; the reply is validated whatever the endpoint promised, and only validated output is rendered to the Markdown SPEC-01 req 3 stores. Carries the never-infer rule: an unreadable value keeps its marker rather than becoming a plausible number.

- `src/ca_agent/vision/client.py` : Calls an OpenAI-compatible endpoint and returns a validated extraction or an explicit failure, never a maybe. Retries only faults that can clear, never a rejected request, honours Retry-After, and bounds attempts. Takes an injected httpx client (ADR-011) so backoff is tested from a recorded sleep sequence with no network.

- `src/ca_agent/vision/preprocess.py` : Normalises corpus images the endpoints reject (bmp, gif, tiff) and bounds the longest edge, which bounds both request size and per-image cost. Rasterises PDF pages with pypdfium2, never PyMuPDF (ADR-010).

- `src/ca_agent/docgen/model.py` : The neutral description of one processed file that becomes its `*.format.md` (req 4). docgen and readers are the same layer, so the orchestrator translates whichever reader ran into this shape - that indirection is what keeps every reader free of document-writing code. Absence is the explicit UNKNOWN string, never an omission.

- `src/ca_agent/docgen/renderer.py` : Renders a FormatDocument as Markdown. Every section is emitted even when empty, because an omitted section cannot be told from one nobody attempted; table cells are escaped, since a pipe in a transcribed column name would reshape the table and turn a correct observation into a wrong one.

- `src/ca_agent/gold/embedder.py` : Turns chunk text into vectors behind a protocol, so nothing else in Gold depends on which model produced them. The backend is injected because sentence-transformers pulls in torch, and a suite that must load a 2 GB model to check row alignment is one nobody runs. Carries model name and dimension, both required by req 2.

- `src/ca_agent/gold/index.py` : Builds and reloads one FAISS index, with the vector-to-chunk mapping written as an explicit ordered sidecar rather than inferred at read time. Reload verifies index and mapping agree and refuses otherwise: an index off by one row answers confidently with the wrong client's text.

- `src/ca_agent/gold/search.py` : Answers a question against the client indexes - the point of the whole pipeline. Searches across indexes rather than merging them, because req 7 keeps each client's vectors separate and merging would destroy that boundary; every hit names its client, and a query embedded by a different model is refused rather than answered with nonsense.

- `src/ca_agent/gold/builder.py` : Builds one index per client scope from Silver's chunk records. Scope isolation is structural - a chunk whose recorded scope disagrees with the directory holding it is refused, never indexed. Empty text is skipped and counted (req 2), row order is stable across builds, and each build publishes a new version directory.

### Layer 4 - orchestration
- `src/ca_agent/pipeline/executor.py` : Runs one file down its chosen route and returns outputs, observations for the format document, and a terminal status. The only place that knows all the readers exist, which is why it is L4. Every route returns rather than raises, because req 6 needs a record for every file and 16,596 of them cannot end on the first damaged one.

- `src/ca_agent/pipeline/orchestrator.py` : The run itself - discover, hash, deduplicate, decide, execute, record, publish. The ordering is the immutability contract in practice: one run ordinal so every output directory is unique by construction, `record.json` written last so a directory without one is an abandoned attempt, and manifests sealed only at the end so a crash leaves the previous run active. Archive members re-enter the same loop and get their own records.

- `src/ca_agent/pipeline/routing.py` : Maps an observed format to its processing route. Lives in L4 because choosing between routes requires knowing all of them exist, which ARCHITECTURE.md forbids an L3 module from doing. Exhaustive over FormatFamily - an unrouted family raises rather than defaulting, so SPEC-01 req 6 coverage cannot silently regress.

### Frontend
- `streamlit_app.py` : The operator-facing search interface over the Gold layer. The CLI can already answer a question, but a chartered accountant should not need a terminal to ask one. The model is a cached resource loaded once per session, the question is batched in a form so it is not embedded on every keystroke, and every result carries the client, document and page it came from.

### Layer 5 - CLI
- `src/ca_agent/cli/__init__.py` : Operator entry point. `discover` walks and hashes the corpus with no conversion and no paid calls; `config hash` prints the per-section fingerprints that govern reuse; `vision-extract` sends one image or PDF page to the vision API and prints the validated structured result, deliberately one file per invocation so the cost of a call is visible before the batch makes thousands.
