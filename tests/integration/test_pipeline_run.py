"""End-to-end pipeline tests (TP-01 Groups D, G, K; SPEC-01 acceptance criteria 7-12).

These are the properties no unit test can prove, because they are about what happens across a
whole run and across two runs. The load-bearing one is requirement 8: a rerun must never
modify, overwrite or delete anything a previous run published. That is asserted by hashing
every file in the output tree before and after, because on Windows neither mtime nor ACLs are
reliable enough to build a durability guarantee on.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.config.settings import PathSettings, PipelineSettings, VisionSettings  # noqa: E402
from ca_agent.core.enums import ProcessingStatus  # noqa: E402
from ca_agent.pipeline.orchestrator import RunOptions, format_report, run_pipeline  # noqa: E402
from ca_agent.storage.paths import SilverPaths  # noqa: E402
from ca_agent.versioning.reuse import RetryPolicy  # noqa: E402
from testdata.builders import files  # noqa: E402

_TEXT = "\n".join(
    (
        "Statement of profit and loss for the year ended 31 March 2026 with all schedules",
        "Depreciation and finance costs reconciled against the trial balance as at that date",
    )
)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A miniature corpus with one file of most routed buckets, across two categories."""
    raw = tmp_path / "raw_data"
    acme = raw / "Business Clients" / "ACME TRADING"
    llp = raw / "LLP" / "EXAMPLE LLP"

    files.write_bytes(acme / "accounts" / "ledger.xlsx", files.workbook_bytes(
        {"Sheet1": [["PAN", "Amount"], ["0001234A", 100], ["0005678B", 250]]},
        text_columns={"Sheet1": (0,)},
    ))
    files.write_bytes(acme / "accounts" / "register.csv", files.csv_bytes())
    files.write_bytes(acme / "notes" / "memo.docx", files.document_bytes(
        [("heading", "Balance Sheet"), ("paragraph", _TEXT)]
    ))
    files.write_bytes(acme / "filings" / "return.json", files.itr_json_bytes(record_count=4))
    files.write_bytes(acme / "filings" / "acknowledgement.pdf", files.text_pdf_bytes([_TEXT]))
    files.write_bytes(acme / "filings" / "scan.pdf", files.scanned_pdf_bytes(1))
    files.write_bytes(acme / "misc" / "Thumbs.db", files.thumbs_db_bytes())
    files.write_bytes(acme / "misc" / "company.tsf", files.tally_binary_bytes())
    files.write_bytes(acme / "misc" / "bundle.zip", files.plain_zip_bytes())
    files.write_bytes(llp / "docs" / "ledger.xlsx", files.workbook_bytes(
        {"Sheet1": [["PAN", "Amount"], ["0001234A", 100], ["0005678B", 250]]},
        text_columns={"Sheet1": (0,)},
    ))
    return raw


def _settings(corpus: Path, output: Path) -> PipelineSettings:
    return PipelineSettings(
        paths=PathSettings(raw_root=corpus, output_root=output),
        # Disabled so the suite can never spend money; the routes record pending work instead.
        vision=VisionSettings(enabled=False, api_key=None),
    )


def _snapshot(root: Path) -> dict[str, str]:
    """Content hash of every file under the output tree.

    Hashes rather than mtimes: Windows mtime granularity and ACL behaviour are not a sound
    basis for an immutability assertion.
    """
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- coverage (acceptance criterion 9) -----------------------------------------------------


def test_every_discovered_file_has_a_processing_record(corpus, tmp_path):
    # Arrange
    output = tmp_path / "out"
    settings = _settings(corpus, output)

    # Act
    summary = run_pipeline(settings, RunOptions())

    # Assert - req 6: nothing is silently skipped, whatever its format. Archive members are
    # additional units of work beyond the discovered files, each with a record of its own.
    accounted = summary.processed + summary.reused + summary.duplicates
    assert accounted == summary.discovered + summary.archive_members
    assert sum(summary.by_status.values()) == accounted


def test_unreadable_formats_are_recorded_rather_than_skipped(corpus, tmp_path):
    # Arrange / Act - a Tally binary and a Thumbs.db have no extraction path at all
    summary = run_pipeline(_settings(corpus, tmp_path / "out"), RunOptions())

    # Assert
    assert summary.by_status.get(ProcessingStatus.NO_READER.value, 0) >= 1
    assert summary.by_status.get(ProcessingStatus.EXCLUDED_NON_DATA.value, 0) >= 1


def test_every_processed_file_gets_a_format_document(corpus, tmp_path):
    # Arrange - requirement 4
    output = tmp_path / "out"

    # Act
    run_pipeline(_settings(corpus, output), RunOptions())
    documents = list(output.rglob("*.format.md"))

    # Assert
    assert documents, "requirement 4 requires a companion document per file"
    assert any(path.name == "ledger.xlsx.format.md" for path in documents)
    assert any(path.name == "scan.pdf.format.md" for path in documents)


def test_format_doc_is_written_per_file_not_per_extension(corpus, tmp_path):
    # Arrange - requirement 4: a generic per-extension document is not sufficient
    output = tmp_path / "out"

    # Act
    run_pipeline(_settings(corpus, output), RunOptions())
    names = [path.name for path in output.rglob("*.format.md")]

    # Assert - two different xlsx files produce two documents, not one shared one
    assert names.count("ledger.xlsx.format.md") == 2


# --- immutability (requirement 8, acceptance criterion 11) --------------------------------------


def test_rerun_does_not_mutate_any_prior_version_artifact(corpus, tmp_path):
    # Arrange - the single most important property in the specification
    output = tmp_path / "out"
    settings = _settings(corpus, output)
    run_pipeline(settings, RunOptions())
    before = _snapshot(output)

    # Act
    run_pipeline(settings, RunOptions())
    after = _snapshot(output)

    # Assert - every artifact the first run published is byte-identical afterwards
    for relpath, digest in before.items():
        assert relpath in after, f"{relpath} was deleted by the rerun"
        assert after[relpath] == digest, f"{relpath} was modified by the rerun"


