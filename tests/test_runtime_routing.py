"""
Routing access requests through the deterministic path.

The research loop is bypassed entirely. An access request is a classification
problem, not a research problem, and giving it eight steps and the full document
set is how a $0.34 mean becomes a $3.90 ticket.
"""

from __future__ import annotations

import json

from access_request import AutonomyLevel, Policy
from fakes import (
    FakeCRM,
    FakeKB,
    FakeLLM,
    FakeProvisioning,
    FakeResponse,
    FakeTicketing,
    admin_contacts,
    customer,
    doc,
    make_deps,
    replies,
)
from datetime import datetime, timezone

from grant_executor import GrantLog, GrantState
from sra_runtime import AccessConfig, TicketContext, handle_access_request, run

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)

USERS = [
    {"email": "jane@customer.com", "active": True, "modules": [], "display_name": "Jane Doe"},
    {"email": "gone@customer.com", "active": False, "modules": [], "display_name": "Sam Gone"},
]


def ticket(subject: str, body: str, sender: str = "admin@customer.com") -> TicketContext:
    return TicketContext(
        ticket_id="49500",
        subject=subject,
        body=body,
        sender_email=sender,
        account_id="acct-1",
    )


def extraction(payload: dict) -> FakeResponse:
    return FakeResponse(text=json.dumps(payload))


def access_deps(
    module: str = "inventory",
    targets: list[str] | None = None,
    admin_age_days: int = 10,
    provisioning: FakeProvisioning | None = None,
    ticketing: FakeTicketing | None = None,
):
    return make_deps(
        llm=FakeLLM(
            extraction({"target_emails": targets if targets is not None else ["jane@customer.com"], "module": module})
        ),
        kb=FakeKB([doc("Docs.", version="v14", age_days=3)]),
        crm=FakeCRM(
            record=customer(product_version="v14"),
            admin_contacts=admin_contacts(age_days=admin_age_days),
            directory=USERS,
        ),
        ticketing=ticketing or FakeTicketing(),
        provisioning=provisioning or FakeProvisioning(),
    )


def auto(grant_log: GrantLog | None = None) -> AccessConfig:
    # The clock is injected here for the same reason authorize() takes `now` as
    # a parameter: an admin record's age is load-bearing, and a test that reads
    # the wall clock reports 399 days one day and 400 the next.
    return AccessConfig(
        policy=Policy(autonomy=AutonomyLevel.STANDARD_AUTO),
        grant_log=grant_log or GrantLog(),
        clock=lambda: NOW,
    )


class TestRouting:
    def test_a_non_access_ticket_is_left_to_the_existing_agent(self):
        result = handle_access_request(
            ticket("Payroll export times out", "Intermittent timeouts."),
            deps=access_deps(),
            access=auto(),
            customer=customer(),
        )
        assert result is None

    def test_an_informational_question_is_left_to_the_existing_agent(self):
        result = handle_access_request(
            ticket("How do I give Jane access?", "Where is the setting?"),
            deps=access_deps(),
            access=auto(),
            customer=customer(),
        )
        assert result is None

    def test_an_unrelated_ticket_still_goes_through_the_normal_loop(self):
        deps = make_deps(
            llm=FakeLLM(replies("Here is the fix.")),
            kb=FakeKB([doc("Docs.", version="v14", age_days=3)]),
            crm=FakeCRM(record=customer(product_version="v14")),
        )
        assert run(ticket("Export fails", "It errors."), deps=deps)["outcome"] == "resolved"


class TestGranting:
    def test_a_valid_standard_request_calls_provisioning_exactly_once(self):
        provisioning = FakeProvisioning()
        out = run(
            ticket("Access request", "Please give jane@customer.com access to Inventory."),
            deps=access_deps(provisioning=provisioning),
            access=auto(),
        )
        assert provisioning.calls == [("acct-1", "jane@customer.com", "inventory")]
        assert out["outcome"] == "resolved:access_granted"

    def test_the_reply_says_what_was_granted_who_authorized_it_and_how_to_undo_it(self):
        out = run(
            ticket("Access request", "Please give jane@customer.com access to Inventory."),
            deps=access_deps(),
            access=auto(),
        )
        body = out["body"].lower()
        for fragment in ("inventory", "jane@customer.com", "authorized by", "revoke"):
            assert fragment in body, fragment

    def test_the_grant_is_recorded_in_the_log(self):
        grant_log = GrantLog()
        run(
            ticket("Access request", "Please give jane@customer.com access to Inventory."),
            deps=access_deps(),
            access=auto(grant_log),
        )
        assert [e.state for e in grant_log.entries] == [GrantState.PENDING, GrantState.GRANTED]

    def test_the_same_ticket_processed_twice_grants_once(self):
        provisioning = FakeProvisioning()
        grant_log = GrantLog()
        for _ in range(2):
            run(
                ticket("Access request", "Please give jane@customer.com access to Inventory."),
                deps=access_deps(provisioning=provisioning),
                access=auto(grant_log),
            )
        assert len(provisioning.calls) == 1


