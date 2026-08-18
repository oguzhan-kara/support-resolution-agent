"""
Repairs to the inherited eval harness.

The harness reported 91% while production QA reported 84%, and the eval rose
from 90 to 91 over the same period that production fell from 90 to 84. A number
that moves opposite to reality is worse than no number, because it manufactures
confidence. These tests pin down why it did that.
"""

from __future__ import annotations

import pytest

from eval_harness import (
    CURRENT_PRODUCT_VERSION,
    cohens_kappa,
    judge_verdict,
    run_suite,
)
from fakes import FakeCRM, FakeKB, FakeLLM, FakeResponse, FakeTicketing, customer, doc, make_deps, replies


def case(
    case_id: str = "c1",
    category: str = "error",
    product_version: str = CURRENT_PRODUCT_VERSION,
    reference: str = "Set the approval chain owner.",
) -> dict:
    return {
        "id": case_id,
        "category": category,
        "product_version": product_version,
        "created_at": "2026-06-01",
        "reference_answer": reference,
        "ticket": {
            "ticket_id": case_id,
            "subject": "Approval workflow failing",
            "body": "ERR_APPROVAL_CHAIN_INVALID on submit.",
            "sender_email": "admin@customer.com",
            "account_id": "acct-1",
        },
    }


def passing_judge() -> FakeLLM:
    return FakeLLM(FakeResponse(text="PASS — matches the reference answer."))


def agent_deps(ticketing: FakeTicketing | None = None):
    return make_deps(
        llm=FakeLLM(replies("Set the approval chain owner.")),
        kb=FakeKB([doc("Approval chain docs.", version="v14", age_days=3)]),
        crm=FakeCRM(record=customer(product_version="v14")),
        ticketing=ticketing or FakeTicketing(),
    )


class TestTheHarnessCannotTouchProduction:
    def test_running_against_the_production_client_is_refused(self):
        """
        eval_harness.py:44 called the real run(), which posts replies
        (sra_runtime.py:132,135) and assigns to the real Tier-2 queue (:143).
        Running the eval was itself a production event — the most common reason
        an eval quietly stops being run. Refusing beats documenting.
        """
        import platform_sdk

        deps = agent_deps()
        deps.ticketing = platform_sdk.ticketing
        with pytest.raises(RuntimeError, match="production"):
            run_suite([case()], deps=deps, judge_llm=passing_judge())

    def test_side_effects_are_contained_by_the_injected_client(self):
        ticketing = FakeTicketing()
        run_suite([case()], deps=agent_deps(ticketing), judge_llm=passing_judge())
        assert [c["ticket_id"] for c in ticketing.post_reply_calls] == ["c1"]

    def test_the_recording_client_reads_through_but_discards_writes(self):
        """Reads against production data are legitimate for an eval. Writes never are."""
        from eval_harness import RecordingTicketing

        upstream = FakeTicketing(recent=[{"id": "t-9"}])
        recorder = RecordingTicketing(upstream)

        assert recorder.recent_tickets("acct-1") == [{"id": "t-9"}]

        recorder.post_reply("t-1", "body", close=True)
        assert upstream.post_reply_calls == []
        assert recorder.post_reply_calls == [{"ticket_id": "t-1", "body": "body", "close": True}]


class TestTheHarnessGradesARealAnswer:
    def test_the_graded_response_is_not_empty(self):
        judge = passing_judge()
        run_suite([case()], deps=agent_deps(), judge_llm=judge)
        graded = judge.calls[0]["messages"][0]["content"]
        assert "Set the approval chain owner." in graded

    def test_a_correct_answer_passes(self):
        result = run_suite([case()], deps=agent_deps(), judge_llm=passing_judge())
        assert result.overall == 1.0


class TestResultsAreNotCollapsedToOneNumber:
    def test_results_break_down_by_category(self):
        cases = [
            case("a", category="informational"),
            case("b", category="configuration"),
            case("c", category="error"),
            case("d", category="access"),
        ]
        result = run_suite(cases, deps=agent_deps(), judge_llm=passing_judge())
        assert set(result.by_category) == {"informational", "configuration", "error", "access"}

    def test_cost_is_reported_at_the_tail_not_only_the_mean(self):
        """Mean cost is $0.34 and p95 is $2.10. Planning from the mean is how
        a $10.5k/month forecast becomes $65k."""
        result = run_suite([case("a"), case("b")], deps=agent_deps(), judge_llm=passing_judge())
        assert result.cost_p95 >= result.cost_p50 > 0

    def test_outcomes_are_counted_so_escalations_are_visible(self):
        result = run_suite([case()], deps=agent_deps(), judge_llm=passing_judge())
        assert sum(result.outcomes.values()) == 1

    def test_the_report_renders_the_breakdown(self):
        rendered = run_suite([case()], deps=agent_deps(), judge_llm=passing_judge()).render()
        assert "error" in rendered and "p95" in rendered


class TestCorpusStaleness:
    def test_cases_written_for_an_older_release_are_flagged(self):
        """
        The 240 cases were built at launch and never updated. v14 shipped ten
        weeks ago and changed the permissions model, the approval workflow
        engine, and several error codes — so for a v14 error code the suite's
        reference answer is now the wrong answer. The eval is structurally
        incapable of failing on the drift that is hurting production.
        """
        result = run_suite(
            [case("a", product_version="v13"), case("b", product_version="v14")],
            deps=agent_deps(),
            judge_llm=passing_judge(),
        )
        assert result.stale_case_share == 0.5

    def test_a_fresh_corpus_is_not_flagged(self):
        result = run_suite([case()], deps=agent_deps(), judge_llm=passing_judge())
        assert result.stale_case_share == 0.0


class TestJudgeParsing:
    def test_a_verdict_that_does_not_start_the_string_is_still_read(self):
        """`verdict.text.strip().startswith("PASS")` (eval_harness.py:38) reads
        FAIL for any judge that reasons before answering."""
        assert judge_verdict("The agent was correct. PASS") is True

    def test_a_quoted_verdict_is_read(self):
        assert judge_verdict('"FAIL" — cited the wrong product version') is False

    def test_a_bare_verdict_is_read(self):
        assert judge_verdict("PASS") is True

    def test_an_unreadable_verdict_is_none_rather_than_silently_failing(self):
        assert judge_verdict("I am not sure about this one.") is None

    def test_the_word_passenger_does_not_count_as_a_pass(self):
        assert judge_verdict("The agent mentioned passengers.") is None


class TestJudgeAccountability:
    def test_perfect_agreement_scores_one(self):
        assert cohens_kappa([True, False, True], [True, False, True]) == pytest.approx(1.0)

    def test_chance_agreement_scores_near_zero(self):
        judge = [True, True, False, False]
        human = [True, False, True, False]
        assert cohens_kappa(judge, human) == pytest.approx(0.0)

    def test_disagreement_scores_negative(self):
        assert cohens_kappa([True, True], [False, False]) < 0.5
