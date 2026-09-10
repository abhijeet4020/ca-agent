# TP-01 — Silver Layer Test Plan (SPEC-01, Phase 1)

Test descriptions follow the Thinking Craftsman Test Description template. Each entry becomes a
pytest function of the same name. Implementation order is test-first: an entry is written here,
then as a failing test, then the code.

Traceability to SPEC-01 acceptance criteria is in the table at the end.

---

## Group A — Domain model and client scoping

## Test: test_scope_id_is_stable_and_distinct_per_category
## Description
ClientScope.scope_id must identify a (category, client) pair, never a client name alone. Two
scopes sharing a client name across different categories must not collide, because SPEC-01
requirement 7 forbids merging scopes.
## Inputs
("Cooperative Audits", "LIC EMPLOYEES CO OP CREDIT SOCIETY SATARA") and
("GST Proprietor", "LIC EMPLOYEES CO OP CREDIT SOCIETY SATARA").
## Expected Output
Two different scope_id values; each is byte-identical across repeated construction.

## Test: test_scope_resolver_maps_client_directory_to_scope
## Description
Longest-prefix resolution assigns a file under category/client to that client scope.
## Inputs
Scope table containing "Business Clients/AMIT SURYAKANT DHAMAL"; path
"Business Clients/AMIT SURYAKANT DHAMAL/AY 18-19/ITRV.pdf".
## Expected Output
Scope resolves with category "Business Clients", client "AMIT SURYAKANT DHAMAL".

## Test: test_mauli_hospital_backup_root_is_one_client_scope
## Description
The Mauli Hospital backup directory has no client level; the category directory is itself the
single client scope. An extractor keyed on the second path component would be wrong here.
## Inputs
Scope table entry flagged category_is_scope; path
"Mauli Hospital Tally Back up/Data/10001/Company.900".
## Expected Output
One scope denoting the Mauli backup root; no per-subdirectory scopes.

## Test: test_unresolvable_path_is_recorded_not_guessed
## Description
A path matching no registered scope must produce an explicit UNSCOPED_PATH outcome. SPEC-01
forbids inventing classifications.
## Inputs
Path "Unknown Category/stray.pdf" against a scope table not containing it.
## Expected Output
Resolution returns no scope and yields the UNSCOPED_PATH error category; no exception escapes.

---

## Group B — Configuration and reuse fingerprinting

## Test: test_section_fingerprint_is_stable_across_key_order_and_defaults
## Description
Requirement 8 makes the config fingerprint part of the correctness contract. It must not change
when keys are reordered or when a default is written out explicitly.
## Inputs
Two config mappings differing only in key order, and a third omitting a key equal to its default.
## Expected Output
All three produce an identical tabular section fingerprint.

## Test: test_changing_one_section_does_not_change_other_section_fingerprints
## Description
Requirement 8 invalidates only affected outputs, so fingerprints are per section. Changing a
vision setting must not invalidate thousands of Parquet files.
## Inputs
Baseline config, and a copy with the vision temperature altered.
## Expected Output
Vision fingerprint differs; tabular, text, chunking and archive fingerprints are unchanged.

## Test: test_api_key_is_excluded_from_fingerprint_and_never_serialised
## Description
Rotating a credential must not invalidate outputs, and the secret must never reach disk.
## Inputs
Two configs identical but for the vision API key.
## Expected Output
Identical vision fingerprints; the canonical JSON contains no key material.

## Test: test_missing_api_key_raises_at_startup_not_at_first_call
## Description
Fail fast. A run over 16,000 files must not discover a missing credential hours in.
## Inputs
Config with the vision route enabled and no API key present.
## Expected Output
A configuration error is raised during load, naming the missing environment variable.

---

## Group C — Content hashing and client-scoped deduplication

## Test: test_unpacked_application_bundle_is_detected_from_its_markers
## Description
A client folder in the corpus holds an entire unpacked copy of the Income Tax e-filing
utility - jQuery, a Java keystore, CSS, the utility's own HTML. Its bundled reference data
(every ISIN, every bank IFSC code) is 19 percent of all extractable text in the corpus and is
not client data at all. Detected by two co-occurring Java-application markers rather than by
folder name, so a client folder that happens to be called ITR is not caught.
## Inputs
A directory tree containing Config/app.keystore and a .properties file.
## Expected Output
The directory holding Config is reported as a bundle root.

## Test: test_client_folder_named_like_a_utility_is_not_treated_as_a_bundle
## Description
Guards the rule against the obvious false positive; clients really do have ITR folders.
## Inputs
A folder named ITR containing only client PDFs and spreadsheets.
## Expected Output
No bundle is reported.

## Test: test_digital_signature_files_alone_do_not_make_a_bundle
## Description
Clients legitimately hold .cer and .pfx digital-signature files, so only a Java keystore
counts as a program marker.
## Inputs
A client folder containing a .cer and a .pfx file.
## Expected Output
No bundle is reported.

## Test: test_every_file_under_a_bundle_is_excluded_including_images
## Description
Exclusion covers the whole subtree, not just the text files: the bundle's 18 icons would
otherwise be sent to the paid vision route.
## Inputs
A bundle containing text, an image and an archive.
## Expected Output
All three are reported as inside the bundle.

