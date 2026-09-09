"""Tests for configuration loading and reuse fingerprinting (TP-01 Group B).

SPEC-01 requirement 8 makes the processing configuration part of the correctness contract:
a change must invalidate the affected outputs, and an unrelated change must not. That makes
fingerprint stability a tested property rather than an implementation detail.
"""

import pytest

from ca_agent.config.fingerprint import canonical_json, section_fingerprint
from ca_agent.config.settings import ConfigError, PipelineSettings, load_settings

_SECTION_NAMES = ("detection", "tabular", "text", "chunking", "pdf", "vision", "archive")


def _settings(**overrides) -> PipelineSettings:
    return PipelineSettings.model_validate(overrides)


def test_section_fingerprint_is_stable_across_key_order_and_defaults():
    # Arrange - same values, different declaration order, and one relying on a default
    default_chunk_size = PipelineSettings().chunking.chunk_size
    ordered = _settings(chunking={"chunk_size": default_chunk_size, "chunk_overlap": 120})
    reversed_order = _settings(chunking={"chunk_overlap": 120, "chunk_size": default_chunk_size})
    omitting_default = _settings(chunking={"chunk_overlap": 120})

    # Act
    fingerprints = {
        section_fingerprint("chunking", candidate.chunking)
        for candidate in (ordered, reversed_order, omitting_default)
    }

    # Assert
    assert len(fingerprints) == 1, "key order and explicit defaults must not shift the hash"


def test_changing_one_section_does_not_change_other_section_fingerprints():
    # Arrange
    baseline = _settings()
    changed = _settings(vision={"temperature": 0.7})

    # Act
    baseline_prints = {name: section_fingerprint(name, getattr(baseline, name)) for name in _SECTION_NAMES}
    changed_prints = {name: section_fingerprint(name, getattr(changed, name)) for name in _SECTION_NAMES}

    # Assert
    assert changed_prints["vision"] != baseline_prints["vision"]
    unaffected = set(_SECTION_NAMES) - {"vision"}
    for name in unaffected:
        assert changed_prints[name] == baseline_prints[name], f"{name} must not be invalidated"


def test_api_key_is_excluded_from_fingerprint_and_never_serialised():
    # Arrange - rotating a credential must not invalidate thousands of outputs
    first = _settings(vision={"api_key": "sk-first-key-value"})
    second = _settings(vision={"api_key": "sk-second-key-value"})

    # Act
    payload = canonical_json("vision", first.vision)

    # Assert
    assert section_fingerprint("vision", first.vision) == section_fingerprint("vision", second.vision)
    assert "sk-first-key-value" not in payload
    assert "api_key" not in payload


def test_secret_is_not_exposed_by_repr():
    # Arrange
    settings = _settings(vision={"api_key": "sk-should-not-leak"})

    # Act
    rendered = f"{settings.vision!r} {settings.vision.model_dump_json()}"

    # Assert
    assert "sk-should-not-leak" not in rendered


def test_fingerprint_changes_when_a_value_changes():
    # Arrange
    baseline = _settings()
    changed = _settings(archive={"max_depth": 2})

    # Act / Assert
    assert section_fingerprint("archive", changed.archive) != section_fingerprint(
        "archive", baseline.archive
    )


def test_fingerprint_is_namespaced_by_section_name():
    # Arrange - two sections that happen to serialise identically must not share a hash
    settings = _settings()

    # Act
    as_tabular = section_fingerprint("tabular", settings.tabular)
    as_other = section_fingerprint("structured", settings.tabular)

    # Assert
    assert as_tabular != as_other


def test_archive_defaults_match_the_approved_depth_policy():
    # Arrange / Act - the depth-3 policy resolves the specification Open Decision
    settings = PipelineSettings()

    # Assert
    assert settings.archive.max_depth == 3
    assert settings.archive.max_total_expanded_bytes > 0
    assert settings.archive.max_member_count > 0
    assert settings.archive.max_compression_ratio > 1


def test_unknown_config_key_is_rejected():
    # Arrange / Act / Assert - a typo must fail loudly, not silently shift the fingerprint
    with pytest.raises(ConfigError):
        load_settings(toml_data={"archive": {"max_dept": 3}})


def test_missing_api_key_raises_at_startup_not_at_first_call():
    # Arrange - a run over thousands of files must not fail hours in
    with pytest.raises(ConfigError) as excinfo:
        load_settings(toml_data={"vision": {"enabled": True}}, env={})

    # Assert
    assert "CAAGENT__VISION__API_KEY" in str(excinfo.value)


def test_disabled_vision_does_not_require_a_key():
    # Arrange / Act
    settings = load_settings(toml_data={"vision": {"enabled": False}}, env={})

    # Assert
    assert settings.vision.enabled is False


def test_environment_overrides_toml_values():
    # Arrange
    env = {"CAAGENT__ARCHIVE__MAX_DEPTH": "2", "CAAGENT__VISION__API_KEY": "sk-env"}

    # Act
    settings = load_settings(toml_data={"archive": {"max_depth": 3}}, env=env)

    # Assert
    assert settings.archive.max_depth == 2
    assert settings.vision.api_key.get_secret_value() == "sk-env"


def test_float_values_do_not_destabilise_the_fingerprint():
    # Arrange - the same numeric value written two ways must hash identically
    from_int = _settings(vision={"temperature": 0})
    from_float = _settings(vision={"temperature": 0.0})

    # Act / Assert
    assert section_fingerprint("vision", from_int.vision) == section_fingerprint(
        "vision", from_float.vision
    )