class TestTheSensitiveModuleIsNeverAutomatic:
    def test_a_payroll_request_never_reaches_provisioning(self):
        provisioning = FakeProvisioning()
        out = run(
            ticket("Access request", "Please give jane@customer.com access to Payroll."),
            deps=access_deps(module="payroll", provisioning=provisioning),
            access=auto(),
        )
        assert provisioning.calls == []
        assert out["outcome"] == "escalated:module_sensitive_requires_approval"

    def test_the_escalation_note_carries_the_finished_analysis(self):
        """
        The value of escalating well: a support engineer confirms in seconds
        instead of reconstructing the case from scratch.
        """
        ticketing = FakeTicketing()
        run(
            ticket("Access request", "Please give jane@customer.com access to Payroll."),
            deps=access_deps(module="payroll", ticketing=ticketing),
            access=auto(),
        )
        note = " ".join(n for _, n in ticketing.add_note_calls).lower()
        assert "payroll" in note
        assert "jane@customer.com" in note
        assert "admin@customer.com" in note

    def test_a_stale_admin_record_blocks_the_grant_and_names_the_problem(self):
        provisioning = FakeProvisioning()
        ticketing = FakeTicketing()
        out = run(
            ticket("Access request", "Please give jane@customer.com access to Inventory."),
            deps=access_deps(admin_age_days=400, provisioning=provisioning, ticketing=ticketing),
            access=auto(),
        )
        assert provisioning.calls == []
        assert out["outcome"] == "escalated:admin_record_stale"
        assert "400 days" in " ".join(n for _, n in ticketing.add_note_calls)


class TestCostOfTheAccessPath:
    def test_the_access_path_does_not_enter_the_research_loop(self):
        """
        One model call, not eight. Trace B spent $3.90 and six minutes without
        reaching a decision; there is no reason for a classification task to be
        able to do that.
        """
        out = run(
            ticket("Access request", "Please give jane@customer.com access to Inventory."),
            deps=access_deps(),
            access=auto(),
        )
        assert out["steps"] <= 2

    def test_the_document_set_is_never_loaded_for_an_access_request(self):
        kb = FakeKB([doc("Docs.", version="v14", age_days=3)])
        deps = access_deps()
        deps.kb = kb
        run(
            ticket("Access request", "Please give jane@customer.com access to Inventory."),
            deps=deps,
            access=auto(),
        )
        assert kb.fetch_product_brain_calls == 0


class TestFallbackBehaviour:
    def test_an_unreadable_extraction_falls_back_to_the_normal_agent(self):
        """
        A ticket that looks like an access request but cannot be parsed is not
        dropped; it goes to the agent that handles everything else today.
        """
        deps = access_deps()
        deps.llm = FakeLLM(FakeResponse(text="I think they want access?"), replies("Let me help."))
        out = run(
            ticket("Access request", "Someone needs access to something."),
            deps=deps,
            access=auto(),
        )
        assert out["outcome"] == "resolved"

    def test_a_suspended_account_is_stopped_before_the_access_path_runs(self):
        provisioning = FakeProvisioning()
        deps = access_deps(provisioning=provisioning)
        deps.crm = FakeCRM(
            record=customer(status="suspended"),
            admin_contacts=admin_contacts(),
            directory=USERS,
        )
        out = run(
            ticket("Access request", "Please give jane@customer.com access to Inventory."),
            deps=deps,
            access=auto(),
        )
        assert provisioning.calls == []
        assert out["outcome"] == "escalated:account_not_serviceable"

    def test_an_engaged_kill_switch_stops_grants_without_a_deploy(self):
        provisioning = FakeProvisioning()
        access = AccessConfig(
            policy=Policy(autonomy=AutonomyLevel.STANDARD_AUTO),
            grant_log=GrantLog(),
            kill_switch=lambda: True,
            clock=lambda: NOW,
        )
        out = run(
            ticket("Access request", "Please give jane@customer.com access to Inventory."),
            deps=access_deps(provisioning=provisioning),
            access=access,
        )
        assert provisioning.calls == []
        assert out["outcome"] == "escalated:kill_switch_engaged"