## Test: test_identical_bytes_in_two_categories_do_not_deduplicate
## Description
Requirement 7: every category/client folder is an independent dedup scope.
## Inputs
The same bytes at "Business Clients/ACME/x.csv" and "LLP/ACME/x.csv".
## Expected Output
Two distinct work items, two content entries, neither marked a duplicate of the other.

## Test: test_duplicate_within_client_shares_output_and_retains_both_source_paths
## Description
Requirement 7: identical content is processed once per client scope, but every source path is
retained.
## Inputs
The same bytes at two different paths inside one client scope.
## Expected Output
One content entry; both source paths recorded against it; the second is skipped_duplicate.

## Test: test_same_name_different_content_within_client_processed_separately
## Description
Requirement 7 states same-named files with different content must both be processed.
## Inputs
Files named FS.xlsx with differing bytes in two subdirectories of one client.
## Expected Output
Two distinct content hashes and two independent work items.

## Test: test_content_hash_is_streamed_and_matches_hashlib
## Description
Hashing must not load a whole file into memory and must be exact.
## Inputs
A file larger than the read block size.
## Expected Output
Digest equals the hashlib sha256 digest of the same bytes.

---

## Group D — Immutability and versioning (requirement 8)

## Test: test_rerun_does_not_mutate_any_prior_version_artifact
## Description
The central durability guarantee: a rerun may add, never alter. Compared by content hash because
Windows modification times and ACLs are unreliable.
## Inputs
A snapshot of relative path to sha256 across the whole output root, then an identical rerun.
## Expected Output
Every pre-existing path is still present and byte-identical.

## Test: test_active_version_resolves_to_newest_successful_version
## Description
Retrieval selects the latest successful version for a source and configuration.
## Inputs
Three sealed manifests with ordinals 1, 2 and 3, where 3 is successful.
## Expected Output
Resolution returns version 3; versions 1 and 2 remain readable as history.

## Test: test_partial_version_retained_in_history_but_absent_from_active_manifest
## Description
Requirement 3: a partially processed PDF is preserved as history but excluded from active
retrieval until a fully successful retry.
## Inputs
A successful version 1, then a version 2 with status partial.
## Expected Output
Active resolution still returns version 1; version 2 exists on disk and is listed as history.

## Test: test_failed_retry_does_not_displace_successful_active_version
## Description
Requirement 8: failed attempts must not replace a successful active version.
## Inputs
Successful version 1, then a failed version 2.
## Expected Output
Active resolution returns version 1.

## Test: test_unchanged_content_and_fingerprint_skips_reader
## Description
Requirement 8 reuse, asserted by observing the reader was never invoked, never by timing.
## Inputs
A completed run, then an identical rerun with a mock reader injected.
## Expected Output
Reader mock call count is zero; the work item reports reuse.

## Test: test_config_change_invalidates_only_affected_route_outputs
## Description
Requirement 8: a change to extraction configuration invalidates the affected outputs only.
## Inputs
A completed run, then a rerun with the vision section changed.
## Expected Output
Vision work reprocesses; tabular and text work reuses.

## Test: test_abandoned_version_directory_without_record_is_ignored_not_deleted
## Description
A crash leaves a version directory with no record file. It must be invisible to resolution and
must never be deleted, because requirement 8 prohibits output deletion.
## Inputs
A version directory containing outputs but no record file.
## Expected Output
Resolution ignores it; the directory still exists after a subsequent run.

---

## Group E — Format detection and routing

## Test: test_detection_prefers_signature_over_extension
## Description
Probing the real corpus showed roughly 30 percent of xls files are actually OOXML or plain text,
and 61 of 66 xlk files are OOXML. Routing on extension would corrupt this bucket.
## Inputs
OOXML bytes named report.xls; JSON bytes named data.xlsx; a plain-text file named x.xls.
## Expected Output
Detected formats are xlsx, json and text respectively; the extension conflict is recorded as
evidence, not as a failure.

## Test: test_thumbs_db_is_classified_non_data
## Description
All 411 db files in the corpus are Thumbs.db shell artifacts, not Tally databases.
## Inputs
A file named Thumbs.db.
## Expected Output
Route NON_DATA; a processing record is still produced.

## Test: test_excel_owner_lock_stub_is_classified_non_data
## Description
Excel owner-lock stubs prefixed with a tilde and dollar sign are not documents.
## Inputs
A file whose name begins with the Excel lock prefix and ends in .xlsx.
## Expected Output
Route NON_DATA with a processing record.

## Test: test_extensionless_pdf_is_routed_by_signature
## Description
28 corpus files have no extension; 12 of them are PDFs.
## Inputs
PDF bytes in a file named "document".
## Expected Output
Routed to the PDF classifier.

## Test: test_tally_binary_records_no_compatible_reader
## Description
Requirement 6: where no compatible reader exists, record an explicit failure and a format
document. Never a silent skip, never a claim of success.
## Inputs
A .tsf file of opaque bytes.
## Expected Output
Status no_reader, error category NO_COMPATIBLE_READER, format document written.

