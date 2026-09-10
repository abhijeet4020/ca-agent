"""Tests for client-supplied document credentials (TP-01 Group I, ADR-008 amendment).

352 corpus PDFs are genuinely locked, and 213 of them are AIS and TIS filings - the richest
income data the firm holds. The Income Tax portal derives their password from the client's own
PAN and date of birth, which the firm already has, so supplying it is not guessing. The rules
these tests pin are the ones that keep that true: credentials come only from a file the user
wrote, they are scoped to one client, and they never reach an output.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.config.credentials import (  # noqa: E402
    CredentialError,
    load_client_credentials,
)
from ca_agent.core.scope import ClientScope  # noqa: E402

_CREDENTIALS = """
[clients."Business Clients/EXAMPLE TRADING COMPANY"]
pan = "AAAAA0000A"
date_of_birth = "1980-01-01"

[clients."Salary Clients/EXAMPLE SALARIED CLIENT"]
pan = "BBBBB1111B"
date_of_birth = "1990-12-31"
extra_passwords = ["bankstatement2026"]
"""


def _scope(category: str, client: str) -> ClientScope:
    return ClientScope(
        category=category, client=client, scope_root=PurePosixPath(f"{category}/{client}")
    )


def _store(tmp_path: Path, body: str = _CREDENTIALS):
    path = tmp_path / "credentials.toml"
    path.write_text(body, encoding="utf-8")
    return load_client_credentials(path)


# --- derivation -----------------------------------------------------------------------------


def test_ais_password_is_derived_from_the_supplied_pan_and_date_of_birth(tmp_path):
    # Arrange - the portal's documented scheme is lowercase PAN followed by DDMMYYYY
    store = _store(tmp_path)

    # Act
    passwords = store.passwords_for(_scope("Business Clients", "EXAMPLE TRADING COMPANY"))

    # Assert
    assert "aaaaa0000a01011980" in passwords


def test_extra_passwords_are_offered_alongside_the_derived_one(tmp_path):
    # Arrange - bank statements use their own scheme, so the firm can supply literals too
    store = _store(tmp_path)

    # Act
    passwords = store.passwords_for(_scope("Salary Clients", "EXAMPLE SALARIED CLIENT"))

    # Assert
    assert "bbbbb1111b31121990" in passwords
    assert "bankstatement2026" in passwords


# --- scoping (requirement 7) --------------------------------------------------------------------


def test_credentials_are_resolved_per_client_scope(tmp_path):
    # Arrange - one client's credential must never be tried against another client's document
    store = _store(tmp_path)

    # Act
    first = store.passwords_for(_scope("Business Clients", "EXAMPLE TRADING COMPANY"))
    second = store.passwords_for(_scope("Salary Clients", "EXAMPLE SALARIED CLIENT"))

    # Assert
    assert set(first).isdisjoint(second)


def test_an_unknown_scope_resolves_to_no_passwords(tmp_path):
    # Arrange / Act
    store = _store(tmp_path)

    # Assert
    assert store.passwords_for(_scope("LLP", "SOME OTHER CLIENT")) == ()


def test_same_client_name_in_two_categories_does_not_share_credentials(tmp_path):
    # Arrange - LIC Employees exists under two categories in the real corpus
    body = """
[clients."Cooperative Audits/EXAMPLE SHARED NAME"]
pan = "AAAPL1111A"
date_of_birth = "1980-01-01"
"""
    store = _store(tmp_path, body)

    # Act
    matched = store.passwords_for(_scope("Cooperative Audits", "EXAMPLE SHARED NAME"))
    other = store.passwords_for(_scope("GST Proprietor", "EXAMPLE SHARED NAME"))

    # Assert
    assert matched
    assert other == ()


# --- optional and safe ------------------------------------------------------------------------------


def test_a_missing_credential_file_is_not_an_error():
    # Arrange / Act - the file is optional; a firm supplying none still gets a complete run
    store = load_client_credentials(None)

    # Assert
    assert store.is_empty()
    assert store.passwords_for(_scope("LLP", "ANY")) == ()


def test_a_nonexistent_path_is_reported_clearly(tmp_path):
    # Arrange / Act / Assert - a configured-but-missing file is a mistake worth failing on
    try:
        load_client_credentials(tmp_path / "absent.toml")
    except CredentialError as error:
        assert "absent.toml" in str(error)
    else:
        raise AssertionError("a configured credential file that does not exist must be reported")


def test_a_malformed_date_of_birth_is_reported_not_silently_skipped(tmp_path):
    # Arrange - a typo here would silently leave 213 filings locked with no explanation
    body = """
[clients."LLP/A CLIENT"]
pan = "AAAPL1111A"
date_of_birth = "12-04-1985"
"""

    # Act / Assert
    try:
        _store(tmp_path, body)
    except CredentialError as error:
        assert "date_of_birth" in str(error)
    else:
        raise AssertionError("an unparseable date must be reported, not ignored")


def test_the_store_never_renders_a_password_in_its_repr(tmp_path):
    # Arrange - these objects end up in tracebacks and debug output
    store = _store(tmp_path)

    # Act
    rendered = repr(store)

    # Assert
    assert "AAAAA0000A" not in rendered
    assert "aaaaa0000a01011980" not in rendered
    assert "bankstatement2026" not in rendered
