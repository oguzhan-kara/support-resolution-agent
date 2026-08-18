"""
Repairs to the inherited runtime.

Each test names the defect it pins down and the evidence for it. These are
repairs, not redesign: the prompt, the model, the tool set, and the retrieval
strategy are all unchanged.
"""

from __future__ import annotations

import pytest

from fakes import (
    BrokenKB,
    FakeCRM,
    FakeKB,
    FakeLLM,
    FakeTicketing,
    calls_tool,
    customer,
    decides,
    doc,
    make_deps,
    replies,
)
from sra_runtime import TicketContext, run


def ticket(**overrides) -> TicketContext:
    fields = {
        "ticket_id": "48812",
        "subject": "Approval workflow failing",
        "body": "We get ERR_APPROVAL_CHAIN_INVALID when submitting.",
        "sender_email": "admin@customer.com",
        "account_id": "acct-1",
    }
    fields.update(overrides)
    return TicketContext(**fields)


class TestTheEvalCanFinallySeeTheAnswer:
    def test_run_returns_the_reply_body(self):
        """
        sra_runtime.py:152 returned only ticket_id and outcome, so
        eval_harness.py:47 (`result.get("body", "")`) graded the empty string on
        every case. Whatever the 91% measured, it was not resolution correctness.
        """
        deps = make_deps(llm=FakeLLM(replies("Set the approval chain owner.")))
        result = run(ticket(), deps=deps)
        assert result["body"] == "Set the approval chain owner."

    def test_run_reports_cost_and_step_count(self):
        deps = make_deps(llm=FakeLLM(replies("ok")))
        result = run(ticket(), deps=deps)
        assert result["cost_usd"] > 0
        assert result["steps"] == 1


class TestContextIsBuiltOnce:
    def test_the_document_set_is_fetched_once_not_once_per_step(self):
        """
        sra_runtime.py:91-94 rebuilt and re-appended the full ~180-chunk
        document set on every step, so token use grew quadratically in steps.
        This is Trace B's $3.90.
        """
        kb = FakeKB()
        deps = make_deps(
            llm=FakeLLM(
                calls_tool("search_product_docs", query="approval chain"),
                calls_tool("check_known_issues", query="ERR_APPROVAL_CHAIN_INVALID"),
                replies("Here is the fix."),
            ),
            kb=kb,
        )
        run(ticket(), deps=deps)
        assert kb.fetch_product_brain_calls == 1

    def test_the_context_block_appears_once_in_the_conversation(self):
        llm = FakeLLM(
            calls_tool("search_product_docs", query="a"),
            calls_tool("check_known_issues", query="b"),
            replies("done"),
        )
        run(ticket(), deps=make_deps(llm=llm))
        blocks = [
            m for m in llm.last_messages if "=== PRODUCT DOCUMENTATION ===" in str(m.get("content"))
        ]
        assert len(blocks) == 1


class TestDecisionHandling:
    def test_an_explicit_escalation_is_not_relabelled_low_confidence(self):
        """
        sra_runtime.py:125-129 checked confidence before action, so an agent
        that deliberately escalated with low confidence was recorded as
        `low_confidence`. The escalation-reason telemetry that ops depends on
        was systematically wrong.
        """
        deps = make_deps(llm=FakeLLM(decides({"action": "escalate", "confidence": 0.4})))
        assert run(ticket(), deps=deps)["outcome"] == "escalated:agent_requested"

    def test_a_low_confidence_reply_still_escalates(self):
        deps = make_deps(llm=FakeLLM(decides({"action": "reply", "body": "maybe", "confidence": 0.4})))
        assert run(ticket(), deps=deps)["outcome"] == "escalated:low_confidence"

    def test_a_decision_missing_action_escalates_instead_of_crashing(self):
        """sra_runtime.py:128 indexed decision["action"] and could raise an
        uncaught KeyError, killing the worker and leaving the ticket stuck."""
        deps = make_deps(llm=FakeLLM(decides({"confidence": 0.9})))
        assert run(ticket(), deps=deps)["outcome"] == "escalated:malformed_response"

    def test_a_json_response_that_is_not_an_object_escalates(self):
        deps = make_deps(llm=FakeLLM(decides(5)))
        assert run(ticket(), deps=deps)["outcome"] == "escalated:malformed_response"

    def test_an_unknown_action_escalates(self):
        deps = make_deps(llm=FakeLLM(decides({"action": "delete_account", "confidence": 0.99})))
        assert run(ticket(), deps=deps)["outcome"] == "escalated:malformed_response"

    def test_a_non_numeric_confidence_escalates(self):
        deps = make_deps(llm=FakeLLM(decides({"action": "reply", "body": "x", "confidence": "high"})))
        assert run(ticket(), deps=deps)["outcome"] == "escalated:malformed_response"

    def test_a_clarify_reply_does_not_close_the_ticket(self):
        ticketing = FakeTicketing()
        deps = make_deps(
            llm=FakeLLM(decides({"action": "clarify", "body": "Which module?", "confidence": 0.9})),
            ticketing=ticketing,
        )
        run(ticket(), deps=deps)
        assert ticketing.post_reply_calls[0]["close"] is False