## Test: test_executable_is_recorded_but_never_executed
## Description
Requirement 6: reading a file does not authorize executing its contents.
## Inputs
An .exe file.
## Expected Output
Status excluded_non_data, category EXECUTABLE_NOT_PROCESSED; no subprocess is spawned.

---

## Group F — Locked files and the error taxonomy (requirement 5)

## Test: test_owner_password_pdf_is_extracted_not_marked_locked
## Description
Many ITR-V and TIS PDFs carry an encryption dictionary but are owner-password-only and open with
an empty user password. Treating them as locked would discard hundreds of readable filings.
## Inputs
A PDF encrypted with an owner password and an empty user password.
## Expected Output
Text is extracted, status success, and the owner restriction is noted in the format document.

## Test: test_user_password_pdf_is_marked_locked_with_status_document
## Description
Requirement 5: a genuinely locked file is skipped, recorded, and processing continues.
## Inputs
A PDF with a non-empty user password.
## Expected Output
Status locked, category PASSWORD_PROTECTED_FILE, format document written, no password requested.

## Test: test_encrypted_zip_member_is_recorded_and_siblings_continue
## Description
Requirement 7: per-member extraction failures are recorded and remaining members still process.
## Inputs
A zip with one AES-encrypted member and two readable members.
## Expected Output
One ARCHIVE_MEMBER_LOCKED record; two members extracted successfully.

## Test: test_corrupt_file_is_not_classified_as_locked
## Description
Requirement 5 insists password errors stay distinct from corruption.
## Inputs
Truncated zip bytes and a truncated OLE file.
## Expected Output
Category CORRUPT_FILE, not PASSWORD_PROTECTED_FILE.

## Test: test_locked_file_replaced_by_user_produces_new_successful_version
## Description
Requirement 5: the user may unlock and replace a source externally; a later run creates a new
immutable successful version without duplicating active outputs.
## Inputs
Run 1 over a locked file; the file is replaced with an unlocked copy; run 2.
## Expected Output
Run 2 produces a successful new version; the run 1 locked record is preserved as history.

---

## Group G — Archives (requirement 7)

## Test: test_nested_zip_beyond_max_depth_records_explicit_failure_not_silent_skip
## Description
The depth-3 policy resolves the specification Open Decision. Exceeding it is a recorded outcome.
## Inputs
A zip nested four levels deep with the maximum depth configured to 3.
## Expected Output
Levels 1 to 3 extract; level 4 yields ARCHIVE_DEPTH_LIMIT_EXCEEDED with a format document.

## Test: test_zip_bomb_ratio_guard_aborts_mid_stream
## Description
Declared member sizes cannot be trusted, so the expansion ratio is checked while streaming.
## Inputs
A member whose expansion ratio exceeds the configured limit.
## Expected Output
Extraction aborts with ARCHIVE_SIZE_LIMIT_EXCEEDED; no oversized file is left published.

## Test: test_unsafe_member_path_is_rejected
## Description
Path traversal must never escape the extraction area.
## Inputs
Members using parent-directory traversal, an absolute drive path, and a Windows reserved name.
## Expected Output
Each yields UNSAFE_MEMBER_PATH; nothing is written outside the extraction root.

## Test: test_archive_member_retains_archive_lineage
## Description
Requirement 7: archive identity and member path are retained as lineage.
## Inputs
A zip containing sub/report.xlsx.
## Expected Output
The member record carries the archive content hash, the member path, and the nesting depth.

---

## Group H — Tabular to Parquet (requirement 1)

## Test: test_leading_zero_identifier_column_stays_string_in_parquet
## Description
PAN, GSTIN and account numbers must survive. This is why pandas is excluded from the read path.
## Inputs
A sheet column containing 0012345 and 0000001 as text.
## Expected Output
Parquet column type is string; values round-trip exactly.

## Test: test_mixed_type_column_is_kept_as_string_and_documented
## Description
Requirement 1 forbids silently coercing invalid values.
## Inputs
A column mixing an integer, a word, and a date.
## Expected Output
Column stays string; the format document records rejected type inference with a reason.

## Test: test_formula_audit_can_be_disabled_for_faster_batch_runs
## Description
The uncached-formula audit reopens and re-walks the whole workbook a second time; a batch run
over thousands of files must be able to trade that warning for speed.
## Inputs
A workbook with an uncached formula cell, converted with audit_uncached_formulas=False.
## Expected Output
Conversion succeeds with no FORMULA_NO_CACHED_VALUE warning and no second workbook read.

## Test: test_every_populated_worksheet_becomes_its_own_parquet
## Description
Requirement 1: each populated worksheet or distinct table is a separate Parquet output.
## Inputs
A workbook with three populated sheets and one empty sheet.
## Expected Output
Three Parquet files; the empty sheet is recorded with no Parquet output.

## Test: test_formula_cell_without_cached_value_is_reported
## Description
Requirement 1 requires reporting formula cells whose cached value is unavailable.
## Inputs
A workbook with a formula cell and no cached result.
## Expected Output
Warning FORMULA_NO_CACHED_VALUE naming sheet and cell; no row is dropped.

