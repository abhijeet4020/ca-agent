"""Tests for the batch vision pass in the executor (TP-01 Group I, SPEC-01 req 3).

The vision route is the only paid step in the pipeline, so these tests are about two things:
that a scanned image or PDF page actually reaches the model and becomes the Markdown the
specification stores, and that a failure is never dressed up as a success. Every test injects
an httpx.MockTransport through the executor's transport seam, so the suite's autouse network
kill-switch never fires and no test can spend money.
"""

from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path, PurePosixPath

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.config.settings import PathSettings, PipelineSettings, VisionSettings  # noqa: E402
from ca_agent.core.enums import ErrorCategory, FormatFamily, ProcessingStatus, Route  # noqa: E402
from ca_agent.core.model import ContentHash, SourceRef  # noqa: E402
from ca_agent.core.scope import ClientScope  # noqa: E402
from ca_agent.pipeline import executor  # noqa: E402
from testdata.builders import files  # noqa: E402

#: Longer than the default 120 non-whitespace character threshold, so a page carrying it is
#: classified TEXT and must never buy a vision call.
_SAMPLE_TEXT = "\n".join(
    (
        "Statement of profit and loss for the year ended 31 March 2026 with all schedules",
        "Depreciation, finance costs and other expenses reconciled against the trial balance",
        "Reserves and surplus carried forward to the balance sheet as at the same date",
    )
)

_SCOPE = ClientScope(
    category="Business Clients", client="ACME TRADING", scope_root=PurePosixPath(".")
)


def _payload(text: str, document_type: str = "Challan") -> dict:
    """A contract-valid extraction reply carrying ``text`` as its visible text."""
    return {
        "document_type": document_type,
        "summary": "A document.",
        "visible_text": text,
        "fields": [{"label": "Amount", "value": "45,000", "confidence": "high"}],
        "tables": [],
        "uncertainties": [],
    }


