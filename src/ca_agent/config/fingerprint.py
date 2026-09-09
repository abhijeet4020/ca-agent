"""Purpose: SPEC-01 req 8 says a change to extraction configuration invalidates the affected
outputs, so the fingerprint is part of the correctness contract rather than a convenience.
Fingerprints are computed per config section - not once globally - so altering a vision setting
cannot invalidate thousands of unrelated Parquet files. Canonicalisation sorts keys, materialises
defaults and quantises floats so that a semantically identical config always hashes identically.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

#: Bump when the meaning of a field changes without its name changing. Doing so deliberately
#: invalidates every fingerprint, forcing a reprocess.
SCHEMA_VERSION = "1"

_FINGERPRINT_LENGTH = 16
_FLOAT_PRECISION = 6
_SEPARATOR = "\x1f"


def _quantise(value: Any) -> Any:
    """Normalise values whose textual form could otherwise drift between runs."""
    if isinstance(value, float):
        rounded = round(value, _FLOAT_PRECISION)
        # Collapse -0.0 to 0.0 so the two never produce different JSON.
        return rounded + 0.0
    if isinstance(value, dict):
        return {key: _quantise(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_quantise(item) for item in value]
    return value


def canonical_json(section_name: str, section: BaseModel) -> str:
    """Render a config section as the stable JSON text that gets hashed.

    Fields listed in the section's ``FINGERPRINT_EXCLUDE`` are omitted: credentials, machine
    specific paths, and settings that alter only retry behaviour rather than output content.
    """
    excluded = set(getattr(type(section), "FINGERPRINT_EXCLUDE", frozenset()))
    payload = _quantise(section.model_dump(mode="json", exclude=excluded))
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def section_fingerprint(section_name: str, section: BaseModel) -> str:
    """Stable short digest identifying this section's effective configuration.

    The section name is mixed in so two sections that happen to serialise identically still
    produce different fingerprints and cannot alias each other's reuse decisions.
    """
    raw = _SEPARATOR.join((SCHEMA_VERSION, section_name, canonical_json(section_name, section)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_FINGERPRINT_LENGTH]
