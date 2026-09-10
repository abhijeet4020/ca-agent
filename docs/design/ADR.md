# Archiecture Decision Record (ADR) Index

This is a index of key Architecture Decision Records. Remember ADRs are high level design decisions which impact multiple packages or source file. Do not document low level design decisions are class design, method design here.

The index format is of two types
1. `<relative document path>` : \{\{short 2-3 lines description of document content\}\}
2. \{\{decision description for short decisions\}\}

## How to use this file
- Remember ADRs are high level design decisions which impact multiple packages or source file. 
- Do not document low level design decisions like class design, method design here.

## ADR Index
- `./packagedesign.md` : Documents the package/module design and their dependencies in mermaid.js format.  
- ADR-001 : Python import package is `src/ca_agent/` while the distribution stays `ca-agent`.
- ADR-002 : Tests live under the existing `tests/` root using the AGENTS.md subfolder names.
- ADR-003 : Bronze is `raw_data/` in place, read-only; Phase 1 builds Silver under `data/silver/`.
- ADR-004 : Reuse fingerprints are computed per config section so a change invalidates only affected outputs.
- ADR-005 : Version identity is a per-run integer ordinal; active version is the highest sealed manifest, with no mutable pointer.
- ADR-006 : Nested archives recurse to depth 3 under size, member-count and compression-ratio caps.
- ADR-007 : Format detection is signature-first; a file extension is recorded as evidence only.
- ADR-008 : Owner-password-only PDFs are opened with an empty password rather than reported as locked.
- ADR-009 : pandas is excluded from the tabular read path to preserve leading-zero identifiers.
- ADR-010 : PyMuPDF is rejected as AGPL-3.0; pypdf, pdfplumber and pypdfium2 are used instead.
- ADR-011 : The vision route uses httpx directly so retry and failure paths are testable without network.
- ADR-012 : Archive member names are sanitised for the filesystem while the original name is kept as lineage.
- ADR-013 : The tabular reader passes bytes, not the source path, to openpyxl so its filename-extension check cannot override signature-first detection.
- ADR-014 : An unpacked desktop application inside a client folder is excluded as NON_DATA, detected by co-occurring program markers rather than by folder name.
- ADR-015 : Tally XML voucher exports get a dedicated route in a later step rather than being chunked as generic field-path text.

---

## ADR-001: Python package is `src/ca_agent/`, not `src/ca-agent/`
`AGENTS.md` specifies `src/ca-agent/`, but a hyphen is not a valid Python identifier, so that
directory could never be imported. The distribution name stays `ca-agent` (declared in
`pyproject.toml`) and the import package is `ca_agent`, which is the standard PEP 503
relationship between the two. **This is a deliberate deviation from `AGENTS.md`, recorded here
because an `AGENTS.md` edit was not authorised.**

## ADR-002: Tests live in `tests/`, not `test/`
`AGENTS.md` specifies `test/plans`, `test/unit`, `test/testdata`. The repository already
contained `tests/`, and the working rules say to follow existing project patterns before
introducing new ones. The `AGENTS.md` subfolder names are adopted inside the existing root:
`tests/unit`, `tests/integration`, `tests/plans`, `tests/testdata`. A second parallel `test/`
tree would be worse than either option.

## ADR-003: Bronze is the raw corpus in place; Phase 1 builds Silver
`raw_data/` **is** the Bronze layer. It is opened read-only and never copied, moved or written
to, which satisfies SPEC-01 req 8's prohibition on modifying source files at the strongest
possible level: no code path writes there at all. Silver (`data/silver/`) holds every derived
artifact. Gold (embeddings and FAISS) is a later phase.

## ADR-004: Reuse fingerprints are per config section, not global
SPEC-01 req 8 invalidates "the affected outputs" when configuration changes. A single global
config hash would invalidate all ~16,600 files whenever any unrelated setting moved — for
instance, changing a vision temperature would discard thousands of Parquet files. Each config
section is therefore fingerprinted independently and each output records only the sections that
produced it. Credentials, timeouts and retry counts are excluded because they do not change the
content of a successful output.

## ADR-005: Version identity is a run ordinal, and there is no mutable "latest" pointer
Each run allocates one integer ordinal at start-up; every version directory it publishes is
named from it. Two consequences: worker output directories are unique by construction so
parallel writers cannot collide, and version ordering is a total integer order requiring no
locks and no trust in the system clock. Active-version resolution scans for the highest *sealed*
manifest rather than reading a pointer file, because a pointer would itself be mutable prior
state and a torn-read hazard if a run died between publishing outputs and repointing. A crashed
run therefore leaves the previous manifest active, and its abandoned version directories are
ignored — never deleted, since req 8 prohibits output deletion.

## ADR-006: Nested archives recurse to depth 3 under explicit caps
SPEC-01 records this as an unresolved Open Decision. The approved policy is: recurse to a maximum
nesting depth of 3, bounded by a total expanded-size cap, a member-count cap, and a per-member
compression-ratio guard checked *while streaming* rather than trusting a declared size. Breaching
any cap produces an explicit processing record and format document, never a silent omission.
Rationale: Indian ITR and TDS downloads are routinely zip-within-zip, so depth 1 would lose real
client data, while unbounded recursion is a denial-of-service risk on an unvetted corpus.

