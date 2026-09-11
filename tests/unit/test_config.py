"""Tests for configuration loading (TP-01 Group B).

Configuration is where a long run either fails in its first second or hours in, so these pin
the two things that decide that: a credential that is genuinely required is demanded up front,
and one that is not - a local model server has none to give - never blocks the run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.config.settings import ConfigError, load_settings  # noqa: E402

# --- local endpoints and .env loading (LM Studio regression) --------------------------------


def test_a_local_vision_endpoint_needs_no_api_key():
    """LM Studio, Ollama and vLLM have no credential to give.

    Requiring one blocked the cheapest way to run the vision route entirely.
    """
    # Arrange / Act
    settings = load_settings(
        toml_data={"vision": {"enabled": True, "base_url": "http://localhost:1234/v1"}},
        env={},
    )

    # Assert
    assert settings.vision.api_key is None
    assert settings.vision.base_url == "http://localhost:1234/v1"


def test_a_hosted_vision_endpoint_still_fails_fast_without_a_key():
    # Arrange - the original fail-fast intent survives for remote endpoints
    with pytest.raises(ConfigError, match="no API key"):
        load_settings(
            toml_data={"vision": {"enabled": True, "base_url": "https://api.openai.com/v1"}},
            env={},
        )


def test_dotenv_is_loaded_when_no_environment_is_supplied(tmp_path):
    """environment.bat runs in a cmd.exe subprocess, so from PowerShell its `set` commands

    never reach the parent environment. A configured .env was therefore silently ignored and
    the defaults used instead, with no error explaining it. The application loads it directly.
    """
    # Arrange
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        '# a comment\n'
        'CAAGENT__VISION__BASE_URL="http://localhost:1234/v1"\n'
        "CAAGENT__VISION__MODEL=google/gemma-3-4b\n"
        "NOT_OURS=ignored\n",
        encoding="utf-8",
    )

    # Act
    settings = load_settings(dotenv_path=dotenv)

    # Assert
    assert settings.vision.base_url == "http://localhost:1234/v1"
    assert settings.vision.model == "google/gemma-3-4b"


def test_a_real_environment_variable_beats_the_dotenv_file(tmp_path, monkeypatch):
    # Arrange - standard dotenv precedence: an exported value always wins
    dotenv = tmp_path / ".env"
    dotenv.write_text("CAAGENT__VISION__MODEL=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("CAAGENT__VISION__MODEL", "from-environment")
    monkeypatch.setenv("CAAGENT__VISION__BASE_URL", "http://localhost:1234/v1")

    # Act
    settings = load_settings(dotenv_path=dotenv)

    # Assert
    assert settings.vision.model == "from-environment"
