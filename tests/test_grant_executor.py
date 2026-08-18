"""
Executing a grant.

The provisioning API takes effect immediately and has no undo; revoking is a
separate call. So the reversal path, the write-ahead log, and the idempotency
key ship with the first grant rather than after the first incident.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from access_request import AccessRequest
from fakes import FakeProvisioning
from grant_executor import (
    GrantExecutor,
    GrantLog,
    GrantState,
    GuardPolicy,
    build_guards,
    idempotency_key,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def request(target: str = "jane@customer.com", module: str = "inventory") -> AccessRequest:
    return AccessRequest(
        account_id="acct-1",
        requester_email="admin@customer.com",
        target_emails=(target,),
        module=module,
    )


def executor(provisioning: FakeProvisioning | None = None, log: GrantLog | None = None):
    return GrantExecutor(
        provisioning=provisioning or FakeProvisioning(),
        log=log or GrantLog(),
        clock=lambda: NOW,
    )


class TestIdempotency:
    def test_the_same_request_produces_the_same_key(self):
        a = idempotency_key("acct-1", "jane@customer.com", "payroll", "t-1")
        b = idempotency_key("acct-1", "jane@customer.com", "payroll", "t-1")
        assert a == b

    def test_a_different_ticket_produces_a_different_key(self):
        a = idempotency_key("acct-1", "jane@customer.com", "payroll", "t-1")
        b = idempotency_key("acct-1", "jane@customer.com", "payroll", "t-2")
        assert a != b

    def test_case_does_not_change_the_key(self):
        a = idempotency_key("acct-1", "Jane@Customer.com", "payroll", "t-1")
        b = idempotency_key("acct-1", "jane@customer.com", "payroll", "t-1")
        assert a == b

    def test_a_replayed_ticket_does_not_grant_twice(self):
        """
        Worker retries, duplicate webhooks, a customer resending the same mail —
        all of these arrive as the same ticket, and none should produce a second
        grant.
        """
        provisioning = FakeProvisioning()
        ex = executor(provisioning)
        first = ex.execute(request(), ticket_id="t-1")
        second = ex.execute(request(), ticket_id="t-1")

        assert len(provisioning.calls) == 1
        assert second.idempotency_key == first.idempotency_key
        assert second.state is GrantState.GRANTED

    def test_a_genuinely_new_request_in_a_new_ticket_does_grant(self):
        provisioning = FakeProvisioning()
        log = GrantLog()
        executor(provisioning, log).execute(request(), ticket_id="t-1")
        executor(provisioning, log).execute(request(), ticket_id="t-2")
        assert len(provisioning.calls) == 2


class TestWriteAheadLogging:
    def test_intent_is_recorded_before_the_api_is_called(self):
        """
        If the call succeeds and the response is lost, the log is the only
        evidence that we changed something in a customer's environment.
        """
        order: list[str] = []

        class OrderingLog(GrantLog):
            def append(self, record):
                order.append(f"log:{record.state}")
                super().append(record)

        class OrderingProvisioning(FakeProvisioning):
            def grant_module_access(self, *args):
                order.append("api:grant")
                return super().grant_module_access(*args)

        GrantExecutor(
            provisioning=OrderingProvisioning(), log=OrderingLog(), clock=lambda: NOW
        ).execute(request(), ticket_id="t-1")

        assert order.index("log:pending") < order.index("api:grant")

    def test_the_log_keeps_the_full_history_not_just_the_latest_state(self):
        log = GrantLog()
        executor(log=log).execute(request(), ticket_id="t-1")
        assert [entry.state for entry in log.entries] == [GrantState.PENDING, GrantState.GRANTED]


class TestFailureClassification:
    def test_a_successful_grant_is_recorded_as_granted(self):
        record = executor().execute(request(), ticket_id="t-1")
        assert record.state is GrantState.GRANTED
        assert record.resolved_at == NOW

    def test_a_rejected_request_is_recorded_as_failed(self):
        """ConnectionRefused: nothing was ever received, so nothing happened."""
        provisioning = FakeProvisioning(error=ConnectionRefusedError("no route"))
        record = executor(provisioning).execute(request(), ticket_id="t-1")
        assert record.state is GrantState.FAILED

    def test_a_bad_argument_is_recorded_as_failed(self):
        provisioning = FakeProvisioning(error=ValueError("unknown module"))
        assert executor(provisioning).execute(request(), ticket_id="t-1").state is GrantState.FAILED

    def test_a_timeout_is_recorded_as_unknown_not_failed(self):
        """
        The request may have been received and applied. Calling this a failure
        would mean our records say no access was granted while the customer's
        system says otherwise — the one state worth waking someone for.
        """
        provisioning = FakeProvisioning(error=TimeoutError("gateway timeout"))
        record = executor(provisioning).execute(request(), ticket_id="t-1")
        assert record.state is GrantState.UNKNOWN

    def test_a_reset_connection_is_recorded_as_unknown(self):
        provisioning = FakeProvisioning(error=ConnectionResetError("sent, no response"))
        assert (
            executor(provisioning).execute(request(), ticket_id="t-1").state is GrantState.UNKNOWN
        )

    def test_a_failure_is_never_retried(self):
        """
        Read tools may retry. A write may not: the inherited execute_tool retried
        every exception three times, which on a grant is three grants.
        """
        provisioning = FakeProvisioning(error=TimeoutError("gateway"))
        executor(provisioning).execute(request(), ticket_id="t-1")
        assert len(provisioning.calls) == 1

    def test_the_error_is_kept_on_the_record(self):
        provisioning = FakeProvisioning(error=TimeoutError("gateway timeout"))
        record = executor(provisioning).execute(request(), ticket_id="t-1")
        assert "gateway timeout" in record.error


class TestReversal:
    def test_revoke_window_reverses_every_grant_in_the_period(self):
        """
        What an incident actually needs: undo everything this thing did in the
        last N hours, in one call. Revoking one at a time is not a plan.
        """
        provisioning = FakeProvisioning()
        log = GrantLog()
        executor(provisioning, log).execute(request("a@customer.com"), ticket_id="t-1")
        executor(provisioning, log).execute(request("b@customer.com"), ticket_id="t-2")

        revoked = GrantExecutor(provisioning, log, lambda: NOW).revoke_window(
            since=NOW - timedelta(hours=6)
        )

        assert len(revoked) == 2
        assert {r.state for r in revoked} == {GrantState.REVOKED}
        assert len(provisioning.revoke_calls) == 2

    def test_grants_that_definitely_never_happened_are_skipped(self):
        provisioning = FakeProvisioning(error=ConnectionRefusedError("no route"))
        log = GrantLog()
        executor(provisioning, log).execute(request(), ticket_id="t-1")

        provisioning.error = None
        assert GrantExecutor(provisioning, log, lambda: NOW).revoke_window(since=NOW - timedelta(hours=6)) == []

    def test_grants_in_an_unknown_state_are_revoked_anyway(self):
        """
        An ambiguous outcome may have taken effect. During an incident the cost
        of a redundant revoke is nothing; the cost of skipping a live grant is
        the incident continuing.
        """
        provisioning = FakeProvisioning(error=TimeoutError("gateway"))
        log = GrantLog()
        executor(provisioning, log).execute(request(), ticket_id="t-1")

        provisioning.error = None
        revoked = GrantExecutor(provisioning, log, lambda: NOW).revoke_window(
            since=NOW - timedelta(hours=6)
        )
        assert len(revoked) == 1

    def test_grants_outside_the_window_are_untouched(self):
        provisioning = FakeProvisioning()
        log = GrantLog()
        GrantExecutor(provisioning, log, lambda: NOW - timedelta(days=2)).execute(
            request(), ticket_id="t-old"
        )
        assert GrantExecutor(provisioning, log, lambda: NOW).revoke_window(
            since=NOW - timedelta(hours=6)
        ) == []

    def test_revoking_twice_does_not_call_the_api_twice(self):
        provisioning = FakeProvisioning()
        log = GrantLog()
        ex = GrantExecutor(provisioning, log, lambda: NOW)
        ex.execute(request(), ticket_id="t-1")
        ex.revoke_window(since=NOW - timedelta(hours=6))
        ex.revoke_window(since=NOW - timedelta(hours=6))
        assert len(provisioning.revoke_calls) == 1


class TestRateLimits:
    def test_a_fresh_account_has_full_capacity(self):
        guards = build_guards(
            GrantLog(), account_id="acct-1", now=NOW, policy=GuardPolicy(), kill_switch_engaged=False
        )
        assert guards.has_grant_capacity

    def test_capacity_falls_as_grants_are_made(self):
        log = GrantLog()
        for i in range(3):
            executor(log=log).execute(request(f"u{i}@customer.com"), ticket_id=f"t-{i}")

        guards = build_guards(log, "acct-1", NOW, GuardPolicy(max_grants_per_account_per_hour=5), False)
        assert guards.account_grants_remaining == 2

    def test_the_per_account_ceiling_stops_further_grants(self):
        log = GrantLog()
        for i in range(5):
            executor(log=log).execute(request(f"u{i}@customer.com"), ticket_id=f"t-{i}")

        guards = build_guards(log, "acct-1", NOW, GuardPolicy(max_grants_per_account_per_hour=5), False)
        assert not guards.has_grant_capacity

    def test_older_grants_fall_out_of_the_window(self):
        log = GrantLog()
        GrantExecutor(FakeProvisioning(), log, lambda: NOW - timedelta(hours=3)).execute(
            request(), ticket_id="t-old"
        )
        guards = build_guards(log, "acct-1", NOW, GuardPolicy(max_grants_per_account_per_hour=5), False)
        assert guards.account_grants_remaining == 5

    def test_the_kill_switch_is_carried_through(self):
        guards = build_guards(GrantLog(), "acct-1", NOW, GuardPolicy(), kill_switch_engaged=True)
        assert guards.kill_switch_engaged

    def test_another_accounts_grants_do_not_consume_this_accounts_budget(self):
        log = GrantLog()
        other = AccessRequest("acct-2", "admin@other.com", ("x@other.com",), "inventory")
        executor(log=log).execute(other, ticket_id="t-9")

        guards = build_guards(log, "acct-1", NOW, GuardPolicy(max_grants_per_account_per_hour=5), False)
        assert guards.account_grants_remaining == 5
        assert guards.global_grants_remaining == GuardPolicy().max_grants_globally_per_hour - 1


class TestUnknownStateIsSurfaced:
    def test_unresolved_grants_can_be_listed_for_reconciliation(self):
        provisioning = FakeProvisioning(error=TimeoutError("gateway"))
        log = GrantLog()
        executor(provisioning, log).execute(request(), ticket_id="t-1")
        assert len(log.needing_reconciliation()) == 1

    def test_a_clean_log_needs_no_reconciliation(self):
        log = GrantLog()
        executor(log=log).execute(request(), ticket_id="t-1")
        assert log.needing_reconciliation() == []
