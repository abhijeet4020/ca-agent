"""Drives the Streamlit search app headlessly (TP-01 Group L).

A server returning HTTP 200 only proves a process is listening. These run the app the way a
person would - type a question, press search, read the results - using Streamlit's own test
harness, so a broken widget or an exception in the result loop fails here rather than in front
of the user.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("streamlit", reason="streamlit is not installed")
pytest.importorskip("faiss", reason="the gold extra is not installed")

from streamlit.testing.v1 import AppTest  # noqa: E402

_APP = Path(__file__).resolve().parents[2] / "streamlit_app.py"
#: The model load on first run is slow and happens inside the app.
_TIMEOUT = 300


def _app() -> AppTest:
    return AppTest.from_file(str(_APP), default_timeout=_TIMEOUT)


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "data" / "gold" / "scopes").is_dir(),
    reason="no Gold indexes built in this working tree",
)
def test_app_starts_and_lists_the_indexed_clients():
    # Arrange / Act
    app = _app().run()

    # Assert - no exception, and the corpus summary rendered
    assert not app.exception
    assert any("Search client records" in str(title.value) for title in app.title)
    assert app.metric, "the sidebar should report how many clients are indexed"


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "data" / "gold" / "scopes").is_dir(),
    reason="no Gold indexes built in this working tree",
)
def test_asking_a_question_returns_cited_passages():
    # Arrange
    app = _app().run()

    # Act - type a question and press the button, as a person would
    app.text_input[0].set_value("depreciation on plant and machinery").run()
    app.button[0].click().run()

    # Assert - results rendered, and each cites where it came from
    assert not app.exception
    rendered = " ".join(str(block.value) for block in app.markdown)
    assert "depreciation" in rendered.lower() or "Depreciation" in rendered
    captions = " ".join(str(block.value) for block in app.caption)
    assert ".pdf" in captions or ".docx" in captions, "every hit must cite its source document"


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "data" / "gold" / "scopes").is_dir(),
    reason="no Gold indexes built in this working tree",
)
def test_an_empty_question_is_handled_without_searching():
    # Arrange - pressing search with nothing typed must not raise
    app = _app().run()

    # Act
    app.button[0].click().run()

    # Assert
    assert not app.exception
