"""Purpose: holds every tunable that governs Silver-layer processing, kept strictly separate
from runtime logic so a config change is a data change rather than a code change. Settings are
layered TOML then environment then explicit overrides, validated with extra-forbid so a typo
fails loudly instead of silently altering a reuse fingerprint. Credentials arrive only from the
environment and are wrapped in SecretStr so they never reach disk or a log line.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import tomli
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

_ENV_PREFIX = "CAAGENT__"
_ENV_DELIMITER = "__"
_GIGABYTE = 1024 * 1024 * 1024


class ConfigError(Exception):
    """Raised when configuration is invalid or incomplete. Always raised during load."""


class _Section(BaseModel):
    """Base for all config sections. Unknown keys are rejected, values are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Fields omitted from the reuse fingerprint (SPEC-01 req 8).
    FINGERPRINT_EXCLUDE: ClassVar[frozenset[str]] = frozenset()


class DetectionSettings(_Section):
    """Signature sniffing. Extension is evidence, never the routing decision."""

    signature_sample_bytes: int = Field(default=4096, gt=0)
    text_probe_bytes: int = Field(default=65536, gt=0)
    treat_extension_conflict_as_failure: bool = False


class TabularSettings(_Section):
    """Spreadsheet and delimited-text conversion to Parquet (SPEC-01 req 1)."""

    # Round-trip identity is what preserves leading zeros in PAN, GSTIN and account numbers.
    require_round_trip_for_typing: bool = True
    infer_column_types: bool = True
    parquet_compression: str = "snappy"
    csv_delimiter_candidates: tuple[str, ...] = (",", ";", "\t", "|")
    csv_sniff_bytes: int = Field(default=8192, gt=0)
    # The audit reopens and re-walks the whole workbook a second time (data_only=False) to find
    # formula cells with no cached value. That doubles per-file cost across a large batch; disable
    # it to trade the FORMULA_NO_CACHED_VALUE warning for faster runs.
    audit_uncached_formulas: bool = True


class TextSettings(_Section):
    """Text extraction from document formats (SPEC-01 req 2)."""

    encoding_fallbacks: tuple[str, ...] = ("utf-8", "utf-16", "cp1252", "latin-1")
    normalise_whitespace: bool = True


class ChunkingSettings(_Section):
    """Chunk production. Embedding of these chunks is Gold-layer work, not Phase 1."""

    chunk_size: int = Field(default=1000, gt=0)
    chunk_overlap: int = Field(default=120, ge=0)
    min_chunk_characters: int = Field(default=1, ge=1)


class PdfSettings(_Section):
    """Per-page text-versus-scanned classification (SPEC-01 req 2 versus req 3)."""

    min_text_characters_for_text_page: int = Field(default=120, ge=0)
    scanned_image_coverage_ratio: float = Field(default=0.5, ge=0.0, le=1.0)
    empty_page_max_characters: int = Field(default=20, ge=0)
    empty_page_max_coverage_ratio: float = Field(default=0.05, ge=0.0, le=1.0)
    raster_dpi: int = Field(default=250, gt=0)
    # A mixed page keeps its native text alongside vision output only when the two differ.
    native_text_duplicate_jaccard: float = Field(default=0.5, ge=0.0, le=1.0)


class VisionSettings(_Section):
    """Vision extraction against an OpenAI-compatible endpoint (SPEC-01 req 3)."""

    FINGERPRINT_EXCLUDE: ClassVar[frozenset[str]] = frozenset(
        {
            "api_key",
            "request_timeout_seconds",
            "max_attempts",
            "backoff_initial_seconds",
            "backoff_multiplier",
            "max_concurrency",
        }
    )

    enabled: bool = True
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: SecretStr | None = None
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=4096, gt=0)
    max_image_edge_pixels: int = Field(default=2000, gt=0)
    prompt_version: str = "1"

    request_timeout_seconds: float = Field(default=120.0, gt=0)
    max_attempts: int = Field(default=5, ge=1)
    backoff_initial_seconds: float = Field(default=0.5, gt=0)
    backoff_multiplier: float = Field(default=2.0, gt=1)
    max_concurrency: int = Field(default=4, ge=1)


class ArchiveSettings(_Section):
    """Archive expansion limits. Depth 3 resolves the specification Open Decision."""

    max_depth: int = Field(default=3, ge=1)
    max_total_expanded_bytes: int = Field(default=2 * _GIGABYTE, gt=0)
    max_member_count: int = Field(default=10_000, gt=0)
    # Checked while streaming, because a declared member size cannot be trusted.
    max_compression_ratio: float = Field(default=100.0, gt=1)