## Test: test_no_rows_are_dropped_on_conversion_error
## Description
Requirement 1: do not silently discard rows.
## Inputs
A sheet with one unparseable cell.
## Expected Output
Parquet row count equals source row count; the offending cell is preserved as raw text.

## Test: test_ooxml_namespace_syntax_error_is_recorded_not_propagated
## Description
Corpus regression: some GST-portal-exported workbooks declare an undefined MS-extension
namespace prefix; lxml's XMLSyntaxError is not a ValueError subclass and escaped uncaught.
## Inputs
An OOXML load that raises lxml.etree.XMLSyntaxError.
## Expected Output
A CORRUPT_FILE failure is returned, never raised; the batch continues.

## Test: test_xlrd_compdoc_error_is_recorded_not_propagated
## Description
Corpus regression: xlrd's compound-document walker raises CompDocError, a plain Exception
subclass distinct from XLRDError, on a workbook with a corrupt stream chain.
## Inputs
A BIFF load that raises xlrd.compdoc.CompDocError.
## Expected Output
A CORRUPT_FILE failure is returned, never raised; the batch continues.

## Test: test_ooxml_content_with_a_misleading_xls_extension_is_still_read
## Description
Corpus regression: openpyxl's load_workbook validates the filename extension before touching
content and refuses a real OOXML workbook saved under a stale .xls/.xlk name, defeating the
signature-first detection SPEC-01 requires. The reader must read from bytes, not the path.
## Inputs
A genuine OOXML workbook payload written to a file named "misnamed.xls".
## Expected Output
Conversion succeeds; no CORRUPT_FILE failure from openpyxl's own extension check.

---

## Group I — PDF classification and vision (requirements 2 and 3)

## Test: test_text_page_is_classified_from_its_character_count
## Description
The text-versus-scanned split decides whether a page costs a paid vision call, so it is
decided from measured content rather than from the file's shape.
## Inputs
A page carrying more than the configured minimum of native characters.
## Expected Output
Page kind TEXT, with the measured character count recorded.

## Test: test_image_only_page_is_classified_scanned
## Inputs
A page with no text layer and an image covering most of it.
## Expected Output
Page kind SCANNED, with the measured image coverage recorded.

## Test: test_blank_page_is_classified_empty_and_never_embedded
## Description
Requirement 2 forbids embedding empty text; an empty page is recorded, not dropped.
## Inputs
A page with no text and no image.
## Expected Output
Page kind EMPTY, recorded, and contributing no text unit.

## Test: test_page_with_little_text_over_an_image_is_classified_mixed
## Description
A stamped or signed scan carries a few characters over a full-page image and must not be
mistaken for a text page.
## Inputs
A page with an image covering most of it and a handful of characters.
## Expected Output
Page kind MIXED.

## Test: test_classification_thresholds_come_from_configuration
## Description
Requirement 8: the thresholds are stamped into the reuse fingerprint, so they must be read
from settings rather than hard-coded.
## Inputs
The same PDF classified under two different minimum-character settings.
## Expected Output
The page kind changes with the setting.

## Test: test_document_with_every_page_text_does_not_require_vision
## Inputs
A three-page PDF with a native text layer throughout.
## Expected Output
requires_vision is false and native text is extracted for every page.

## Test: test_document_with_any_scanned_page_requires_vision
## Description
Requirement 3: one scanned page makes the whole document a vision document.
## Inputs
A three-page PDF with two text pages and one scanned page.
## Expected Output
requires_vision is true, and the text pages still carry their native text so no vision call
is wasted on them.

## Test: test_owner_password_only_pdf_is_extracted_not_marked_locked
## Description
ADR-008: hundreds of ITR-V and TIS filings carry an encryption dictionary but open with an
empty user password. Treating the marker as "locked" would discard the most valuable filings
in the corpus.
## Inputs
A PDF encrypted with an owner password and an empty user password.
## Expected Output
Text is extracted, the owner restriction is recorded, and the status is not locked.

## Test: test_supplied_password_opens_an_otherwise_locked_pdf
## Description
ADR-008 amendment. 352 corpus PDFs are genuinely locked and 213 of them are AIS/TIS filings,
whose password the Income Tax portal derives from the client's own PAN and date of birth. The
firm holds those values, so supplying them is not guessing.
## Inputs
A PDF locked with a known password, and that password supplied by the caller.
## Expected Output
Text is extracted and the document is not reported locked.

## Test: test_ais_password_is_derived_from_the_supplied_pan_and_date_of_birth
## Description
The portal's documented scheme is lowercase PAN followed by DDMMYYYY.
## Inputs
A credential entry with PAN ABCPD7033X and date of birth 1985-04-12.
## Expected Output
The candidate password abcpd7033x12041985 is offered.

## Test: test_a_wrong_supplied_password_still_reports_locked
## Description
A supplied credential must not turn a failure into a false success.
## Inputs
A locked PDF and a password that does not open it.
## Expected Output
Status locked with PASSWORD_PROTECTED_FILE.

