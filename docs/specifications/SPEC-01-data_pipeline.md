# Specification for Data Pipeline - Data

This application processes raw data from `raw_data/`, organized into independent category/client directories.

## Data Pipeline Requirements

The pipeline reads client files from `raw_data/` and follows the bronze, silver, and gold architecture. Preserve original source data, retain client/category/source-path lineage, and make validated derived data available to agents.

### 1. Tabular conversion to Parquet

- Convert supported tabular formats to Parquet, including CSV, TSV, XLS, XLSX, XLSM, and XLSB. Every other format must also enter reader selection and extraction as described in requirement 6; files must not be silently skipped.
- Process every populated worksheet or distinct table into a separate Parquet output. Record empty sheets and any tables that cannot be converted.
- Preserve column names, values, and appropriate data types, including leading zeros in identifiers. Document type inference, mixed types, missing values, and conversion errors; do not silently discard rows or coerce invalid values.
- Read stored spreadsheet values without executing macros. Report formula cells with unavailable cached values.
- Generate a per-source Markdown format document describing all tables and worksheets and linking to their Parquet outputs.

### 2. Text extraction, embeddings, and FAISS

- Extract text from supported documents, including PDF, DOCX, DOC, TXT, RTF, Markdown, and HTML, using appropriate readers.
- Split eligible extracted text into traceable chunks, generate text embeddings, and persist vectors in a separate FAISS index for each category/client folder. Matching client names in different categories must not share an index. Publish new immutable index versions rather than modifying existing index artifacts.
- Persist chunk text and metadata alongside the index, including client, source path, page/section where available, chunk identifier, embedding model, and vector dimension. Maintain the mapping between vectors and source chunks when saving and loading.
- Fully text-based PDFs retain the text extraction, embeddings, and FAISS route. Scanned and mixed native-text/scanned PDFs follow requirement 3 and produce combined Markdown instead of embeddings. Record content categories and page references for every PDF; do not embed empty text.
- Record embedded tables and images in other document formats where applicable. Distinguish content detection from successful extraction.
- Generate a Markdown format document for each source document describing observed structure, extraction status, limitations, and references to derived text and index metadata.

### 3. Separate Python script for image extraction

- Implement a separate Python script for images such as JPG, JPEG, PNG, BMP, GIF, TIFF, and TIF. Preprocess formats where required by the API. Reader selection and extraction failures must follow requirement 6.
- Call a vision-capable, OpenAI-compatible API with configurable endpoint, model, and credentials.
- Request structured information, including visible text, fields, values, and tables, and save the received results as Markdown. Preserve source/client identification and mark uncertain or unreadable values explicitly.
- Include image-format information in the same Markdown: format, dimensions, frame/page count where available, detected content categories, model used, processing status, and extraction limitations. This output also serves as the per-file format document; no duplicate Markdown is required.
- Record API and response-processing errors per file and continue processing other files. Do not report a failed extraction as successful.

- Scanned PDFs and mixed native-text/scanned PDFs must use the vision-processing route. Produce one combined Markdown document per PDF, preserving page order and page references, combining extracted information from all its images with any available native text, and avoiding duplicate text.
- Keep extracted tables as Markdown tables in that combined document. Do not convert these PDF tables to Parquet or automatically embed any of the combined PDF output, including its native-text portions.
- Include the PDF's format information in its combined Markdown document. This fulfills its per-file format-documentation requirement.
- If some pages fail, preserve successful page content, mark each failed page explicitly, and record overall status `partial`. Preserve this version as history but exclude it from active retrieval until all pages complete successfully. A later successful retry creates a new version.

### 4. Per-file Markdown data-format documentation

**Generate a Markdown document for every source file handled by these processing routes. Describe that individual file's observed data format; a generic document for each extension is not sufficient.**