## ADR-007: Format detection is signature-first; the extension is only evidence
Probing the real corpus disproved the assumption that extensions are reliable: 61 of 66 `.xlk`
files are OOXML, roughly 30 percent of sampled `.xls` files are OOXML or plain text, all 411
`.db` files are Windows `Thumbs.db` artifacts rather than Tally databases, and 28 files have no
extension at all. Routing therefore uses signature and container probing, and an
extension/content mismatch is recorded as evidence in the format document rather than treated as
a failure.

No magic-number *library* is used. The formats needing precise identification (ZIP and its
OOXML variants, OLE and its variants, PDF, the image formats, 7z, RAR, gzip, RTF) have
unambiguous signatures, and everything a library would additionally recognise still routes to
`NO_COMPATIBLE_READER`. A hand-written table in `readers/detection.py` is therefore fully
deterministic, testable, and one dependency lighter. Weak two-byte markers such as BMP are
validated against their header fields rather than trusted on the prefix alone.

Running detection over the full corpus then proved the approach twice over. **261 files named
`*.pdf` are actually Java serialized object streams** (magic `AC ED 00 05`) written by the
Income Tax e-filing utility; a PDF parser would have been handed all of them. They are recorded
as `JAVA_SERIALIZED` and never deserialised, since Java deserialisation is a code-execution
vector. It also exposed a bug: a compound file keeps its directory in a sector named by the
header, not at a fixed offset, so an initial prefix-scan implementation misclassified **175
genuine `.xls` and `.doc` files** as unknown. Detection now parses with `olefile` and keeps the
scan only as a fallback for damaged containers. Unknown files fell from 451 to 14 of 16,596.

## ADR-008: Owner-password-only PDFs are read, not reported as locked
Many ITR-V and TIS PDFs in the corpus carry an encryption dictionary but have an empty *user*
password; the owner password only restricts printing and copying. Treating any `/Encrypt` marker
as `locked` would discard hundreds of readable filings. The pipeline attempts an empty password
and, when that succeeds, processes the document normally while noting the owner restriction in
its format document. Only a genuine refusal is recorded as `PASSWORD_PROTECTED_FILE`. No password
is ever guessed, requested, or brute-forced.

## ADR-009: pandas is excluded from the tabular read path
pandas coerces `"0012345"` to the integer `12345`, which would silently destroy PAN, GSTIN, TAN
and bank-account identifiers across the entire corpus — exactly the fields a financial analysis
agent depends on. Spreadsheets are read cell-by-cell and written with `pyarrow` directly. A
column becomes a typed Arrow column only when every non-empty cell parses to one type *and*
round-trips back to its original text; otherwise it stays a string and the format document
records why inference was rejected. This directly implements SPEC-01 req 1's prohibition on
silently coercing invalid values.

## ADR-010: PyMuPDF is rejected on licensing grounds
PyMuPDF is the most capable Python PDF toolkit but is AGPL-3.0, which is incompatible with
proprietary software. PDF work therefore uses `pypdf` for structure and encryption, `pdfplumber`
for native text with layout, and `pypdfium2` (BSD/Apache) for rasterisation.

## ADR-011: The vision route uses httpx directly rather than the OpenAI SDK
An OpenAI-compatible endpoint is plain HTTP, and `httpx.MockTransport` allows the retry, backoff
and partial-page-failure paths to be tested deterministically with zero network access. The SDK
would add a dependency whose retry behaviour is harder to script in tests. An autouse test
fixture makes any real socket connection raise, so "no test touches the network" is enforced by
the harness rather than by reviewer discipline.

## ADR-012: Archive member names are sanitised, not rejected, while the original is kept as lineage
Extracting the real corpus surfaced a zip whose member name contains embedded newline characters
(`G D CONS
INV NO 297
10-03-2020.pdf`). Windows cannot create such a file, and an early
implementation both failed to write it and then crashed in its own cleanup, because `unlink` on an
unexpressible path raises too.

Rejecting these members would discard real client invoices, so the on-disk name is sanitised
(control characters and `< > : " | ? *` replaced, trailing dots and spaces stripped) and given a
short digest suffix so two different names cannot collide onto one path and silently overwrite each
other. **The original member name is always retained in the processing record as lineage**, so the
sanitised filename is a storage detail rather than a loss of information.

One case is still rejected outright rather than sanitised: a single letter followed by a colon
(`a:b.txt`). Windows parses that as a drive specifier, so sanitising the colon would mask a genuine
attempt to escape the extraction root. Cleanup after any failed write is now best-effort and can
never raise, because SPEC-01 req 7 requires that siblings continue.