## Test: test_no_password_is_ever_written_to_a_record_or_message
## Description
A password reaching a processing record or log would leak a client credential into an output
that is committed and shared.
## Inputs
A locked PDF with a supplied password that fails.
## Expected Output
Neither the password nor the PAN appears anywhere in the resulting error.

## Test: test_credentials_are_resolved_per_client_scope
## Description
Requirement 7: scopes are independent, so one client's credential must never be tried against
another client's document.
## Inputs
A credential store holding entries for two different client scopes.
## Expected Output
Each scope resolves only to its own candidate passwords.

## Test: test_a_missing_credential_file_is_not_an_error
## Description
The credential file is optional; a firm that supplies none must still get a complete run.
## Inputs
A configuration pointing at no credential file.
## Expected Output
An empty store, and locked files simply stay locked.

## Test: test_genuinely_locked_pdf_is_recorded_as_password_protected
## Inputs
A PDF whose user password is not empty.
## Expected Output
Status locked with PASSWORD_PROTECTED_FILE; no password is guessed.

## Test: test_page_that_fails_extraction_is_marked_and_the_rest_survive
## Description
Requirement 3: preserve the pages that worked, mark the one that did not, and report the
document as partial rather than failed.
## Inputs
A three-page PDF where page 2 raises during text extraction.
## Expected Output
Pages 1 and 3 carry their text, page 2 carries a failure, and the document status is partial.

## Test: test_corrupt_pdf_is_reported_not_raised
## Inputs
Bytes that begin with a PDF header but are not a valid document.
## Expected Output
A CORRUPT_FILE failure is returned, never raised.

## Test: test_page_units_are_numbered_in_page_order
## Description
Requirement 3 requires page order to be preserved, and a chunk citing "page 7" must mean it.
## Inputs
A four-page text PDF.
## Expected Output
Units are PAGE units whose refs run page:1 to page:4 in order.

## Test: test_fully_text_pdf_routes_to_text_extraction
## Inputs
A PDF whose every page carries a native text layer.
## Expected Output
Route PDF_TEXT; chunks produced; no vision call.

## Test: test_image_only_pdf_routes_to_vision
## Inputs
A PDF built from page images with no text layer.
## Expected Output
Route PDF_VISION; one combined Markdown; no Parquet and no chunks.

## Test: test_mixed_pdf_emits_single_combined_markdown_in_page_order
## Description
Requirement 3: one combined document per PDF, page order preserved, native text merged, no
duplication.
## Inputs
A three-page PDF with native text on page 1 and scanned pages 2 and 3.
## Expected Output
One Markdown; page 1 from native text with no vision call; pages 2 and 3 from vision; page
references present and ordered.

## Test: test_scanned_pdf_with_failing_page_records_partial_and_marks_that_page
## Description
Requirement 3: preserve successful page content, mark failed pages, overall status partial.
## Inputs
A three-page scanned PDF where page 2 returns a server error after retries.
## Expected Output
Pages 1 and 3 present; an explicit failure marker for page 2; status partial.

## Test: test_the_model_is_asked_for_a_strict_json_schema
## Description
Requirement 3 wants structured output. Asking for a schema and validating the reply is what
makes "did the extraction work" a checkable question rather than a judgement about prose.
## Inputs
Any extraction request.
## Expected Output
The request body carries a response_format of type json_schema with strict set.

## Test: test_a_valid_structured_reply_is_parsed_into_its_fields
## Inputs
A reply holding document type, visible text, fields, tables and uncertainties.
## Expected Output
Each part is available as typed data, not as a blob of text to re-parse.

## Test: test_a_reply_missing_a_required_key_is_a_parse_error
## Description
Requirement 3: never report a failed extraction as successful.
## Inputs
A JSON reply with no visible_text key.
## Expected Output
RESPONSE_PARSE_ERROR; nothing is published.

## Test: test_a_reply_that_is_not_json_is_a_parse_error
## Description
Not every OpenAI-compatible endpoint honours response_format, so the reply is validated
whatever the endpoint promised.
## Inputs
A reply whose content is prose.
## Expected Output
RESPONSE_PARSE_ERROR; nothing is published.

## Test: test_an_empty_extraction_is_not_reported_as_success
## Inputs
A schema-valid reply whose visible text and fields are all empty.
## Expected Output
The result is not successful, because a blank page and a failed read must stay distinguishable.

## Test: test_unreadable_values_are_preserved_not_inferred
## Description
The prompt forbids guessing an unreadable figure; the marker has to survive into the output,
because a silently invented amount in an audit file is the worst possible failure.
## Inputs
A reply marking a field value [UNREADABLE].
## Expected Output
The marker is preserved verbatim in both the parsed field and the rendered Markdown.

## Test: test_structured_output_renders_to_the_markdown_contract
## Description
SPEC-01 req 3 asks for structured Markdown on disk; the JSON is the wire format, not the
artifact.
## Inputs
A parsed extraction carrying fields and a table.
## Expected Output
Markdown with the agreed sections, the table as a Markdown table, in a stable order.