class StructuredSettings(_Section):
    """Structural routing of JSON and XML (SPEC-01 req 6)."""

    min_collection_size: int = Field(default=3, ge=1)
    min_key_jaccard: float = Field(default=0.8, ge=0.0, le=1.0)
    min_object_element_ratio: float = Field(default=0.9, ge=0.0, le=1.0)
    min_scalar_leaf_ratio: float = Field(default=0.7, ge=0.0, le=1.0)
    max_element_depth: int = Field(default=2, ge=1)
    # A unit is grouped by top-level field path, but a Tally register has a single root child
    # holding the whole document - one corpus export renders to 56 million characters. Units are
    # therefore split at this size so memory and chunk addressing stay bounded.
    max_unit_characters: int = Field(default=100_000, gt=0)
    # Some Tally exports embed raw control characters that no XML parser will accept. Retrying
    # in recovery mode salvages real master data instead of failing the whole file; the fallback
    # is always recorded as a warning so a recovered parse is never mistaken for a clean one.
    recover_malformed_xml: bool = True


class EmbeddingSettings(_Section):
    """Reserved for the Gold layer. Declared now so its fingerprint exists; unused in Phase 1."""

    enabled: bool = False
    provider: str = "sentence-transformers"
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    dimension: int = Field(default=384, gt=0)


class ExternalToolSettings(_Section):
    """Optional external converters. Absent tools yield NO_COMPATIBLE_READER, never a skip."""

    FINGERPRINT_EXCLUDE: ClassVar[frozenset[str]] = frozenset({"soffice_path", "unrar_path"})

    soffice_path: str | None = None
    unrar_path: str | None = None


class PathSettings(_Section):
    """Machine-specific locations. Excluded from every fingerprint by construction."""

    raw_root: Path = Path("raw_data")
    output_root: Path = Path("data")


class PipelineSettings(BaseModel):
    """Root configuration object. One section per independently fingerprinted concern."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    paths: PathSettings = PathSettings()
    detection: DetectionSettings = DetectionSettings()
    tabular: TabularSettings = TabularSettings()
    text: TextSettings = TextSettings()
    chunking: ChunkingSettings = ChunkingSettings()
    pdf: PdfSettings = PdfSettings()
    vision: VisionSettings = VisionSettings()
    archive: ArchiveSettings = ArchiveSettings()
    structured: StructuredSettings = StructuredSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    external_tools: ExternalToolSettings = ExternalToolSettings()


def _nest_environment(env: Mapping[str, str]) -> dict[str, Any]:
    """Turn CAAGENT__SECTION__KEY variables into a nested mapping."""
    nested: dict[str, Any] = {}
    for raw_key, value in env.items():
        if not raw_key.startswith(_ENV_PREFIX) or value == "":
            continue
        parts = raw_key[len(_ENV_PREFIX) :].split(_ENV_DELIMITER)
        if len(parts) != 2:
            continue
        section, field_name = (part.lower() for part in parts)
        nested.setdefault(section, {})[field_name] = value
    return nested


def _merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Two-level merge: sections are merged per key rather than replaced wholesale."""
    merged: dict[str, Any] = {
        key: dict(value) if isinstance(value, dict) else value for key, value in base.items()
    }
    for section, values in overlay.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return merged


def load_settings(
    config_path: Path | None = None,
    *,
    toml_data: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> PipelineSettings:
    """Load, layer and validate configuration.

    Precedence is defaults, then TOML, then environment, then explicit overrides. Validation
    and the credential check both happen here so a long run fails in its first second rather
    than hours in.
    """
    if toml_data is not None and config_path is not None:
        raise ConfigError("pass either config_path or toml_data, not both")

    layered: dict[str, Any] = _read_toml(config_path) if config_path else dict(toml_data or {})
    layered = _merge(layered, _nest_environment(os.environ if env is None else env))
    if overrides:
        layered = _merge(layered, overrides)

    try:
        settings = PipelineSettings.model_validate(layered)
    except ValidationError as error:
        raise ConfigError(f"invalid pipeline configuration: {error}") from error

    _require_vision_credentials(settings)
    return settings


def _read_toml(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("rb") as handle:
            return tomli.load(handle)
    except OSError as error:
        raise ConfigError(f"cannot read config file {config_path}: {error}") from error
    except tomli.TOMLDecodeError as error:
        raise ConfigError(f"malformed TOML in {config_path}: {error}") from error


def _require_vision_credentials(settings: PipelineSettings) -> None:
    """Fail fast when the vision route is enabled without a credential."""
    if settings.vision.enabled and settings.vision.api_key is None:
        raise ConfigError(
            "vision route is enabled but no API key was supplied; set "
            f"{_ENV_PREFIX}VISION{_ENV_DELIMITER}API_KEY in the environment or .env"
        )