- Include the relative source path, independent category/client scope, extension, detected format where available, content hash, processing/output version, processing status, and derived output references. Archive members must also retain their originating archive and member paths.
- Preserve the source-relative directory structure and full filename when naming companion documents, for example `report.xlsx.format.md` and `report.pdf.format.md`, to avoid collisions across clients, folders, and extensions.
- Place companion documents in immutable version-specific output directories so reruns never overwrite them. Duplicates retain individual source-path records and references to shared content outputs within the same client scope.
- Store generated format documents with pipeline outputs, separately from the source-map inventory. Source maps list names and paths; format documents record structure observed during processing.
- Record unknown or inaccessible information explicitly. Do not invent schemas, column meanings, or content classifications.

| Source type | Required Markdown format information |
| --- | --- |
| Tabular files | Table/worksheet names and locations, row and column counts, column names and types, missing-value information, available descriptions and units, header/delimiter/encoding details where relevant, conversion warnings, and Parquet output paths. Distinguish inferred types or descriptions from source-provided information. |
| PDF | Page count where available; presence of text, images, and tables; page locations and counts where detectable; scanned/image-only classification; extraction status and limitations. |
| DOCX, DOC, TXT, RTF, Markdown, HTML | Text sections/headings, tables and images where applicable, table columns/types when determinable, encoding where relevant, extraction status, and limitations. |
| Images | Include format and content information in the structured extraction Markdown produced by the image script, as described in requirement 3. |
| JSON/XML | Object/element hierarchy, field paths, nested collections, inferred types, routing decisions, and Parquet/text output references. |
| Archives | Archive identity, member paths, extraction outcomes, duplicate relationships, and references to member processing records. |
| Other formats | Reader selected, observed structure where extractable, output references, and explicit extraction exceptions or `NO_COMPATIBLE_READER` failure. |
| Locked or failed files | Source identification, processing stage, status, error category/message, and retry action. Mark uninspectable structure as unknown. |

### 5. Password protection and exceptions

- Catch password-protection/encryption exceptions in every applicable processing route. Record status `locked`, error category `PASSWORD_PROTECTED_FILE`, source path, processing stage, and a useful message.
- Skip the locked file, write its Markdown status document, and continue processing other files. Do not request passwords or attempt automatic unlocking.
- The user may manually unlock and replace the source. This is an external action; the pipeline must never modify the source. A subsequent run retries the replacement and creates new immutable outputs and status records without duplicating active results.
- Keep password-protection errors distinct from unsupported formats, corrupt files, extraction/conversion errors, and API failures. Do not classify every reader exception as a locked file.
- Do not publish incomplete or failed outputs as successful Parquet datasets, indexed text, or extracted image data.

### 6. All-format coverage and exception recording

- Include every discovered file in processing, including JSON, XML, archives, native Tally-related files, and other formats outside the initial reader list. Do not exclude files solely because their extensions are not listed above.
- Use available appropriate Python libraries to attempt data extraction. Record any reader/extraction exception with source identity, processing stage, reader, error category/message, and status; continue processing other files.
- Where no compatible reader exists, record an explicit `NO_COMPATIBLE_READER` failure and a Markdown status document. This is a recorded processing failure, never a silent skip or a claim that extraction succeeded.
- Reader selection does not imply successful support for every format. Document extraction limitations and preserve unknown structure as unknown. Reading a file does not authorize executing its contents.
- Route JSON/XML by observed structure: clearly tabular collections become Parquet datasets; document-like text follows chunking, embeddings, and the client-specific FAISS route. Preserve nested structure and field paths in the per-source Markdown document and trace each derived output to its collection or field path.

### 7. ZIP extraction and client-scoped deduplication

- Extract all files from ZIP archives into a separate output area without modifying the archive or original source tree. Record per-member extraction failures, including locked members, and continue processing remaining members where possible.
- Retain archive identity and each member path as lineage. Extracted files enter the same reader-selection, format-documentation, and exception-handling process as ordinary source files.
- Before content conversion, compute SHA-256 content hashes and check duplicates across ordinary files and extracted members within the same category/client folder.
- Process identical content once per client scope and applicable processing configuration, retaining every source path and duplicate relationship. Same-named files with different content must both be processed. Deduplication must not delete or alter originals or extracted artifacts.
- Every category/client folder is independent, even when a client name matches a folder in another category. Never merge scopes or deduplicate across categories. The Mauli Hospital backup root remains its single client scope as identified in the source-map index.