## Test: test_api_key_is_read_from_the_environment_and_never_logged
## Description
The key arrives from .env only, and a request log or error must never carry it.
## Inputs
A client configured with an API key, driven to an error.
## Expected Output
The key appears in the Authorization header and nowhere in any message or repr.

## Test: test_oversized_image_is_downscaled_before_sending
## Description
Bounding the longest edge bounds both the request size and the per-image cost.
## Inputs
An image larger than the configured maximum edge.
## Expected Output
The sent image is within the limit and keeps its aspect ratio.

## Test: test_pdf_page_is_rasterised_at_the_configured_dpi
## Description
ADR-010: rasterisation uses pypdfium2, never PyMuPDF.
## Inputs
A one-page PDF and a configured DPI.
## Expected Output
A PNG whose pixel size matches the page size at that DPI.

## Test: test_vision_client_retries_on_429_then_503_and_records_backoff
## Description
Backoff is asserted from a recorded sleep sequence, never from wall-clock timing.
## Inputs
Mock transport scripted with 429, then 503, then 200; a stubbed sleep recorder.
## Expected Output
Recorded delays match the configured schedule; the call ultimately succeeds.

## Test: test_vision_client_does_not_retry_client_errors
## Inputs
Mock transport returning 400, then 401, then 413 across three separate calls.
## Expected Output
One attempt each; status failed with API_ERROR; never reported successful.

## Test: test_vision_failure_is_never_reported_as_success
## Description
Requirement 3: do not report a failed extraction as successful.
## Inputs
A response missing the required Markdown sections.
## Expected Output
RESPONSE_PARSE_ERROR, status failed, no Markdown published as active.

## Test: test_no_test_makes_a_real_network_call
## Description
Enforced by an autouse guard rather than reviewer discipline.
## Inputs
The whole suite.
## Expected Output
Any real socket connect or HTTP transport raises immediately.

---

## Group J — Text, chunking, JSON and XML (requirements 2 and 6)

## Test: test_docx_paragraphs_and_tables_are_extracted_in_document_order
## Description
Requirement 2: extracted text must preserve the document's own reading order. python-docx
exposes paragraphs and tables as two separate collections, so body order has to be recovered
from the XML rather than assumed.
## Inputs
A docx whose body is paragraph, table, paragraph in that order.
## Expected Output
Units come back in body order, with the table's cell text between the two paragraphs.

## Test: test_docx_headings_become_their_own_units
## Description
Heading structure is the natural chunk boundary for a financial statement or audit report.
## Inputs
A docx with two "Heading 1" paragraphs, each followed by body text.
## Expected Output
Two units of type HEADING whose unit_ref names the heading text.

## Test: test_detected_tables_are_distinguished_from_extracted_tables
## Description
Requirement 2: distinguish content detection from successful extraction.
## Inputs
A docx with three tables where one fails to parse.
## Expected Output
Result reports three detected and two extracted, so the format document can report both.

## Test: test_html_script_and_style_content_is_not_extracted_as_text
## Description
Script and style bodies are markup machinery, not document content; embedding them would
pollute retrieval with JavaScript.
## Inputs
An HTML page containing a script block, a style block and one paragraph.
## Expected Output
Only the paragraph text appears in the extracted units.

## Test: test_email_headers_and_body_are_both_extracted
## Description
For an email the routing headers carry as much analytical value as the body.
## Inputs
An .eml with From, To, Subject, Date and a plain-text body.
## Expected Output
A header unit carrying all four fields plus a body unit.

## Test: test_email_html_only_body_is_extracted_as_text
## Description
Many client emails have no plain-text alternative part.
## Inputs
An .eml whose only body part is text/html.
## Expected Output
The HTML is reduced to text rather than reported as having no body.

## Test: test_pptx_slides_are_extracted_without_a_new_dependency
## Description
Only two presentations exist in the corpus, so slide text is read from the package XML with
lxml rather than adding python-pptx for two files.
## Inputs
A pptx with two slides carrying text runs.
## Expected Output
One unit per slide, in slide order.

## Test: test_plain_text_encoding_is_detected_and_recorded
## Description
Corpus text files are a mix of UTF-8, UTF-16 and cp1252, and client names contain Devanagari.
## Inputs
A UTF-16 encoded text file containing non-ASCII characters.
## Expected Output
Text decodes correctly and the detected encoding is recorded on the result.

## Test: test_corrupt_document_is_reported_not_raised
## Description
Requirement 5 and the batch-continuity rule: one damaged document cannot abort the run.
## Inputs
Bytes that claim to be a docx but are not a valid package.
## Expected Output
A failure with a CORRUPT_FILE category is returned, never raised.

## Test: test_chunk_metadata_carries_full_lineage
## Inputs
Text units from a document inside a client scope.
## Expected Output
Each chunk records scope, category, client, source path, content hash, unit type, unit reference,
sequence, and character offsets.

## Test: test_empty_text_produces_no_chunks
## Description
Requirement 2: do not embed empty text.
## Inputs
A document extracting to whitespace only.
## Expected Output
Zero chunks; the skipped count is recorded so the format document can report it.

