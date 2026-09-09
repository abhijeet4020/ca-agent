"""Tests for client-scope identity and resolution (TP-01 Group A).

SPEC-01 requirement 7 makes the category/client pair the unit of isolation for
deduplication and indexing, so these tests guard the two traps found in the real
corpus: duplicate client names across categories, and the Mauli Hospital backup
root which has no client directory level.
"""

from pathlib import PurePosixPath

import pytest

from ca_agent.catalog.scope_resolver import ScopeResolver, discover_scopes
from ca_agent.core.enums import ErrorCategory
from ca_agent.core.scope import ClientScope

_LIC = "LIC EMPLOYEES CO OP CREDIT SOCIETY SATARA"
_MAULI = "Mauli Hospital Tally Back up"


def _scope(category: str, client: str, *, category_is_scope: bool = False) -> ClientScope:
    root = PurePosixPath(category) if category_is_scope else PurePosixPath(category) / client
    return ClientScope(
        category=category,
        client=client,
        scope_root=root,
        category_is_scope=category_is_scope,
    )


def test_scope_id_is_stable_and_distinct_per_category():
    # Arrange
    cooperative = _scope("Cooperative Audits", _LIC)
    proprietor = _scope("GST Proprietor", _LIC)

    # Act
    first_id = cooperative.scope_id
    repeated_id = _scope("Cooperative Audits", _LIC).scope_id

    # Assert
    assert first_id == repeated_id, "scope_id must be deterministic across construction"
    assert cooperative.scope_id != proprietor.scope_id
    assert cooperative != proprietor


def test_scope_id_is_filesystem_safe():
    # Arrange - real category names contain spaces and the corpus contains Devanagari
    scope = _scope("GST TDS Governement Clients", "SHRI HANUMAN & GANESH (SANTHA)")

    # Act
    scope_id = scope.scope_id

    # Assert
    assert all(character.isalnum() or character == "_" for character in scope_id)


def test_scope_resolver_maps_client_directory_to_scope():
    # Arrange
    target = _scope("Business Clients", "AMIT SURYAKANT DHAMAL")
    resolver = ScopeResolver([target, _scope("LLP", "SUMILON PROPERTIES LLP")])

    # Act
    resolved = resolver.resolve(
        PurePosixPath("Business Clients/AMIT SURYAKANT DHAMAL/AY 18-19/ITRV.pdf")
    )

    # Assert
    assert resolved == target
    assert resolved.category == "Business Clients"
    assert resolved.client == "AMIT SURYAKANT DHAMAL"


def test_mauli_hospital_backup_root_is_one_client_scope():
    # Arrange - the category directory is itself the client scope
    mauli = _scope(_MAULI, _MAULI, category_is_scope=True)
    resolver = ScopeResolver([mauli])

    # Act
    resolved = resolver.resolve(PurePosixPath(f"{_MAULI}/Data/10001/Company.900"))

    # Assert
    assert resolved == mauli
    assert resolved.scope_root == PurePosixPath(_MAULI)


def test_resolver_prefers_longest_matching_prefix():
    # Arrange - a client directory nested under another registered root must win
    outer = _scope(_MAULI, _MAULI, category_is_scope=True)
    inner = _scope(_MAULI, "Data")
    resolver = ScopeResolver([outer, inner])

    # Act
    resolved = resolver.resolve(PurePosixPath(f"{_MAULI}/Data/10001/Company.900"))

    # Assert
    assert resolved == inner


def test_unresolvable_path_is_recorded_not_guessed():
    # Arrange
    resolver = ScopeResolver([_scope("Business Clients", "AMIT SURYAKANT DHAMAL")])
    stray = PurePosixPath("Unknown Category/stray.pdf")

    # Act
    resolved = resolver.resolve(stray)
    category = resolver.unresolved_error_category()

    # Assert
    assert resolved is None, "an unknown path must never be guessed into a scope"
    assert category is ErrorCategory.UNSCOPED_PATH


def test_resolver_rejects_absolute_paths():
    # Arrange
    resolver = ScopeResolver([_scope("Business Clients", "AMIT SURYAKANT DHAMAL")])

    # Act / Assert - fail fast rather than silently mis-resolving
    with pytest.raises(ValueError):
        resolver.resolve(PurePosixPath("/Business Clients/AMIT SURYAKANT DHAMAL/x.pdf"))


def test_discover_scopes_treats_category_as_scope_when_flagged(tmp_path):
    # Arrange
    (tmp_path / "Business Clients" / "ACME").mkdir(parents=True)
    (tmp_path / "LLP" / "ACME").mkdir(parents=True)
    (tmp_path / _MAULI / "Data").mkdir(parents=True)

    # Act
    scopes = discover_scopes(tmp_path, category_is_scope=frozenset({_MAULI}))

    # Assert
    roots = {str(scope.scope_root) for scope in scopes}
    assert roots == {"Business Clients/ACME", "LLP/ACME", _MAULI}
    assert len({scope.scope_id for scope in scopes}) == 3


def test_discover_scopes_ignores_empty_category(tmp_path):
    # Arrange - the real corpus has an empty "GST Audit Clients" directory
    (tmp_path / "GST Audit Clients").mkdir(parents=True)
    (tmp_path / "LLP" / "ACME").mkdir(parents=True)

    # Act
    scopes = discover_scopes(tmp_path, category_is_scope=frozenset())

    # Assert
    assert [scope.scope_root for scope in scopes] == [PurePosixPath("LLP/ACME")]
