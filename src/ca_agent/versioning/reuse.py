"""Purpose: decides, for one unit of work, whether a prior result can be reused or must be
reprocessed. This is where SPEC-01 req 8's incremental-processing rules live: unchanged content
under unchanged configuration reuses, a configuration change reprocesses the affected outputs,
and prior failures or partial results are retried. Kept as a pure function over a prior outcome
so the policy is testable without touching the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ca_agent.core.enums import ProcessingStatus
from ca_agent.versioning.manifest import ManifestEntry


class WorkDecision(str, Enum):
    """What the scheduler should do with one unit of work."""

    PROCESS_NEW = "process_new"
    REUSE = "reuse"
    RETRY = "retry"
    REPROCESS_CONFIG_CHANGED = "reprocess_config_changed"
    REPROCESS_FORCED = "reprocess_forced"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Operator control over which prior outcomes get another attempt."""

    force: bool = False
    retry_locked: bool = True
    retry_failed: bool = True


#: Outcomes that are settled unless the configuration changes. Retrying the ~1,839 unreadable
#: Tally binaries and shell artifacts on every run would waste the whole run budget for no
#: possible gain, since nothing about them can change while the reader set is unchanged.
_SETTLED_STATUSES = frozenset(
    {
        ProcessingStatus.SUCCESS,
        ProcessingStatus.NO_READER,
        ProcessingStatus.EXCLUDED_NON_DATA,
        ProcessingStatus.SKIPPED_DUPLICATE,
    }
)


def decide(
    prior: ManifestEntry | None,
    route_fingerprint: str,
    policy: RetryPolicy,
) -> WorkDecision:
    """Choose an action for one unit of work given its most recent recorded outcome."""
    if prior is None:
        return WorkDecision.PROCESS_NEW
    if policy.force:
        return WorkDecision.REPROCESS_FORCED
    if prior.route_fingerprint != route_fingerprint:
        return WorkDecision.REPROCESS_CONFIG_CHANGED
    if prior.status in _SETTLED_STATUSES:
        return WorkDecision.REUSE
    if prior.status is ProcessingStatus.LOCKED and not policy.retry_locked:
        return WorkDecision.REUSE
    if prior.status is ProcessingStatus.FAILED and not policy.retry_failed:
        return WorkDecision.REUSE
    # PARTIAL, LOCKED and FAILED all remain recoverable: SPEC-01 req 5 expects a rerun to pick
    # up a manually unlocked file, and req 3 expects a partial PDF to be retried.
    return WorkDecision.RETRY
