"""
Executing and reversing a grant.

`provisioning.grant_module_access` takes effect immediately and has no undo;
revoking is a separate call. The design rule that follows is simple: do not make
an irreversible call until the reversal path exists. So this module ships with
the first grant rather than after the first incident.

Four properties, each of which exists because of a specific way this can go
wrong:

  Write-ahead logging. Intent is recorded before the API call, so a lost
  response cannot leave us unaware that we changed a customer's environment.

  Idempotency. Worker retries, duplicate webhooks, and a customer resending the
  same mail all arrive as the same ticket. None of them should grant twice.

  No retries. The inherited execute_tool retried every exception three times
  (sra_runtime.py:71-81). On a read that is wasteful; on a grant it is three
  grants.

  A distinction between "failed" and "unknown". A rejected request definitely
  did nothing. A timeout may have been applied. Recording the second as a
  failure means our records say no access exists while the customer's system
  says otherwise, and nobody goes looking. UNKNOWN is the only state here that
  should wake someone.

Standard library only; the provisioning client and the clock are injected.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Callable

from access_request import AccessRequest, RuntimeGuards

_log = logging.getLogger("sra.grants")


class GrantState(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    FAILED = "failed"
    UNKNOWN = "unknown"
    REVOKED = "revoked"


# Errors that prove the request never reached the provisioning system. Safe to
# record as a definite non-event.
DEFINITELY_NOT_APPLIED: tuple[type[BaseException], ...] = (
    ConnectionRefusedError,
    ValueError,
    TypeError,
    KeyError,
    PermissionError,
)

# Everything else — timeouts, resets, aborted connections — leaves the outcome
# genuinely unknown. Assume it may have been applied.


def idempotency_key(account_id: str, target_email: str, module: str, ticket_id: str) -> str:
    """
    Stable identity for "this grant, from this ticket".

    The ticket id is part of the key on purpose: a genuinely new request in a new
    ticket should grant, while the same ticket arriving twice should not.
    """
    payload = "|".join(
        part.strip().lower() for part in (account_id, target_email, module, ticket_id)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class GrantRecord:
    idempotency_key: str
    account_id: str
    target_email: str
    module: str
    ticket_id: str
    state: GrantState
    created_at: datetime
    resolved_at: datetime | None = None
    error: str | None = None


class GrantLog:
    """
    Append-only record of every grant this system attempted.

    In production this is a durable table; the in-memory version here is the
    reference implementation the tests and the eval run against. Nothing is ever
    updated in place — a state change is a new entry — because the question that
    matters during an incident is "what did we do and when", not "what is the
    current state".
    """

    def __init__(self) -> None:
        self.entries: list[GrantRecord] = []

    def append(self, record: GrantRecord) -> None:
        self.entries.append(record)

    def find(self, key: str) -> GrantRecord | None:
        """Latest entry for a key."""
        for record in reversed(self.entries):
            if record.idempotency_key == key:
                return record
        return None

    def latest_by_key(self) -> dict[str, GrantRecord]:
        latest: dict[str, GrantRecord] = {}
        for record in self.entries:
            latest[record.idempotency_key] = record
        return latest

    def since(self, when: datetime) -> list[GrantRecord]:
        return [r for r in self.latest_by_key().values() if r.created_at >= when]

    def needing_reconciliation(self) -> list[GrantRecord]:
        """
        Grants whose outcome we do not know.

        This list should always be empty. When it is not, our records and a
        customer's reality may disagree, which is the one condition here worth
        a page at three in the morning.
        """
        return [r for r in self.latest_by_key().values() if r.state is GrantState.UNKNOWN]

    def count_since(self, when: datetime, account_id: str | None = None) -> int:
        return sum(
            1
            for r in self.since(when)
            if r.state in (GrantState.GRANTED, GrantState.UNKNOWN)
            and (account_id is None or r.account_id == account_id)
        )


@dataclass(frozen=True)
class GuardPolicy:
    """
    Ceilings on how much this can do per hour.

    These are not performance limits. They bound the blast radius of a fault we
    have not thought of: whatever goes wrong, it goes wrong at most this many
    times before someone notices.
    """

    max_grants_per_account_per_hour: int = 5
    max_grants_globally_per_hour: int = 50
    window: timedelta = field(default=timedelta(hours=1))


def build_guards(
    grant_log: GrantLog,
    account_id: str,
    now: datetime,
    policy: GuardPolicy,
    kill_switch_engaged: bool,
) -> RuntimeGuards:
    """
    Snapshot runtime state for `authorize()`.

    Resolving this here is what lets the decision function stay pure. The kill
    switch is passed in rather than read here so it can come from whatever the
    deployment uses for runtime configuration — the requirement is only that
    engaging it never needs a deploy.
    """
    window_start = now - policy.window
    return RuntimeGuards(
        kill_switch_engaged=kill_switch_engaged,
        account_grants_remaining=max(
            0,
            policy.max_grants_per_account_per_hour
            - grant_log.count_since(window_start, account_id=account_id),
        ),
        global_grants_remaining=max(
            0, policy.max_grants_globally_per_hour - grant_log.count_since(window_start)
        ),
    )


class GrantExecutor:
    def __init__(
        self,
        provisioning: Any,
        log: GrantLog | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.provisioning = provisioning
        self.log = log if log is not None else GrantLog()
        self.clock = clock or (lambda: datetime.now())

    def execute(self, request: AccessRequest, ticket_id: str) -> GrantRecord:
        """
        Attempt one grant, exactly once.

        Callers reach this only after `authorize()` returned GRANT. This function
        does not re-decide; it records, acts, and records again.
        """
        target = request.target_emails[0]
        module = request.module or ""
        key = idempotency_key(request.account_id, target, module, ticket_id)

        existing = self.log.find(key)
        if existing is not None and existing.state is not GrantState.FAILED:
            # Already attempted. A previous UNKNOWN is deliberately not retried:
            # it may have been applied, and a retry could be a second grant.
            _log.info("grant %s already attempted (%s); not repeating", key, existing.state)
            return existing

        now = self.clock()
        pending = GrantRecord(
            idempotency_key=key,
            account_id=request.account_id,
            target_email=target,
            module=module,
            ticket_id=ticket_id,
            state=GrantState.PENDING,
            created_at=now,
        )
        self.log.append(pending)  # before the call, always

        try:
            self.provisioning.grant_module_access(request.account_id, target, module)
        except DEFINITELY_NOT_APPLIED as e:
            resolved = replace(
                pending, state=GrantState.FAILED, resolved_at=self.clock(), error=str(e)
            )
            _log.error("grant %s failed and was not applied: %s", key, e)
        except Exception as e:
            # Sent, no clean answer. Assume it may have taken effect.
            resolved = replace(
                pending, state=GrantState.UNKNOWN, resolved_at=self.clock(), error=str(e)
            )
            _log.error("grant %s outcome UNKNOWN, needs reconciliation: %s", key, e)
        else:
            resolved = replace(pending, state=GrantState.GRANTED, resolved_at=self.clock())
            _log.info("granted %s to %s on %s", module, target, request.account_id)

        self.log.append(resolved)
        return resolved

    def revoke_window(self, since: datetime) -> list[GrantRecord]:
        """
        Reverse every grant this system made since `since`.

        The provisioning API has no undo, so this is the undo. It is deliberately
        coarse: during an incident the question is "what has this thing done in
        the last six hours", and revoking one record at a time is not a plan.

        Grants in an UNKNOWN state are revoked too. A redundant revoke costs
        nothing; skipping one that was actually applied means the incident
        continues while we believe it is over.
        """
        reversed_records: list[GrantRecord] = []

        for record in self.log.since(since):
            if record.state not in (GrantState.GRANTED, GrantState.UNKNOWN):
                continue
            try:
                self.provisioning.revoke_module_access(
                    record.account_id, record.target_email, record.module
                )
            except Exception as e:
                # Keep going. One failed revoke must not stop the rest, and the
                # entry stays in its current state so it shows up again.
                _log.error("revoke failed for %s: %s", record.idempotency_key, e)
                continue

            revoked = replace(record, state=GrantState.REVOKED, resolved_at=self.clock())
            self.log.append(revoked)
            reversed_records.append(revoked)

        _log.warning("revoked %d grants issued since %s", len(reversed_records), since.isoformat())
        return reversed_records