def test_unchanged_content_and_fingerprint_skips_reprocessing(corpus, tmp_path):
    # Arrange - requirement 8's reuse contract
    output = tmp_path / "out"
    settings = _settings(corpus, output)
    first = run_pipeline(settings, RunOptions())

    # Act
    second = run_pipeline(settings, RunOptions())

    # Assert - everything the first run settled is reused rather than redone. Only the partial
    # PDF is retried, because its vision pages are still outstanding (req 8's retry rule), and
    # the reused archive is not re-expanded, so its members are not re-queued either.
    assert first.processed > 0
    settled = summary_settled(first)
    assert second.reused == settled
    assert second.processed == first.discovered - settled
    assert second.archive_members == 0, "a reused archive must not be expanded again"


def summary_settled(summary) -> int:
    """Top-level files whose first-run outcome needs no further attempt."""
    retryable = summary.by_status.get("partial", 0) + summary.by_status.get("failed", 0)
    return summary.discovered - retryable


def test_forcing_a_rerun_publishes_a_new_version_without_touching_the_old(corpus, tmp_path):
    # Arrange
    output = tmp_path / "out"
    settings = _settings(corpus, output)
    run_pipeline(settings, RunOptions())
    before = _snapshot(output)

    # Act
    forced = run_pipeline(settings, RunOptions(policy=RetryPolicy(force=True)))
    after = _snapshot(output)

    # Assert
    assert forced.processed > 0
    assert len(after) > len(before), "a forced rerun must publish additional versions"
    for relpath, digest in before.items():
        assert after[relpath] == digest


def test_a_dry_run_writes_nothing(corpus, tmp_path):
    # Arrange
    output = tmp_path / "out"

    # Act
    summary = run_pipeline(_settings(corpus, output), RunOptions(dry_run=True))

    # Assert
    assert summary.discovered > 0
    assert not list(output.rglob("*.format.md"))


# --- scope isolation (requirement 7, acceptance criterion 8) ----------------------------------------


def test_identical_bytes_in_two_categories_do_not_deduplicate(corpus, tmp_path):
    # Arrange - the same ledger.xlsx exists under Business Clients and under LLP
    output = tmp_path / "out"

    # Act
    run_pipeline(_settings(corpus, output), RunOptions())
    scopes = {path.parent.name for path in output.rglob("record.json")}
    documents = [path for path in output.rglob("ledger.xlsx.format.md")]

    # Assert - two independent outputs, because scope is part of the work identity
    assert len(documents) == 2
    assert len(scopes) >= 1


def test_each_scope_gets_its_own_sealed_manifest(corpus, tmp_path):
    # Arrange
    output = tmp_path / "out"

    # Act
    run_pipeline(_settings(corpus, output), RunOptions())
    manifests = list(SilverPaths(output).scopes_dir().rglob("manifest-*.json"))

    # Assert - one per scope that produced work
    assert len(manifests) == 2


# --- records (requirement 4 and 12) -------------------------------------------------------------------


def test_records_carry_lineage_and_outputs(corpus, tmp_path):
    # Arrange
    output = tmp_path / "out"

    # Act
    run_pipeline(_settings(corpus, output), RunOptions())
    records = [json.loads(path.read_text(encoding="utf-8")) for path in output.rglob("record.json")]

    # Assert
    assert records
    for record in records:
        assert record["content_sha256"]
        assert record["source_paths"]
        assert record["route"]
        assert record["version_id"]
        assert record["status"]


def test_archive_members_are_processed_with_their_own_records(corpus, tmp_path):
    # Arrange - requirement 7: members re-enter the router as ordinary work
    output = tmp_path / "out"

    # Act
    summary = run_pipeline(_settings(corpus, output), RunOptions())

    # Assert
    assert summary.archive_members >= 2, "the zip holds a readme and a csv"


def test_scanned_pdf_is_recorded_as_needing_vision_without_spending(corpus, tmp_path):
    # Arrange - vision is disabled, so the classification still happens but no call is made
    output = tmp_path / "out"

    # Act
    summary = run_pipeline(_settings(corpus, output), RunOptions())

    # Assert
    assert summary.pages_needing_vision >= 1
    assert summary.by_status.get(ProcessingStatus.PARTIAL.value, 0) >= 1


def test_chunks_are_produced_for_text_bearing_files(corpus, tmp_path):
    # Arrange / Act
    output = tmp_path / "out"
    summary = run_pipeline(_settings(corpus, output), RunOptions())

    # Assert
    assert summary.chunks > 0
    assert list(output.rglob("chunks.jsonl"))


def test_leading_zero_identifiers_survive_the_whole_pipeline(corpus, tmp_path):
    # Arrange - the guarantee that matters most for a tax corpus, asserted end to end
    import pyarrow.parquet as pq

    output = tmp_path / "out"

    # Act
    run_pipeline(_settings(corpus, output), RunOptions())
    parquet_files = list(output.rglob("*.parquet"))

    # Assert
    values = set()
    for path in parquet_files:
        table = pq.read_table(path)
        if "PAN" in table.schema.names:
            values.update(table.column("PAN").to_pylist())
    assert "0001234A" in values


def test_the_report_summarises_without_drowning_the_operator(corpus, tmp_path):
    # Arrange / Act
    summary = run_pipeline(_settings(corpus, tmp_path / "out"), RunOptions())
    report = format_report(summary)

    # Assert
    assert "discovered:" in report
    assert "status:" in report
    assert len(report.splitlines()) < 80, "a run report must stay readable"