def _mock_transport(monkeypatch, replies: list[tuple[int, dict | None]]) -> dict:
    """Wire the executor to a mock transport returning ``replies`` in call order.

    Returns a counter so a test can assert exactly how many paid calls were made - the
    property that matters most, because every call costs money.
    """
    seen = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        index = min(seen["count"], len(replies) - 1)
        seen["count"] += 1
        status, payload = replies[index]
        if status == 200:
            content = json.dumps(payload)
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        return httpx.Response(status)

    monkeypatch.setattr(
        executor,
        "_vision_transport",
        lambda settings: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return seen


def _settings(tmp_path: Path, *, vision_enabled: bool = True, max_attempts: int = 1, max_edge=None):
    vision = VisionSettings(
        enabled=vision_enabled, api_key=None, model="test-model", max_attempts=max_attempts
    )
    if max_edge is not None:
        vision = vision.model_copy(update={"max_image_edge_pixels": max_edge})
    return PipelineSettings(
        paths=PathSettings(raw_root=tmp_path, output_root=tmp_path / "out"), vision=vision
    )


def _execute(path: Path, *, route: Route, family: FormatFamily, destination: Path, settings):
    return executor.execute(
        path,
        route=route,
        family=family,
        source=SourceRef(scope=_SCOPE, relative_path=PurePosixPath(path.name)),
        content=ContentHash(hexdigest="a" * 64, size_bytes=path.stat().st_size),
        destination=destination,
        extraction_root=destination,
        settings=settings,
        route_fingerprint="fingerprint",
    )


# --- the image route -----------------------------------------------------------------------


def test_image_route_writes_one_vision_markdown_and_succeeds(tmp_path, monkeypatch):
    # Arrange
    image = files.write_bytes(tmp_path / "challan.png", files.image_bytes("PNG", (48, 48)))
    seen = _mock_transport(monkeypatch, [(200, _payload("GST CHALLAN CPIN 25090100012345"))])

    # Act
    result = _execute(
        image,
        route=Route.VISION_IMAGE,
        family=FormatFamily.IMAGE,
        destination=tmp_path / "dest",
        settings=_settings(tmp_path),
    )

    # Assert - the paid call happened, and its result is the artifact on disk
    assert seen["count"] == 1
    assert result.status is ProcessingStatus.SUCCESS
    assert {output.relative_path.as_posix() for output in result.outputs} == {"vision/document.md"}
    markdown = (tmp_path / "dest" / "vision" / "document.md").read_text(encoding="utf-8")
    assert "25090100012345" in markdown, "the extracted text must survive to the Markdown"
    assert "test-model" in markdown, "req 3 requires the model used to be recorded"


def test_vision_failure_is_never_reported_as_success(tmp_path, monkeypatch):
    # Arrange - a server fault must be a recorded failure, never a silent empty success
    image = files.write_bytes(tmp_path / "challan.png", files.image_bytes())
    _mock_transport(monkeypatch, [(500, None)])

    # Act
    result = _execute(
        image,
        route=Route.VISION_IMAGE,
        family=FormatFamily.IMAGE,
        destination=tmp_path / "dest",
        settings=_settings(tmp_path),
    )

    # Assert
    assert result.status is ProcessingStatus.FAILED
    assert result.error is not None
    assert result.error.category is ErrorCategory.API_ERROR
    assert result.outputs == ()
    assert not (tmp_path / "dest" / "vision" / "document.md").exists()


def test_disabled_vision_records_pending_without_spending(tmp_path, monkeypatch):
    # Arrange - req 6 wants the file recorded, and the paid step must not be attempted
    image = files.write_bytes(tmp_path / "challan.png", files.image_bytes())
    seen = _mock_transport(monkeypatch, [(200, _payload("unused"))])

    # Act
    result = _execute(
        image,
        route=Route.VISION_IMAGE,
        family=FormatFamily.IMAGE,
        destination=tmp_path / "dest",
        settings=_settings(tmp_path, vision_enabled=False),
    )

    # Assert
    assert result.status is ProcessingStatus.PARTIAL
    assert seen["count"] == 0
    assert result.retry_action


# --- the PDF route -------------------------------------------------------------------------


def test_fully_text_pdf_routes_to_text_extraction(tmp_path, monkeypatch):
    # Arrange - a digital filing must never buy a vision call
    pdf = files.write_bytes(tmp_path / "return.pdf", files.text_pdf_bytes([_SAMPLE_TEXT]))
    seen = _mock_transport(monkeypatch, [(200, _payload("unused"))])

    # Act
    result = _execute(
        pdf,
        route=Route.PDF_TEXT,
        family=FormatFamily.PDF,
        destination=tmp_path / "dest",
        settings=_settings(tmp_path),
    )

    # Assert
    assert seen["count"] == 0, "a text PDF must not be sent to the paid route"
    assert result.effective_route is None
    assert result.status is ProcessingStatus.SUCCESS
    kinds = {output.relative_path.as_posix() for output in result.outputs}
    assert "chunks/chunks.jsonl" in kinds
    assert not (tmp_path / "dest" / "vision" / "document.md").exists()


def test_image_only_pdf_routes_to_vision_with_one_markdown_and_no_chunks(tmp_path, monkeypatch):
    # Arrange - SPEC-01 req 3: a scanned PDF is a vision document, not a text one
    pdf = files.write_bytes(tmp_path / "scan.pdf", files.scanned_pdf_bytes(1))
    seen = _mock_transport(monkeypatch, [(200, _payload("SCANNED PAGE ONE"))])

    # Act
    result = _execute(
        pdf,
        route=Route.PDF_TEXT,
        family=FormatFamily.PDF,
        destination=tmp_path / "dest",
        settings=_settings(tmp_path),
    )

    # Assert - the classifier redirects the route, and no chunks or Parquet are produced
    assert result.effective_route is Route.PDF_VISION
    assert result.status is ProcessingStatus.SUCCESS
    assert seen["count"] == 1
    assert {output.relative_path.as_posix() for output in result.outputs} == {"vision/document.md"}
    assert not (tmp_path / "dest" / "chunks" / "chunks.jsonl").exists()


def test_mixed_pdf_emits_single_combined_markdown_in_page_order(tmp_path, monkeypatch):
    # Arrange - page 1 has native text, pages 2 and 3 are scans
    pdf = files.write_bytes(
        tmp_path / "mixed.pdf",
        files.pdf_bytes(
            [
                {"text": _SAMPLE_TEXT},
                {"image_fraction": 0.95},
                {"image_fraction": 0.95},
            ]
        ),
    )
    seen = _mock_transport(
        monkeypatch,
        [(200, _payload("SECOND PAGE VISION")), (200, _payload("THIRD PAGE VISION"))],
    )

    # Act
    result = _execute(
        pdf,
        route=Route.PDF_TEXT,
        family=FormatFamily.PDF,
        destination=tmp_path / "dest",
        settings=_settings(tmp_path),
    )

    # Assert - one document, page order preserved, native text page not sent
    assert result.effective_route is Route.PDF_VISION
    assert seen["count"] == 2, "the native text page must not be sent to the paid route"
    markdown = (tmp_path / "dest" / "vision" / "document.md").read_text(encoding="utf-8")
    assert markdown.count("## Page") == 3
    assert (
        markdown.index("## Page 1") < markdown.index("## Page 2") < markdown.index("## Page 3")
    )
    assert "Statement of profit and loss" in markdown, "page 1 keeps its native text"
    assert "SECOND PAGE VISION" in markdown
    assert "THIRD PAGE VISION" in markdown


def test_scanned_pdf_with_failing_page_records_partial_and_marks_that_page(tmp_path, monkeypatch):
    # Arrange - page 2 fails after retries; pages 1 and 3 succeed
    pdf = files.write_bytes(tmp_path / "scan.pdf", files.scanned_pdf_bytes(3))
    _mock_transport(
        monkeypatch,
        [(200, _payload("PAGE ONE")), (500, None), (200, _payload("PAGE THREE"))],
    )

    # Act
    result = _execute(
        pdf,
        route=Route.PDF_TEXT,
        family=FormatFamily.PDF,
        destination=tmp_path / "dest",
        settings=_settings(tmp_path),
    )

    # Assert - successful pages preserved, the failed page marked, overall partial
    assert result.status is ProcessingStatus.PARTIAL
    assert result.error is not None
    markdown = (tmp_path / "dest" / "vision" / "document.md").read_text(encoding="utf-8")
    assert "PAGE ONE" in markdown
    assert "PAGE THREE" in markdown
    assert "## Page 2" in markdown
    assert "failed" in markdown.lower(), "the failed page must be marked, not omitted"


def test_disabled_vision_on_a_scanned_pdf_records_pending_without_spending(tmp_path, monkeypatch):
    # Arrange
    pdf = files.write_bytes(tmp_path / "scan.pdf", files.scanned_pdf_bytes(1))
    seen = _mock_transport(monkeypatch, [(200, _payload("unused"))])

    # Act
    result = _execute(
        pdf,
        route=Route.PDF_TEXT,
        family=FormatFamily.PDF,
        destination=tmp_path / "dest",
        settings=_settings(tmp_path, vision_enabled=False),
    )

    # Assert - classified and recorded as pending, no call, no artifact
    assert result.status is ProcessingStatus.PARTIAL
    assert result.effective_route is Route.PDF_VISION
    assert seen["count"] == 0
    assert not (tmp_path / "dest" / "vision" / "document.md").exists()


def test_a_rasterised_pdf_page_is_bounded_before_sending(tmp_path, monkeypatch):
    # Arrange - a full-page scan at the configured DPI can run to several megabytes, which a
    # local server rejects outright; the longest edge must be bounded exactly as for an image.
    pdf = files.write_bytes(tmp_path / "scan.pdf", files.scanned_pdf_bytes(1))
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        url = next(
            part["image_url"]["url"]
            for part in body["messages"][0]["content"]
            if part["type"] == "image_url"
        )
        captured["image"] = base64.b64decode(url.split(",", 1)[1])
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(_payload("PAGE"))}}]}
        )

    monkeypatch.setattr(
        executor,
        "_vision_transport",
        lambda settings: httpx.Client(transport=httpx.MockTransport(handler)),
    )

    # Act - a 250 dpi US Letter page rasterises to 2125 x 2750 before it is bounded
    _execute(
        pdf,
        route=Route.PDF_TEXT,
        family=FormatFamily.PDF,
        destination=tmp_path / "dest",
        settings=_settings(tmp_path, max_edge=200),
    )

    # Assert
    from PIL import Image

    with Image.open(io.BytesIO(captured["image"])) as image:
        assert max(image.size) <= 200


# --- the combined document itself -----------------------------------------------------------


def test_combined_markdown_carries_format_information_and_limitations():
    # Arrange - req 3 asks for format, dimensions, model, status and limitations in the document
    from ca_agent.vision.contract import (
        VisionDocument,
        VisionExtraction,
        VisionPage,
        render_vision_document,
    )

    extraction = VisionExtraction(
        document_type="GST payment challan",
        summary="One challan.",
        visible_text="CPIN 25090100012345",
        fields=(),
        tables=(),
        uncertainties=(),
    )
    document = VisionDocument(
        display_name="challan.png",
        source_path="Business Clients/ACME/challan.png",
        image_format="PNG",
        width=1560,
        height=2000,
        page_count=1,
        model="google/gemma-3-4b",
        status="success",
        pages=(VisionPage(number=1, extraction=extraction),),
        limitations=("The bottom of the page is partly obscured.",),
    )

    # Act
    markdown = render_vision_document(document)

    # Assert
    assert "# challan.png" in markdown
    assert "1560" in markdown and "2000" in markdown
    assert "google/gemma-3-4b" in markdown
    assert "25090100012345" in markdown
    assert "partly obscured" in markdown
    assert "## Page" not in markdown, "a single page needs no page heading"