## Test: test_repeated_unit_refs_stay_individually_addressable
## Description
Corpus regression: unit_ref is a human citation, not an identifier. One corpus bank statement
extracts to 784 units carrying only 167 distinct refs, with the heading "Receipt" repeated 250
times, so a chunk naming only a ref and an offset cannot be resolved back to one place.
## Inputs
Two units sharing a unit_ref but holding different text.
## Expected Output
Chunks carry distinct unit_sequence values and distinct chunk ids.

## Test: test_offsets_resolve_against_the_unit_named_by_unit_sequence
## Description
The offsets of one unit are meaningless when applied to another.
## Inputs
Two units sharing a unit_ref, of very different lengths.
## Expected Output
Each chunk's offsets resolve correctly against the unit its unit_sequence names.

## Test: test_chunk_character_offsets_locate_the_text_in_its_unit
## Description
An offset that does not resolve back to the source text makes a chunk untraceable, which
defeats the lineage requirement.
## Inputs
A unit long enough to split into several chunks.
## Expected Output
For every chunk, unit_text[char_start:char_end] equals the chunk text.

## Test: test_chunk_ids_are_deterministic_across_runs
## Description
Requirement 8: a rerun that reuses content must not renumber or rename its chunks.
## Inputs
The same units, scope and configuration chunked twice.
## Expected Output
Identical chunk ids in identical order.

## Test: test_identical_text_in_two_scopes_produces_different_chunk_ids
## Description
Requirement 7: scopes are independent, so no chunk id may be shared across them.
## Inputs
The same text chunked under two different client scopes.
## Expected Output
The chunk ids differ.

## Test: test_archive_member_lineage_is_carried_into_chunks
## Description
Requirement 12: a chunk from inside an archive must name the archive and the member.
## Inputs
Units from a source reference carrying an archive chain.
## Expected Output
Chunks record the archive id and member path.

## Test: test_chunk_overlap_is_applied_between_adjacent_chunks
## Inputs
A unit split with a configured overlap.
## Expected Output
Adjacent chunks share the configured amount of text.

## Test: test_tabular_json_collection_routes_to_parquet
## Description
Requirement 6: route JSON by observed structure, not extension.
## Inputs
An ITR-shaped JSON with a homogeneous array of records.
## Expected Output
Parquet written for that field path; the path is recorded as lineage.

## Test: test_document_like_json_routes_to_chunking
## Inputs
A deeply nested JSON with no homogeneous collection.
## Expected Output
No Parquet; chunks whose unit type is a field path.

## Test: test_a_short_array_is_not_treated_as_a_collection
## Description
The minimum collection size stops a two-element array of options becoming a table.
## Inputs
An array of two homogeneous objects.
## Expected Output
No Parquet; the values appear as field-path text.

## Test: test_an_array_of_dissimilar_objects_is_not_treated_as_a_collection
## Description
Key-set Jaccard is what separates a record array from a list of unrelated structures.
## Inputs
An array of four objects with almost no keys in common.
## Expected Output
No Parquet; the values appear as field-path text.

## Test: test_an_array_of_scalars_is_not_treated_as_a_collection
## Inputs
An array of six strings.
## Expected Output
No Parquet, because the object-element ratio is zero.

## Test: test_a_collection_of_deeply_nested_objects_is_not_flattened_to_parquet
## Description
Depth is capped so a Parquet table cannot silently lose nested structure.
## Inputs
An array of records whose values are themselves nested objects several levels deep.
## Expected Output
No Parquet; the records render as field-path text instead.

## Test: test_field_paths_use_dotted_notation_with_list_indices
## Description
Requirement 6: every chunk must trace back to the field path it came from.
## Inputs
A nested JSON containing an array of scalars.
## Expected Output
Lines read as Schedule.Items.0.Amount, in document order.

## Test: test_xml_elements_attributes_and_text_are_all_rendered
## Description
XML carries data in three places and dropping any one loses client data.
## Inputs
An XML element with an attribute, child elements and text.
## Expected Output
All three appear in the field-path rendering.

## Test: test_repeated_xml_elements_become_a_collection
## Description
Requirement 6: XML is routed by observed structure exactly as JSON is.
## Inputs
An XML document with six repeated homogeneous child elements.
## Expected Output
Parquet is written for that field path.

## Test: test_extracted_collections_are_not_duplicated_in_the_text_rendering
## Description
Content written to Parquet must not also be chunked and embedded, or every row is stored twice.
## Inputs
A document with one qualifying collection and one prose field.
## Expected Output
The text rendering references the collection's output but does not repeat its rows.

## Test: test_malformed_json_is_reported_not_raised
## Description
Requirement 5 and the batch-continuity rule.
## Inputs
Truncated JSON bytes.
## Expected Output
A CORRUPT_FILE failure is returned, never raised.

## Test: test_xml_external_entity_is_not_resolved
## Description
An XXE payload in a client file must never cause a local file read or a network fetch.
## Inputs
XML declaring an external entity pointing at a local file.
## Expected Output
The entity is not expanded and no file is read.

