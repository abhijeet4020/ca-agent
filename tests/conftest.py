"""Purpose: shared test fixtures for the Silver-layer suite. The central one is a network
kill-switch: SPEC-01's vision route talks to a paid API, so "no test makes a real call" is
enforced here by making socket connection raise, rather than trusting every future test author
to inject a mock. Tests that exercise HTTP inject httpx.MockTransport instead.
"""

from __future__ import annotations

import socket

import pytest

_REAL_CONNECT = socket.socket.connect


class NetworkAccessAttempted(AssertionError):
    """Raised when a test tries to open a real network connection."""


@pytest.fixture(autouse=True)
def _block_network(monkeypatch, request):
    """Fail any test that attempts a real outbound connection.

    Marked tests can opt out with @pytest.mark.allow_network, which nothing in the Phase 1
    suite uses; it exists so an opt-out is explicit and greppable rather than accidental.
    """
    if request.node.get_closest_marker("allow_network"):
        return

    def _refuse(self, address):  # noqa: ANN001 - signature must match socket.connect
        raise NetworkAccessAttempted(
            f"test attempted a real network connection to {address}; inject a mock transport"
        )

    monkeypatch.setattr(socket.socket, "connect", _refuse)


@pytest.fixture
def silver_paths(tmp_path):
    """A SilverPaths rooted in an isolated temporary output tree."""
    from ca_agent.storage.paths import SilverPaths

    return SilverPaths(tmp_path / "output")