class TestAccountStatusIsEnforcedInCode:
    @pytest.mark.parametrize("status", ["suspended", "delinquent", "SUSPENDED", " Delinquent "])
    def test_a_non_serviceable_account_escalates_however_confident_the_model_is(self, status):
        """
        The rule existed only as prompt text (sra_runtime.py:28) with nothing
        enforcing it. A prompt is a request; this is a requirement.
        """
        deps = make_deps(
            llm=FakeLLM(replies("Here you go.", confidence=0.99)),
            crm=FakeCRM(record=customer(status=status)),
        )
        result = run(ticket(), deps=deps)
        assert result["outcome"] == "escalated:account_not_serviceable"

    def test_a_non_serviceable_account_is_never_replied_to(self):
        ticketing = FakeTicketing()
        deps = make_deps(
            llm=FakeLLM(replies("Here you go.", confidence=0.99)),
            crm=FakeCRM(record=customer(status="suspended")),
            ticketing=ticketing,
        )
        run(ticket(), deps=deps)
        assert ticketing.post_reply_calls == []

    def test_an_active_account_proceeds_normally(self):
        deps = make_deps(llm=FakeLLM(replies("ok")), crm=FakeCRM(record=customer(status="active")))
        assert run(ticket(), deps=deps)["outcome"] == "resolved"


class TestRetryPolicy:
    def test_transient_failures_are_retried(self):
        kb = BrokenKB(TimeoutError("gateway timeout"))
        deps = make_deps(
            llm=FakeLLM(calls_tool("search_product_docs", query="x"), replies("ok")), kb=kb
        )
        run(ticket(), deps=deps)
        assert kb.attempts == 3

    def test_deterministic_failures_are_not_retried(self):
        """
        sra_runtime.py:71-81 retried every exception three times with a blocking
        sleep, including failures that cannot succeed on a second attempt. Once
        a write tool exists, a blanket retry is a double-grant.
        """
        kb = BrokenKB(TypeError("search() got an unexpected keyword argument"))
        deps = make_deps(
            llm=FakeLLM(calls_tool("search_product_docs", query="x"), replies("ok")), kb=kb
        )
        run(ticket(), deps=deps)
        assert kb.attempts == 1

    def test_an_unknown_tool_name_is_not_retried(self):
        """TOOLS[name] raised KeyError, which the retry loop then retried."""
        llm = FakeLLM(calls_tool("drop_database"), replies("ok"))
        deps = make_deps(llm=llm)
        result = run(ticket(), deps=deps)
        assert result["outcome"] == "resolved"


class TestProgressAndBudget:
    def test_repeated_identical_tool_calls_stop_the_run(self):
        """
        Trace B: four near-identical searches, eight steps, $3.90, no decision.
        The agent had no way to notice it was repeating itself.
        """
        llm = FakeLLM(calls_tool("search_product_docs", query="payroll export timeout"))
        deps = make_deps(llm=llm)
        result = run(ticket(), deps=deps)
        assert result["outcome"] == "escalated:no_progress"
        assert result["steps"] < 8

    def test_a_run_that_exceeds_the_cost_ceiling_escalates(self):
        """There is no cost ceiling in the inherited system. Highest observed
        ticket: $4.80."""
        from fakes import FakeResponse, FakeToolCall

        expensive = FakeResponse(
            tool_calls=[FakeToolCall("search_product_docs", {"query": "q"})],
            prompt_tokens=400_000,
            completion_tokens=0,
        )
        deps = make_deps(llm=FakeLLM(expensive))
        result = run(ticket(), deps=deps)
        assert result["outcome"] == "escalated:cost_ceiling_exceeded"

    def test_running_out_of_steps_still_escalates(self):
        llm = FakeLLM(
            *[calls_tool("search_product_docs", query=f"distinct query {i}") for i in range(12)]
        )
        result = run(ticket(), deps=make_deps(llm=llm))
        assert result["outcome"] == "escalated:max_steps_exceeded"


class TestDocumentVersionAwareness:
    def test_an_answer_from_the_wrong_product_version_is_not_auto_closed(self):
        """
        Trace A: a clear, confident (0.88) explanation of the v13 approval
        engine sent to a customer on v14, auto-closed, reopened two days later.
        The agent could not have known — nothing in the context carried a
        version. If the only documentation we hold describes a different release
        than the customer runs, the answer is unverifiable and belongs to a human.
        """
        deps = make_deps(
            llm=FakeLLM(replies("Configure the approval chain owner.", confidence=0.88)),
            kb=FakeKB([doc("v13 approval engine behaviour.", version="v13", age_days=124)]),
            crm=FakeCRM(record=customer(product_version="v14")),
        )
        result = run(ticket(), deps=deps)
        assert result["outcome"] == "escalated:doc_version_mismatch"

    def test_a_matching_version_answers_normally(self):
        deps = make_deps(
            llm=FakeLLM(replies("Configure the approval chain owner.", confidence=0.88)),
            kb=FakeKB([doc("v14 approval engine behaviour.", version="v14", age_days=3)]),
            crm=FakeCRM(record=customer(product_version="v14")),
        )
        assert run(ticket(), deps=deps)["outcome"] == "resolved"

    def test_the_context_tells_the_model_which_version_the_customer_runs(self):
        llm = FakeLLM(replies("ok"))
        deps = make_deps(
            llm=llm,
            kb=FakeKB([doc("Docs.", version="v14", age_days=3)]),
            crm=FakeCRM(record=customer(product_version="v14")),
        )
        run(ticket(), deps=deps)
        context = " ".join(str(m.get("content")) for m in llm.last_messages)
        assert "v14" in context


class TestTheEvalCannotTouchProduction:
    def test_replies_go_through_the_injected_client(self):
        ticketing = FakeTicketing()
        run(ticket(), deps=make_deps(llm=FakeLLM(replies("ok")), ticketing=ticketing))
        assert len(ticketing.post_reply_calls) == 1

    def test_escalations_go_through_the_injected_client(self):
        ticketing = FakeTicketing()
        deps = make_deps(
            llm=FakeLLM(decides({"action": "escalate", "confidence": 0.9})), ticketing=ticketing
        )
        run(ticket(), deps=deps)
        assert ticketing.assign_to_queue_calls == [("48812", "tier2")]