## Test: test_json_leading_zero_identifier_survives_flattening
## Description
ITR JSON is full of zero-padded PAN and TAN values.
## Inputs
A record array containing a PAN value with leading zeros.
## Expected Output
Parquet column is string with the value intact.

---

## Group K — Coverage and format documentation (requirements 4 and 6)

## Test: test_every_discovered_file_has_a_processing_record
## Description
Requirement 6 and acceptance criterion 9: no silent skips anywhere.
## Inputs
A synthetic tree containing one file of every routed bucket.
## Expected Output
Record count equals discovered file count.

## Test: test_format_doc_is_written_per_file_not_per_extension
## Description
Requirement 4: a generic per-extension document is not sufficient.
## Inputs
Two different xlsx files in one client scope.
## Expected Output
Two distinct companion documents named after the full filename.

## Test: test_format_doc_preserves_source_relative_structure_and_full_filename
## Description
Requirement 4: naming must avoid collisions across clients, folders and extensions.
## Inputs
Files a/report.xlsx and b/report.pdf in one scope.
## Expected Output
Companion documents a/report.xlsx.format.md and b/report.pdf.format.md, with no collision.

## Test: test_unknown_information_is_marked_unknown_not_invented
## Description
Requirement 4: do not invent schemas, column meanings or content classifications.
## Inputs
A file whose structure cannot be inspected.
## Expected Output
Format document marks the structure explicitly unknown.

## Test: test_format_document_records_every_required_identity_field
## Description
Requirement 4 lists the identity a document must carry: source path, scope, extension,
detected format, content hash, version, status and output references.
## Inputs
A document for an ordinary tabular file.
## Expected Output
Every listed field appears with its value.

## Test: test_archive_member_document_names_its_archive_and_member_path
## Description
Requirement 4: archive members must retain their originating archive and member paths.
## Inputs
A document for a file extracted from a zip.
## Expected Output
Both the archive identity and the member path appear.

## Test: test_tabular_document_distinguishes_inferred_types_from_source_information
## Description
Requirement 4 is explicit that an inferred type must not be presented as though the source
declared it.
## Inputs
A worksheet with one inferred column and one kept as text.
## Expected Output
The document marks which is inferred and gives the reason the other was not.

## Test: test_locked_file_document_records_stage_status_and_retry_action
## Description
Requirement 4's last row: a locked file still gets a document saying what to do about it.
## Inputs
A document for a password-protected PDF.
## Expected Output
Status, error category, processing stage and the retry action are all present.

## Test: test_a_section_with_nothing_to_report_says_so_explicitly
## Description
An absent section is ambiguous between "nothing found" and "never looked", which requirement
4's prohibition on inventing content makes unacceptable.
## Inputs
A document whose table section is empty.
## Expected Output
The section is present and explicitly says none were found.

## Test: test_rendered_document_escapes_pipes_in_values
## Description
A transcribed value containing a pipe would otherwise corrupt the surrounding table.
## Inputs
A column name containing a pipe character.
## Expected Output
The pipe is escaped and the table structure survives.

---

## Acceptance criteria traceability

| AC | Requirement | Covering tests | Level |
|---|---|---|---|
| 1 | CSV and Excel to Parquet with format Markdown | Group H, test_format_doc_is_written_per_file_not_per_extension | unit |
| 2 | Text inputs produce chunks and traceable metadata | Group J chunking tests, test_fully_text_pdf_routes_to_text_extraction | unit |
| 3 | Scanned and mixed PDFs produce one combined Markdown | test_mixed_pdf_emits_single_combined_markdown_in_page_order, test_image_only_pdf_routes_to_vision | unit |
| 4 | Vision Markdown, explicit failures, partial excluded from active | Group I | unit |
| 5 | Locked file recorded, others continue, unlock creates new version | Group F | unit and integration |
| 6 | JSON and XML routed by observed structure | test_tabular_json_collection_routes_to_parquet, test_document_like_json_routes_to_chunking | unit |
| 7 | ZIP members deduplicated within client scope | Group C, Group G | integration |
| 8 | Matching client names in different categories stay independent | test_identical_bytes_in_two_categories_do_not_deduplicate, test_scope_id_is_stable_and_distinct_per_category | unit and integration |
| 9 | Every discovered file has a record | test_every_discovered_file_has_a_processing_record | integration |
| 10 | Unchanged files reuse; changes and failures retry | test_unchanged_content_and_fingerprint_skips_reader, test_config_change_invalidates_only_affected_route_outputs | unit and integration |
| 11 | Reruns never modify or delete; latest successful wins | test_rerun_does_not_mutate_any_prior_version_artifact, test_active_version_resolves_to_newest_successful_version | integration |
| 12 | Distinct lineage and versioned paths; duplicates share content | test_duplicate_within_client_shares_output_and_retains_both_source_paths, test_archive_member_retains_archive_lineage | unit and integration |

Acceptance criterion 2 is satisfied at Silver only up to chunk production. Embeddings and FAISS
persistence are Gold-layer work in a later phase and are deliberately out of scope for TP-01.
