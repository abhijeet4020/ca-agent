"""Tests for the vision route (TP-01 Group I, SPEC-01 requirement 3).

This is the only route that spends money and the only one that talks to a model, so two
properties matter more than anything else. A failed extraction must never be reported as a
success - a blank result and a broken call have to stay distinguishable - and an unreadable
figure must never be quietly replaced by a plausible one, because an invented amount in an
audit file is the worst outcome this pipeline can produce.

Every test here injects an httpx.MockTransport. The suite's autouse fixture makes a real socket
connection raise, so "no test spends money" is enforced by the harness, not by discipline.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.config.settings import VisionSettings  # noqa: E402
from ca_agent.core.enums import ErrorCategory  # noqa: E402
from ca_agent.vision.client import VisionClient  # noqa: E402
from ca_agent.vision.contract import (  # noqa: E402
    RESPONSE_SCHEMA,
    ExtractedField,
    ExtractedTable,
    VisionExtraction,
    render_markdown,
)
from testdata.builders import files  # noqa: E402

_SETTINGS = VisionSettings(api_key="test-key-not-real", model="test-model")

_VALID_PAYLOAD = {
    "document_type": "GST payment challan",
    "summary": "A GST challan for the September 2025 period.",
    "visible_text": "GST CHALLAN\nCPIN 25090100012345\nAmount 45,000",
    "fields": [
        {"label": "CPIN", "value": "25090100012345", "confidence": "high"},
        {"label": "Amount", "value": "45,000", "confidence": "high"},
    ],
    "tables": [
        {
            "caption": "Tax breakup",
            "columns": ["Head", "Amount"],
            "rows": [["IGST", "45,000"], ["CGST", "0"]],
        }
    ],
    "uncertainties": [],
}


def _reply(payload: object, *, status: int = 200) -> httpx.Response:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(
        status,
        json={"choices": [{"message": {"content": content}}]},
    )


def _client(handler, settings: VisionSettings | None = None, sleeps: list[float] | None = None):
    transport = httpx.MockTransport(handler)
    return VisionClient(
        httpx.Client(transport=transport),
        settings=settings or _SETTINGS,
        sleep=(sleeps if sleeps is None else sleeps.append),
    )


def _extract(client) -> object:
    return client.extract(files.image_bytes(), media_type="image/png")


# --- the structured contract -------------------------------------------------------------


def test_the_model_is_asked_for_a_json_schema(tmp_path):
    # Arrange - asking for a schema is what makes "did this work" checkable
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _reply(_VALID_PAYLOAD)

    # Act
    _extract(_client(handler))

    # Assert - the schema is always sent; `strict` is separately configurable
    response_format = seen["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["schema"] == RESPONSE_SCHEMA


def test_a_valid_structured_reply_is_parsed_into_its_fields():
    # Arrange / Act
    result = _extract(_client(lambda request: _reply(_VALID_PAYLOAD)))

    # Assert - typed data, not a blob to re-parse downstream
    assert result.ok is True
    extraction = result.extraction
    assert extraction.document_type == "GST payment challan"
    assert extraction.fields[0] == ExtractedField("CPIN", "25090100012345", "high")
    assert extraction.tables[0].columns == ("Head", "Amount")
    assert extraction.tables[0].rows[0] == ("IGST", "45,000")


def test_a_reply_missing_a_required_key_is_a_parse_error():
    # Arrange - SPEC-01 req 3: never report a failed extraction as successful
    payload = {key: value for key, value in _VALID_PAYLOAD.items() if key != "visible_text"}

    # Act
    result = _extract(_client(lambda request: _reply(payload)))

    # Assert
    assert result.ok is False
    assert result.error is not None
    assert result.error.category is ErrorCategory.RESPONSE_PARSE_ERROR


def test_a_reply_that_is_not_json_is_a_parse_error():
    # Arrange - not every OpenAI-compatible endpoint honours response_format
    result = _extract(_client(lambda request: _reply("Here is what I found in the image!")))

    # Assert
    assert result.ok is False
    assert result.error.category is ErrorCategory.RESPONSE_PARSE_ERROR


def test_an_empty_extraction_is_not_reported_as_success():
    # Arrange - a blank page and a failed read must stay distinguishable
    payload = {
        "document_type": "",
        "summary": "",
        "visible_text": "   ",
        "fields": [],
        "tables": [],
        "uncertainties": [],
    }

    # Act
    result = _extract(_client(lambda request: _reply(payload)))

    # Assert
    assert result.ok is False
    assert result.error.category is ErrorCategory.RESPONSE_PARSE_ERROR


def test_unreadable_values_are_preserved_not_inferred():
    # Arrange - an invented amount in an audit file is the worst failure this pipeline has
    payload = dict(_VALID_PAYLOAD)
    payload["fields"] = [{"label": "Amount", "value": "[UNREADABLE]", "confidence": "low"}]

    # Act
    result = _extract(_client(lambda request: _reply(payload)))

    # Assert
    assert result.extraction.fields[0].value == "[UNREADABLE]"
    assert "[UNREADABLE]" in render_markdown(result.extraction)


def test_structured_output_renders_to_the_markdown_contract():
    # Arrange - SPEC-01 req 3 asks for structured Markdown on disk; JSON is only the wire format
    extraction = VisionExtraction(
        document_type="Bank statement",
        summary="One month of transactions.",
        visible_text="STATEMENT OF ACCOUNT",
        fields=(ExtractedField("Account", "0012345", "high"),),
        tables=(
            ExtractedTable(
                caption="Transactions",
                columns=("Date", "Amount"),
                rows=(("01-04-2025", "1,000"),),
            ),
        ),
        uncertainties=("The closing balance is partly obscured.",),
    )

    # Act
    markdown = render_markdown(extraction)

    # Assert
    assert "## Visible Text" in markdown
    assert "## Fields and Values" in markdown
    assert "## Tables" in markdown
    assert "## Uncertainties" in markdown
    assert "| Date | Amount |" in markdown
    assert "0012345" in markdown, "a leading zero must survive rendering"


def test_rendered_markdown_escapes_pipes_so_a_table_cannot_be_broken():
    # Arrange - a value containing a pipe would silently corrupt the table structure
    extraction = VisionExtraction(
        document_type="x",
        summary="",
        visible_text="x",
        fields=(),
        tables=(ExtractedTable("t", ("A",), (("has | pipe",),)),),
        uncertainties=(),
    )

    # Act
    markdown = render_markdown(extraction)

    # Assert
    assert r"has \| pipe" in markdown


# --- retry and backoff ------------------------------------------------------------------------


def test_vision_client_retries_on_429_then_503_and_records_backoff():
    # Arrange - backoff is asserted from a recorded sleep sequence, never from wall-clock time
    statuses = [429, 503, 200]
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        if status == 200:
            return _reply(_VALID_PAYLOAD)
        return httpx.Response(status)

    # Act
    result = _extract(_client(handler, sleeps=sleeps))

    # Assert
    assert result.ok is True
    assert len(sleeps) == 2
    assert sleeps[0] < sleeps[1], "backoff must grow between attempts"


def test_retry_after_header_is_honoured():
    # Arrange - a server that tells us when to come back is obeyed rather than guessed at
    statuses = [429, 200]
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if statuses.pop(0) == 200:
            return _reply(_VALID_PAYLOAD)
        return httpx.Response(429, headers={"Retry-After": "7"})

    # Act
    _extract(_client(handler, sleeps=sleeps))

    # Assert
    assert sleeps == [7.0]


@pytest.mark.parametrize("status", [400, 401, 403, 413])
def test_vision_client_does_not_retry_client_errors(status):
    # Arrange - retrying a rejected request just spends money to fail again
    attempts = {"count": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(status)

    # Act
    result = _extract(_client(handler, sleeps=sleeps))

    # Assert
    assert attempts["count"] == 1
    assert sleeps == []
    assert result.ok is False
    assert result.error.category is ErrorCategory.API_ERROR


def test_attempts_are_bounded_and_the_failure_is_recorded():
    # Arrange - a permanently failing endpoint must not retry forever
    attempts = {"count": 0}
    settings = VisionSettings(api_key="k", max_attempts=3)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(503)

    # Act
    result = _extract(_client(handler, settings=settings, sleeps=[]))

    # Assert
    assert attempts["count"] == 3
    assert result.ok is False
    assert result.error.category is ErrorCategory.API_ERROR


def test_a_timeout_is_retried_then_recorded():
    # Arrange
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    # Act
    result = _extract(_client(handler, VisionSettings(api_key="k", max_attempts=2), sleeps))

    # Assert
    assert result.ok is False
    assert result.error.category is ErrorCategory.API_TIMEOUT
    assert len(sleeps) == 1


# --- credentials ---------------------------------------------------------------------------------


def test_api_key_is_sent_as_a_bearer_token_and_never_logged():
    # Arrange - the key arrives from .env only and must not reach any message
    secret = "sk-do-not-leak-me"
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(500)

    settings = VisionSettings(api_key=secret, max_attempts=1)

    # Act
    result = _extract(_client(handler, settings, sleeps=[]))

    # Assert
    assert seen["auth"] == f"Bearer {secret}"
    assert secret not in f"{result.error} {result!r} {settings!r}"


def test_settings_repr_does_not_expose_the_key():
    # Arrange - pydantic SecretStr, checked rather than assumed
    settings = VisionSettings(api_key="sk-do-not-leak-me")

    # Assert
    assert "sk-do-not-leak-me" not in repr(settings)


# --- preprocessing ------------------------------------------------------------------------------------


def test_oversized_image_is_downscaled_before_sending(tmp_path):
    # Arrange - bounding the longest edge bounds both request size and per-image cost
    from ca_agent.vision.preprocess import prepare_image

    payload = files.image_bytes("PNG", (4000, 2000))

    # Act
    prepared = prepare_image(payload, max_edge_pixels=1000)

    # Assert
    from PIL import Image

    with Image.open(__import__("io").BytesIO(prepared.content)) as image:
        assert max(image.size) == 1000
        assert image.size == (1000, 500), "aspect ratio must be preserved"
    assert prepared.media_type == "image/png"


def test_a_small_image_is_not_resampled(tmp_path):
    # Arrange - needless re-encoding loses detail the model needs
    from ca_agent.vision.preprocess import prepare_image

    payload = files.image_bytes("PNG", (64, 64))

    # Act
    prepared = prepare_image(payload, max_edge_pixels=1000)

    # Assert
    from PIL import Image

    with Image.open(__import__("io").BytesIO(prepared.content)) as image:
        assert image.size == (64, 64)


def test_a_bitmap_is_converted_to_png(tmp_path):
    # Arrange - the corpus holds bmp and gif, which many endpoints reject
    from ca_agent.vision.preprocess import prepare_image

    # Act
    prepared = prepare_image(files.image_bytes("BMP", (32, 32)), max_edge_pixels=1000)

    # Assert
    assert prepared.media_type == "image/png"
    assert prepared.content.startswith(b"\x89PNG")


def test_pdf_page_is_rasterised_at_the_configured_dpi(tmp_path):
    # Arrange - ADR-010: pypdfium2, never PyMuPDF
    from ca_agent.vision.preprocess import rasterise_pdf_page

    source = files.write_bytes(tmp_path / "one.pdf", files.text_pdf_bytes(["A page of text"]))

    # Act - US Letter is 8.5 x 11 inches, so 72 dpi is 612 x 792 pixels
    prepared = rasterise_pdf_page(source, page_number=1, dpi=72)

    # Assert
    from PIL import Image

    with Image.open(__import__("io").BytesIO(prepared.content)) as image:
        assert image.size == (612, 792)
    assert prepared.media_type == "image/png"


def test_the_image_is_sent_as_a_base64_data_url():
    # Arrange - the OpenAI-compatible shape every endpoint understands
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _reply(_VALID_PAYLOAD)

    payload = files.image_bytes()

    # Act
    _client(handler).extract(payload, media_type="image/png")

    # Assert
    content = seen["messages"][0]["content"]
    url = next(part["image_url"]["url"] for part in content if part["type"] == "image_url")
    assert url == f"data:image/png;base64,{base64.b64encode(payload).decode()}"


def test_temperature_is_zero_so_extraction_is_repeatable():
    # Arrange - a document either says a number or it does not
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _reply(_VALID_PAYLOAD)

    # Act
    _extract(_client(handler))

    # Assert
    assert seen["temperature"] == 0.0


# --- local endpoints and schema strictness (LM Studio regression) ---------------------------


def test_strict_is_omitted_by_default():
    """LM Studio rejects `strict` outright with HTTP 400 "terminated".

    Sending it unconditionally made every local endpoint unusable. Omitting it costs nothing,
    because the reply is validated against the same schema on arrival - that validation, not
    the flag, is what makes a failed extraction impossible to mistake for a success.
    """
    # Arrange
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _reply(_VALID_PAYLOAD)

    # Act
    _extract(_client(handler, VisionSettings(api_key=None, model="m")))

    # Assert
    schema_block = seen["response_format"]["json_schema"]
    assert "strict" not in schema_block
    assert schema_block["schema"] == RESPONSE_SCHEMA


def test_strict_is_sent_when_configured():
    # Arrange - OpenAI honours strict, so it stays available
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _reply(_VALID_PAYLOAD)

    # Act
    _extract(_client(handler, VisionSettings(api_key="k", strict_schema=True)))

    # Assert
    assert seen["response_format"]["json_schema"]["strict"] is True


def test_no_authorization_header_is_sent_without_a_key():
    # Arrange - a local server has no credential, and sending an empty bearer confuses some
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return _reply(_VALID_PAYLOAD)

    # Act
    result = _extract(_client(handler, VisionSettings(api_key=None)))

    # Assert
    assert seen["auth"] is None
    assert result.ok is True
