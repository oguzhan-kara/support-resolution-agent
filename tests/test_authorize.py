"""
The authorization decision.

Every test here runs with no clock, no network, no model, and no fixture files.
That is the point of the design rather than a side effect of it: `authorize()`
is a pure function, so an exhaustive adversarial suite costs milliseconds and
can run on every commit. A security property that is expensive to test is a
security property that stops being tested.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from access_request import (
    AccessRequest,
    AccountFacts,
    AdminContactRecord,
    AutonomyLevel,
    Decision,
    DirectoryUser,
    Policy,
    ReasonCode,
    RuntimeGuards,
    Sensitivity,
    authorize,
    classify_module,
    resolve_module,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def world(
    *,
    requester: str = "admin@customer.com",
    targets: tuple[str, ...] = ("jane@customer.com",),
    target_hint: str = "",
    module: str | None = "inventory",
    is_revocation: bool = False,
    status: str = "active",
    domains: tuple[str, ...] = ("customer.com",),
    admin_emails: tuple[str, ...] = ("admin@customer.com",),
    admin_age_days: int | None = 10,
    directory: dict[str, DirectoryUser] | None = None,
    target_active: bool = True,
    target_modules: tuple[str, ...] = (),
    autonomy: AutonomyLevel = AutonomyLevel.STANDARD_AUTO,
    guards: RuntimeGuards | None = None,
    max_age_days: int = 90,
    max_targets: int = 1,
) -> dict:
    if directory is None:
        directory = {
            email.lower(): DirectoryUser(
                email=email, active=target_active, modules=target_modules, display_name="Jane Doe"
            )
            for email in targets
        }
    return {
        "request": AccessRequest(
            account_id="acct-1",
            requester_email=requester,
            target_emails=targets,
            target_hint=target_hint,
            module=module,
            raw_module_text=module or "",
            is_revocation=is_revocation,
        ),
        "account": AccountFacts(
            account_id="acct-1",
            status=status,
            email_domains=domains,
            product_version="v14",
        ),
        "admin_record": AdminContactRecord(
            account_id="acct-1",
            admin_emails=admin_emails,
            last_updated=None if admin_age_days is None else NOW - timedelta(days=admin_age_days),
        ),
        "directory": directory,
        "policy": Policy(
            autonomy=autonomy,
            admin_record_max_age_days=max_age_days,
            max_targets_per_request=max_targets,
        ),
        "guards": guards or RuntimeGuards(),
        "now": NOW,
    }


# --------------------------------------------------------------------------- #
# Types and the module registry
# --------------------------------------------------------------------------- #


class TestModuleRegistry:
    def test_payroll_is_restricted(self):
        assert classify_module("payroll") is Sensitivity.RESTRICTED

    def test_inventory_is_standard(self):
        assert classify_module("inventory") is Sensitivity.STANDARD

    def test_an_unregistered_module_is_unknown_not_standard(self):
        """Fail closed. A module we have never heard of is not a safe default."""
        assert classify_module("time_machine") is Sensitivity.UNKNOWN

    def test_none_is_unknown(self):
        assert classify_module(None) is Sensitivity.UNKNOWN

    def test_resolution_is_case_and_whitespace_insensitive(self):
        assert resolve_module("  Payroll  ") == "payroll"
        assert resolve_module("PAYROLL") == "payroll"

    def test_known_aliases_resolve(self):
        assert resolve_module("Payroll Reports") == "payroll_reports"
        assert resolve_module("the payroll module") == "payroll"

    def test_similar_module_names_do_not_collide(self):
        """'Payroll' and 'Payroll Reports' are different grants."""
        assert resolve_module("Payroll") != resolve_module("Payroll Reports")

    def test_an_unresolvable_name_returns_none_rather_than_guessing(self):
        assert resolve_module("the thing Jane needs") is None

    def test_request_is_immutable(self):
        request = world()["request"]
        with pytest.raises(FrozenInstanceError):
            request.module = "payroll"


# --------------------------------------------------------------------------- #
# Runtime guards and account state
# --------------------------------------------------------------------------- #


class TestRuntimeGuards:
    def test_an_engaged_kill_switch_stops_everything_before_any_other_check(self):
        """
        Checked first, so a kill switch works even on a request that would
        otherwise be flawless. It is read at runtime, not deploy time: stopping
        must not require a release.
        """
        result = authorize(**world(guards=RuntimeGuards(kill_switch_engaged=True)))
        assert result.decision is Decision.ESCALATE
        assert result.reason is ReasonCode.KILL_SWITCH_ENGAGED

    def test_an_exhausted_global_rate_limit_stops_an_otherwise_valid_grant(self):
        result = authorize(**world(guards=RuntimeGuards(global_grants_remaining=0)))
        assert result.decision is Decision.ESCALATE
        assert result.reason is ReasonCode.RATE_LIMIT_EXCEEDED

    def test_an_exhausted_per_account_rate_limit_stops_a_grant(self):
        result = authorize(**world(guards=RuntimeGuards(account_grants_remaining=0)))
        assert result.reason is ReasonCode.RATE_LIMIT_EXCEEDED

    def test_a_rate_limit_does_not_block_a_decision_that_grants_nothing(self):
        result = authorize(
            **world(module="payroll", guards=RuntimeGuards(global_grants_remaining=0))
        )
        assert result.reason is ReasonCode.MODULE_SENSITIVE_REQUIRES_APPROVAL


class TestAccountState:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("suspended", ReasonCode.ACCOUNT_SUSPENDED),
            ("delinquent", ReasonCode.ACCOUNT_DELINQUENT),
            ("SUSPENDED", ReasonCode.ACCOUNT_SUSPENDED),
            (" Delinquent ", ReasonCode.ACCOUNT_DELINQUENT),
        ],
    )
    def test_a_non_serviceable_account_escalates(self, status, expected):
        result = authorize(**world(status=status))
        assert result.decision is Decision.ESCALATE
        assert result.reason is expected


# --------------------------------------------------------------------------- #
# The admin contact list as an authorization source
# --------------------------------------------------------------------------- #


class TestAdminRecordFreshness:
    def test_a_record_older_than_policy_is_not_an_authorization_source(self):
        """
        Median age across the 900 accounts is about seven months, and 22% have
        not been touched in over a year. A list nobody has verified is a stale
        cache, not an authorization source. Blocking on age converts an
        invisible data-quality problem into a named queue of accounts for
        Customer Success.
        """
        result = authorize(**world(admin_age_days=91))
        assert result.decision is Decision.ESCALATE
        assert result.reason is ReasonCode.ADMIN_RECORD_STALE

    def test_a_record_exactly_at_the_boundary_is_still_valid(self):
        assert authorize(**world(admin_age_days=90)).reason is not ReasonCode.ADMIN_RECORD_STALE

    def test_a_year_old_record_is_stale(self):
        assert authorize(**world(admin_age_days=400)).reason is ReasonCode.ADMIN_RECORD_STALE

    def test_a_record_with_no_timestamp_fails_closed(self):
        """Unknown age is not young."""
        assert authorize(**world(admin_age_days=None)).reason is ReasonCode.ADMIN_RECORD_MISSING

    def test_a_record_listing_nobody_fails_closed(self):
        assert authorize(**world(admin_emails=())).reason is ReasonCode.ADMIN_RECORD_MISSING

    def test_the_age_is_reported_as_evidence(self):
        result = authorize(**world(admin_age_days=91))
        assert result.evidence["admin_record_age_days"] == 91


class TestRequesterIdentity:
    def test_a_requester_absent_from_the_admin_list_escalates(self):
        result = authorize(**world(requester="stranger@customer.com"))
        assert result.decision is Decision.ESCALATE
        assert result.reason is ReasonCode.REQUESTER_NOT_ADMIN

    def test_comparison_is_case_insensitive(self):
        result = authorize(
            **world(requester="Admin@Customer.COM", admin_emails=("admin@customer.com",))
        )
        assert result.reason is not ReasonCode.REQUESTER_NOT_ADMIN

    def test_a_requester_on_a_foreign_domain_escalates_even_when_listed(self):
        """
        Being on the admin list is not sufficient if the address does not belong
        to the account. The list is maintained by hand and a stale entry can
        outlive the relationship it recorded.
        """
        result = authorize(
            **world(requester="admin@attacker.com", admin_emails=("admin@attacker.com",))
        )
        assert result.reason is ReasonCode.REQUESTER_DOMAIN_MISMATCH

    def test_a_lookalike_domain_does_not_pass(self):
        result = authorize(
            **world(
                requester="admin@customer.com.attacker.io",
                admin_emails=("admin@customer.com.attacker.io",),
            )
        )
        assert result.reason is ReasonCode.REQUESTER_DOMAIN_MISMATCH


# --------------------------------------------------------------------------- #
# The request itself
# --------------------------------------------------------------------------- #


class TestRequestShape:
    def test_a_revocation_is_never_executed_by_this_capability(self):
        """
        The Director asked for grants. Revocation has a different failure mode —
        removing access someone needs during their working day — and it was not
        asked for, so it is not silently in scope.
        """
        result = authorize(**world(is_revocation=True))
        assert result.decision is Decision.ESCALATE
        assert result.reason is ReasonCode.REVOCATION_NOT_SUPPORTED

    def test_a_request_with_no_identified_target_asks_rather_than_guesses(self):
        result = authorize(**world(targets=(), directory={}))
        assert result.decision is Decision.CLARIFY
        assert result.reason is ReasonCode.REQUEST_INCOMPLETE

    def test_a_name_matching_two_people_asks_which_one(self):
        """'Please give Jane access' with two Janes on the account."""
        directory = {
            "jane.doe@customer.com": DirectoryUser(
                "jane.doe@customer.com", True, (), "Jane Doe"
            ),
            "jane.roe@customer.com": DirectoryUser(
                "jane.roe@customer.com", True, (), "Jane Roe"
            ),
        }
        result = authorize(**world(targets=(), target_hint="Jane", directory=directory))
        assert result.decision is Decision.CLARIFY
        assert result.reason is ReasonCode.TARGET_USER_AMBIGUOUS

    def test_a_single_name_match_still_asks_rather_than_resolving_it(self):
        """
        One fuzzy name match is not identification. Resolving 'Jane' to an
        address and then granting on it means a typo becomes a data-access event.
        """
        directory = {
            "jane.doe@customer.com": DirectoryUser("jane.doe@customer.com", True, (), "Jane Doe")
        }
        result = authorize(**world(targets=(), target_hint="Jane", directory=directory))
        assert result.decision is Decision.CLARIFY
        assert result.reason is ReasonCode.REQUEST_INCOMPLETE

    def test_a_bulk_request_is_not_handled_automatically(self):
        result = authorize(**world(targets=("a@customer.com", "b@customer.com")))
        assert result.decision is Decision.ESCALATE
        assert result.reason is ReasonCode.BULK_REQUEST_UNSUPPORTED


class TestTargetUser:
    def test_an_unknown_target_escalates(self):
        result = authorize(**world(targets=("ghost@customer.com",), directory={}))
        assert result.reason is ReasonCode.TARGET_USER_UNKNOWN

    def test_a_deactivated_target_escalates(self):
        """A departed employee is the case this exists for."""
        result = authorize(**world(target_active=False))
        assert result.reason is ReasonCode.TARGET_USER_INACTIVE

    def test_a_target_who_already_has_the_module_is_told_so(self):
        result = authorize(**world(module="inventory", target_modules=("inventory",)))
        assert result.decision is Decision.CLARIFY
        assert result.reason is ReasonCode.ALREADY_HAS_ACCESS


class TestModuleSensitivity:
    def test_an_unresolvable_module_escalates_rather_than_guessing(self):
        result = authorize(**world(module=None))
        assert result.decision is Decision.ESCALATE
        assert result.reason is ReasonCode.MODULE_UNKNOWN

    def test_an_unregistered_module_escalates(self):
        result = authorize(**world(module="time_machine"))
        assert result.reason is ReasonCode.MODULE_UNKNOWN

    def test_payroll_is_never_auto_granted_however_permissive_the_policy(self):
        """
        The request that started this was specifically about payroll. Every gate
        can pass and the answer is still a prepared action for a human, because
        the authorization source is a hand-maintained list and the data is
        compensation.
        """
        for autonomy in (AutonomyLevel.STANDARD_AUTO, AutonomyLevel.FULL_AUTO):
            result = authorize(**world(module="payroll", autonomy=autonomy))
            assert result.decision is Decision.PREPARE_FOR_APPROVAL
            assert result.reason is ReasonCode.MODULE_SENSITIVE_REQUIRES_APPROVAL

    @pytest.mark.parametrize("module", ["payroll", "finance", "hr_records", "audit_log"])
    def test_every_restricted_module_requires_approval(self, module):
        result = authorize(**world(module=module, autonomy=AutonomyLevel.FULL_AUTO))
        assert result.decision is Decision.PREPARE_FOR_APPROVAL


class TestAutonomyLevels:
    def test_standard_auto_grants_a_standard_module(self):
        result = authorize(**world(module="inventory", autonomy=AutonomyLevel.STANDARD_AUTO))
        assert result.decision is Decision.GRANT
        assert result.reason is ReasonCode.AUTHORIZED

    def test_prepare_only_prepares_the_same_request(self):
        result = authorize(**world(module="inventory", autonomy=AutonomyLevel.PREPARE_ONLY))
        assert result.decision is Decision.PREPARE_FOR_APPROVAL
        assert result.reason is ReasonCode.AUTHORIZED

    def test_shadow_mode_acts_on_nothing_but_still_records_what_it_would_have_done(self):
        """
        Day 0 runs at OFF. The decision is still computed and the reason still
        recorded, so the shadow period produces a diff against human outcomes
        rather than silence.
        """
        result = authorize(**world(module="inventory", autonomy=AutonomyLevel.OFF))
        assert result.decision is Decision.ESCALATE
        assert result.reason is ReasonCode.AUTHORIZED


# --------------------------------------------------------------------------- #
# Properties that must hold across every combination
# --------------------------------------------------------------------------- #


class TestInvariants:
    def test_nothing_reaches_grant_without_the_authorized_reason(self):
        cases = [
            world(),
            world(status="suspended"),
            world(admin_age_days=400),
            world(requester="stranger@customer.com"),
            world(requester="admin@attacker.com", admin_emails=("admin@attacker.com",)),
            world(module="payroll"),
            world(module=None),
            world(module="time_machine"),
            world(targets=()),
            world(targets=("a@customer.com", "b@customer.com")),
            world(target_active=False),
            world(is_revocation=True),
            world(guards=RuntimeGuards(kill_switch_engaged=True)),
            world(guards=RuntimeGuards(global_grants_remaining=0)),
            world(autonomy=AutonomyLevel.OFF),
            world(autonomy=AutonomyLevel.PREPARE_ONLY),
            world(admin_emails=()),
            world(target_modules=("inventory",)),
        ]
        for case in cases:
            result = authorize(**case)
            if result.decision is Decision.GRANT:
                assert result.reason is ReasonCode.AUTHORIZED

    def test_only_one_case_in_that_set_actually_grants(self):
        granted = [
            case
            for case in [
                world(),
                world(status="suspended"),
                world(admin_age_days=400),
                world(module="payroll"),
                world(targets=()),
            ]
            if authorize(**case).decision is Decision.GRANT
        ]
        assert len(granted) == 1

    def test_the_decision_is_deterministic(self):
        first, second = authorize(**world()), authorize(**world())
        assert (first.decision, first.reason) == (second.decision, second.reason)

    def test_every_result_carries_a_summary_a_human_can_act_on(self):
        for case in [world(), world(module="payroll"), world(admin_age_days=400), world(targets=())]:
            assert len(authorize(**case).human_summary) > 20

    def test_authorize_never_sees_ticket_text(self):
        """
        The structural defence against prompt injection: the decision function's
        parameters carry validated fields only. There is no argument through
        which attacker-controlled prose could arrive.
        """
        import inspect

        params = set(inspect.signature(authorize).parameters)
        assert params == {
            "request",
            "account",
            "admin_record",
            "directory",
            "policy",
            "guards",
            "now",
        }
        assert not any(
            field in AccessRequest.__dataclass_fields__ for field in ("body", "subject", "text")
        )