## ADR-013: The tabular reader passes bytes, not the source path, to openpyxl
Converting the full corpus of ~3,000 tabular files surfaced a reader bug, not a data problem:
`openpyxl.load_workbook` validates the **filename extension** before it inspects any content, and
raises `InvalidFileException` for anything not already named `.xlsx`/`.xlsm`/`.xltx`/`.xltm` — even
a byte-for-byte valid OOXML workbook. Detection (ADR-007) already classifies files by signature
specifically because extensions lie at scale in this corpus (~18% of sampled `.xls` are genuine
OOXML zips saved under a stale name); calling `load_workbook(path, ...)` reintroduced the exact
extension dependency detection was built to remove, and it failed every one of those files with a
misleading "old .xls format" error. The fix reads the file once and hands openpyxl a `BytesIO`
buffer instead of the path — openpyxl's extension check only applies to path/string arguments, so
it never sees the misleading name. The formula-audit second pass (ADR-009's round-trip typing
needs it to find uncached formula cells) takes the same bytes, which also removes a second disk
read.

The same run surfaced two more corpus-specific exceptions escaping the existing `_EXCEPTION_MAP`
discipline: `lxml.etree.XMLSyntaxError` (a handful of GST-portal-exported workbooks declare an
`x15` namespace prefix on an element but never define it — this is a `SyntaxError` subclass, not
`ValueError`, so it was not caught) and `xlrd.compdoc.CompDocError` (a plain `Exception` subclass
xlrd's compound-document directory walker raises on a corrupt stream chain, distinct from the
`XLRDError`/`AssertionError` cases already handled). Both are now mapped to `CORRUPT_FILE`, the
same outcome the file would reach if it were genuinely unreadable — no different exception type
should be allowed to abort a 16,000-file batch over one damaged workbook.

## ADR-014: An unpacked desktop application inside a client folder is excluded as NON_DATA
Validating the text route over the corpus showed that **19 percent of all extractable text is not
client data**. One client folder, `Business Clients/SANDEEP KOTHAWALE/AY 2019-20/REVISED/ITR`,
contains an entire unpacked copy of the Income Tax e-filing utility: jQuery, CSS, a Java
keystore, a public-key certificate, the utility's own HTML pages, and its bundled reference
data — `ISIN_LIST.properties` (3.3 MB, every ISIN on the exchange) and `IFSC.txt` (1.7 MB, every
bank IFSC code in India). Chunking these would produce roughly 5,500 chunks of pure noise, cost
real money to embed in the Gold layer, and actively degrade retrieval: a question about a
client's income would compete against thousands of ISIN and IFSC chunks. Its 18 bundled icons
would additionally have been sent to the paid vision route.

The exclusion is by **co-occurring program markers, never by folder name**. A directory is an
application bundle when it is the parent of a directory holding a Java `.keystore` *and* that
same subtree also contains a `.properties` file. Both conditions are needed: clients really do
keep their genuine filings in folders called `ITR`, and they really do hold `.cer` and `.pfx`
digital-signature files, so neither the name nor a lone certificate can be the signal. Over the
full corpus the rule finds exactly one directory, 62 files, 15.8 MB, with no false positives.

`catalog/bundles.py` lives in L2 because directory structure is tree knowledge and the catalog
is the only layer that walks the tree; `select_route` is a pure function of one `FormatProbe`
and could not make this decision without becoming path-aware. **Every excluded file still gets a
processing record and a format document** — only its route changes to `NON_DATA`, so SPEC-01
req 6's "no silent skips" guarantee is untouched.

## ADR-015: Tally XML voucher exports get a dedicated route, in a later step
Validating the structured route over the corpus showed that **six Tally XML exports from a
single client produce about 92 percent of all structured text** — roughly 370,000 of 403,672
chunks. Reading one explains why. Each `<VOUCHER>` carries a handful of fields anyone would
want — `DATE`, `VOUCHERNUMBER`, `VOUCHERTYPENAME`, `PARTYLEDGERNAME`, `NARRATION` and its
ledger amounts — wrapped in dozens of Tally's own serialisation flags: `ISMSTFROMSYNC`,
`ASORIGINAL`, `AUDITED`, `FORJOBCOSTING`, `ISOPTIONAL`, `USEFOREXCISE`, `ALLOWCONSUMPTION`,
`OLDAUDITENTRYIDS` and more, repeated verbatim for every voucher in the file. The bulk of a
47 MB "bank statement" is therefore Tally's file format, not the client's transactions.

The generic structured reader is behaving correctly: a voucher array is nested well past the
depth cap and fails the scalar-value ratio, so it is properly refused as a flat collection and
falls to field-path text. The defect is in the *representation*, not the routing — this is
transactional data that belongs in a table.

The decision is a **dedicated Tally route**, recognising the `ENVELOPE / BODY / IMPORTDATA`
voucher shape and flattening each voucher to one row (date, number, type, party, narration,
amount) with its ledger entries as child rows. That turns roughly 370,000 near-useless chunks
into two clean, queryable tables per export. It is enough work to be **its own step rather than
an addition to step 8**, so step 8 ships as validated and the Tally route is scheduled after
the remaining Phase 1 steps. Until it lands these files still produce complete, honest output;
it is merely verbose.
