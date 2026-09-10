"""Purpose: turns client credentials the firm already holds into candidate document passwords
(ADR-008 amendment). 352 corpus PDFs are genuinely locked and 213 are AIS or TIS filings, whose
password the Income Tax portal derives from the client's own PAN and date of birth. Supplying a
value the firm holds is not guessing, so this module reads only an explicitly configured file
and never invents a candidate. Credentials are keyed by client scope, because requirement 7
makes scopes independent, and are kept out of every repr so they cannot leak into a traceback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ca_agent.core.scope import ClientScope

#: The Income Tax portal's documented scheme for AIS, TIS and Form 26AS: the PAN in lowercase
#: followed by the date of birth as DDMMYYYY, with no separator.
_DOB_PASSWORD_FORMAT = "%d%m%Y"


class CredentialError(Exception):
    """The credential file is configured but unusable. Always raised at load time."""


@dataclass(frozen=True, slots=True)
class ClientCredentials:
    """What the firm holds for one client. Never rendered, never logged."""

    pan: str | None = None
    date_of_birth: date | None = None
    extra_passwords: tuple[str, ...] = ()

    def candidates(self) -> tuple[str, ...]:
        """Candidate passwords for this client, most likely first.

        Only values derived from what the firm supplied are returned. Nothing is generated,
        permuted or brute-forced, which is the line ADR-008 draws.
        """
        derived: list[str] = []
        if self.pan and self.date_of_birth:
            derived.append(
                f"{self.pan.lower()}{self.date_of_birth.strftime(_DOB_PASSWORD_FORMAT)}"
            )
        derived.extend(self.extra_passwords)
        return tuple(dict.fromkeys(derived))

    def __repr__(self) -> str:
        return f"ClientCredentials(pan={'set' if self.pan else 'unset'})"


@dataclass(frozen=True, slots=True)
class CredentialStore:
    """Client credentials keyed by ``category/client``, resolved per scope."""

    _by_scope_root: dict[str, ClientCredentials] = field(default_factory=dict)

    def passwords_for(self, scope: ClientScope) -> tuple[str, ...]:
        """Candidate passwords for one scope only.

        Keying on category and client together is what stops LIC Employees under Cooperative
        Audits lending its credential to the unrelated LIC Employees under GST Proprietor.
        """
        entry = self._by_scope_root.get(f"{scope.category}/{scope.client}")
        return entry.candidates() if entry else ()

    def is_empty(self) -> bool:
        return not self._by_scope_root

    def __repr__(self) -> str:
        return f"CredentialStore(clients={len(self._by_scope_root)})"


def load_client_credentials(path: Path | None) -> CredentialStore:
    """Load the optional credential file. No path means no credentials, which is not an error."""
    if path is None:
        return CredentialStore()
    if not path.exists():
        raise CredentialError(f"credential file {path} is configured but does not exist")

    import tomli

    try:
        with path.open("rb") as handle:
            document = tomli.load(handle)
    except OSError as error:
        raise CredentialError(f"cannot read credential file {path}: {error}") from error
    except tomli.TOMLDecodeError as error:
        raise CredentialError(f"malformed TOML in {path}: {error}") from error

    clients = document.get("clients", {})
    if not isinstance(clients, dict):
        raise CredentialError(f"{path}: the [clients] table must map scope to credentials")

    return CredentialStore({key: _entry(path, key, value) for key, value in clients.items()})


def _entry(path: Path, key: str, value: object) -> ClientCredentials:
    if not isinstance(value, dict):
        raise CredentialError(f"{path}: entry for {key!r} must be a table")

    unknown = set(value) - {"pan", "date_of_birth", "extra_passwords"}
    if unknown:
        # A typo here would silently leave a client's documents locked with no explanation.
        raise CredentialError(f"{path}: entry for {key!r} has unknown keys {sorted(unknown)}")

    return ClientCredentials(
        pan=_optional_text(path, key, value.get("pan"), "pan"),
        date_of_birth=_optional_date(path, key, value.get("date_of_birth")),
        extra_passwords=_password_list(path, key, value.get("extra_passwords")),
    )


def _optional_text(path: Path, key: str, value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CredentialError(f"{path}: {name} for {key!r} must be a non-empty string")
    return value.strip()


def _optional_date(path: Path, key: str, value: object) -> date | None:
    """Accept a TOML date, or an ISO string for the many editors that quote it."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as error:
            raise CredentialError(
                f"{path}: date_of_birth for {key!r} must be an ISO date such as 1985-04-12"
            ) from error
    raise CredentialError(f"{path}: date_of_birth for {key!r} must be an ISO date")


def _password_list(path: Path, key: str, value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CredentialError(f"{path}: extra_passwords for {key!r} must be a list of strings")
    return tuple(item for item in value if item)
