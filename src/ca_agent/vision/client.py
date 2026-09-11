"""Purpose: calls an OpenAI-compatible vision endpoint and returns a validated extraction, or
an explicit failure - never a maybe (SPEC-01 req 3). This is the only route that spends money,
so every decision here is about not spending it twice: retries are bounded and only for faults
that can plausibly clear, a rejected request is never retried, and a reply that fails the
contract is a failure rather than something to salvage. Transport is httpx with an injected
client (ADR-011) so retry and backoff are testable with no network at all.
"""

from __future__ import annotations

import base64
import random
from collections.abc import Callable
from dataclasses import dataclass

from ca_agent.config.settings import VisionSettings
from ca_agent.core.enums import ErrorCategory
from ca_agent.core.model import ErrorInfo
from ca_agent.vision.contract import (
    EXTRACTION_PROMPT,
    RESPONSE_SCHEMA,
    ContractError,
    VisionExtraction,
    parse_extraction,
)

_STAGE = "vision_extraction"
_ENDPOINT = "/chat/completions"
_SCHEMA_NAME = "document_extraction"
#: Faults that may clear on their own. Everything else is the request's own fault and retrying
#: it only spends money to fail again.
_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
#: Jitter spreads retries so a batch of workers does not synchronise into a second burst.
_JITTER_RATIO = 0.25


@dataclass(frozen=True, slots=True)
class VisionResult:
    """The outcome of one extraction. ``ok`` is true only if a contract-valid reply arrived."""

    ok: bool
    extraction: VisionExtraction | None = None
    error: ErrorInfo | None = None
    attempts: int = 0

    def __post_init__(self) -> None:
        if self.ok and self.extraction is None:
            raise ValueError("a successful vision result must carry an extraction")
        if not self.ok and self.error is None:
            raise ValueError("a failed vision result must carry an error")


class VisionClient:
    """Sends one image at a time to an OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        http_client,
        *,
        settings: VisionSettings,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        """Take the HTTP client rather than build one, so tests inject a mock transport."""
        self._http = http_client
        self._settings = settings
        self._sleep = sleep if sleep is not None else _default_sleep
        self._jitter = jitter if jitter is not None else random.random

    def extract(self, image: bytes, *, media_type: str) -> VisionResult:
        """Extract one image. Returns a failure rather than raising, so a batch continues."""
        import httpx

        request = self._request_body(image, media_type)
        headers = self._headers()
        url = self._settings.base_url.rstrip("/") + _ENDPOINT

        last_error: ErrorInfo | None = None
        for attempt in range(1, self._settings.max_attempts + 1):
            try:
                response = self._http.post(
                    url,
                    json=request,
                    headers=headers,
                    timeout=self._settings.request_timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.TransportError) as error:
                last_error = self._error(
                    ErrorCategory.API_TIMEOUT
                    if isinstance(error, httpx.TimeoutException)
                    else ErrorCategory.API_ERROR,
                    f"attempt {attempt}: {type(error).__name__}",
                )
                if attempt < self._settings.max_attempts:
                    self._back_off(attempt, None)
                    continue
                return VisionResult(ok=False, error=last_error, attempts=attempt)

            if response.status_code in _RETRYABLE_STATUSES:
                category = (
                    ErrorCategory.API_RATE_LIMITED
                    if response.status_code == 429
                    else ErrorCategory.API_ERROR
                )
                last_error = self._error(category, f"attempt {attempt}: HTTP {response.status_code}")
                if attempt < self._settings.max_attempts:
                    self._back_off(attempt, response.headers.get("Retry-After"))
                    continue
                return VisionResult(ok=False, error=last_error, attempts=attempt)

            if response.status_code >= 400:
                # A rejected request will be rejected again; retrying only spends money.
                return VisionResult(
                    ok=False,
                    error=self._error(
                        ErrorCategory.API_ERROR, f"HTTP {response.status_code}; not retried"
                    ),
                    attempts=attempt,
                )

            return self._parse(response, attempt)

        return VisionResult(
            ok=False,
            error=last_error or self._error(ErrorCategory.API_ERROR, "no attempt was made"),
            attempts=self._settings.max_attempts,
        )

    # --- request construction ------------------------------------------------------------

    def _request_body(self, image: bytes, media_type: str) -> dict:
        encoded = base64.b64encode(image).decode("ascii")
        return {
            "model": self._settings.model,
            # Zero temperature because a document either says a number or it does not; there is
            # nothing here worth sampling for.
            "temperature": self._settings.temperature,
            "max_tokens": self._settings.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": _schema_block(self._settings.strict_schema),
            },
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": EXTRACTION_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                        },
                    ],
                }
            ],
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = self._settings.api_key
        if key is not None:
            # get_secret_value is the only place the key is unwrapped, and it goes straight
            # into a header - never into a log line, a message or a record.
            headers["Authorization"] = f"Bearer {key.get_secret_value()}"
        return headers

    # --- response handling ---------------------------------------------------------------

    def _parse(self, response, attempt: int) -> VisionResult:
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            return VisionResult(
                ok=False,
                error=self._error(
                    ErrorCategory.RESPONSE_PARSE_ERROR, f"unexpected response shape: {error}"
                ),
                attempts=attempt,
            )

        try:
            extraction = parse_extraction(content if isinstance(content, str) else "")
        except ContractError as error:
            return VisionResult(
                ok=False,
                error=self._error(ErrorCategory.RESPONSE_PARSE_ERROR, str(error)),
                attempts=attempt,
            )

        if not extraction.has_content():
            # A well-formed but wholly empty object means the model did not read the document.
            # Reporting that as success would make a failed read indistinguishable from a
            # blank page, which SPEC-01 req 3 forbids.
            return VisionResult(
                ok=False,
                error=self._error(
                    ErrorCategory.RESPONSE_PARSE_ERROR,
                    "reply contained no text, fields or tables",
                ),
                attempts=attempt,
            )

        return VisionResult(ok=True, extraction=extraction, attempts=attempt)

    # --- backoff -----------------------------------------------------------------------------

    def _back_off(self, attempt: int, retry_after: str | None) -> None:
        """Wait before the next attempt, obeying the server when it says how long."""
        seconds = _parse_retry_after(retry_after)
        if seconds is None:
            base = self._settings.backoff_initial_seconds * (
                self._settings.backoff_multiplier ** (attempt - 1)
            )
            seconds = base * (1 + _JITTER_RATIO * self._jitter())
        self._sleep(seconds)

    def _error(self, category: ErrorCategory, message: str) -> ErrorInfo:
        return ErrorInfo(category=category, message=message, stage=_STAGE, reader="vision")


def _schema_block(strict: bool) -> dict:
    """The json_schema block, with `strict` present only when it was asked for.

    LM Studio rejects the flag outright with HTTP 400 "terminated", so sending it
    unconditionally makes every local endpoint unusable. Omitting it costs nothing here: the
    reply is validated against the same schema on arrival, which is what actually makes a
    failed extraction impossible to mistake for a successful one.
    """
    block: dict = {"name": _SCHEMA_NAME, "schema": RESPONSE_SCHEMA}
    if strict:
        block["strict"] = True
    return block


def _parse_retry_after(value: str | None) -> float | None:
    """Honour a numeric Retry-After. A date-formatted one is ignored in favour of backoff."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _default_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