### 8. Incremental processing and immutable versions

- The pipeline must never modify, overwrite, or delete source files or previously generated outputs. Treat generated Parquet, Markdown, extracted files, text/chunk metadata, FAISS indexes, and processing records as immutable artifacts.
- On reruns, reuse successful outputs for unchanged content under unchanged relevant processing settings. Process new or changed content and retry prior failures or partial results. Changes to extraction configuration or embedding models invalidate reuse of the affected outputs.
- Reprocessing creates new output versions and new processing/status records. Preserve all previous versions and history; never update prior artifacts in place.
- Active retrieval resolves to the latest successful version for the relevant source and processing configuration. Keep older versions available as history. Failed or partial attempts must not replace a successful active version; incompatible embedding models/dimensions must not be mixed in an index.
- Track active-version selection through newly published versioned manifests/index snapshots, preserving earlier snapshots. Shared outputs for duplicates retain all associated source references without duplicate active content.
- Source deletion by the pipeline and output cleanup/deletion are prohibited. Manual unlocking/replacement by the user is handled as a new source version, not as permission for pipeline mutation.

## Open Decision

Recursive extraction of nested archives, maximum nesting depth, and expanded-size limits were raised during the interview but were not confirmed. This specification does not establish a recursive nested-archive policy. Nested archives still require a processing record under the all-format coverage rule; no silent omission is permitted.

## Acceptance Criteria

1. CSV and multi-sheet Excel inputs produce readable Parquet outputs covering their tables, with per-source Markdown listing columns, types, counts, and output references.
2. Eligible textual inputs, including fully text-based PDFs, produce non-empty chunks, persisted client-specific FAISS vectors, and source-traceable metadata, plus format Markdown.
3. Scanned and mixed PDFs produce one combined Markdown document per PDF in page order, with native text and vision-extracted content. Tables remain Markdown tables; no Parquet or automatic embeddings are generated for these PDFs.
4. The image script saves structured extraction and image-format information in Markdown. API or page failures are explicitly documented. A partially processed PDF retains successful content and failed-page markers but remains outside active retrieval until complete.
5. A locked file produces an exception record and Markdown status document while other files continue. After manual unlocking/replacement, rerunning creates a new immutable successful version without duplicate active outputs.
6. JSON/XML collections route to Parquet when tabular and to embeddings when document-like; Markdown records nested structure and field lineage.
7. ZIP members are extracted and checked against both ordinary files and other extracted members. Identical content within one client scope is processed once with all paths retained; same-named different content is processed separately.
8. Matching client folders in different categories have independent deduplication scopes and FAISS indexes. Identical bytes across those scopes do not cause cross-client reuse.
9. Every discovered file has a processing record. An extraction exception or missing reader produces an explicit failure record and Markdown status document, while other files continue. Unavailable structure is marked unknown.
10. Unchanged successful files reuse their outputs on reruns. New/changed files, relevant configuration/model changes, and previous failures trigger the applicable processing or retry.
11. Reruns do not modify or delete any existing source or output artifact. They create new versions where necessary; retrieval selects the latest successful applicable version and preserves prior versions as history.
12. Same-named files in different source locations and repeated processing attempts have distinct lineage and versioned output paths, while duplicates within a client reference shared content outputs.

## Raw Data Source Map

The complete raw-data inventory is maintained in the [raw-data source-map index](../raw_data_docs/index_sourcemap.md) and its 94 linked client-specific Markdown documents. The index includes category totals, file-type totals, client source paths and counts, and root/category directory entries.

**Never read all source-map documents at once. Read the index first, then only the client source maps relevant to the current task.**

The inventory dated 2026-09-09 covers 16,596 files and 3,305 subdirectories across 15 top-level directories. It records only directory names, filenames, and extensions; raw-file and archive contents were not inspected.
