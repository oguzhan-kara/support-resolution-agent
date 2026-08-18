"""
Tests for the evaluation suite itself.

The inherited eval scored 91% while production fell to 84%, and rose while
production fell. Its problem was not a wrong threshold; it was that nobody had
ever checked whether it could fail. These tests check that.
"""

from __future__ import annotations

import pytest

from access_request import Decision, ReasonCode
from eval_access import MUTATIONS, run_access_suite, run_with_mutation
from eval_cases_access import CURRENT_VERSION, DECISION_CASES, PIPELINE_CASES


class TestTheSuitePasses:
    def test_there_are_no_false_grants(self):
        """The gate. Everything else in this file is secondary to this line."""
        report = run_access_suite()
        assert report.false_grants == (), [o.case_id for o in report.false_grants]

    def test_every_case_lands_on_its_expected_decision_and_reason(self):
        report = run_access_suite()
        assert report.mismatches == (), [
            f"{o.case_id}: expected {o.expected_decision}/{o.expected_reason}, "
            f"got {o.actual_decision}/{o.actual_reason}"
            for o in report.mismatches
        ]

    def test_the_suite_still_automates_something(self):
        """
        A capability that refuses everything passes a safety suite trivially.
        This asserts the suite would notice if the gates were tightened into
        uselessness.
        """
        assert run_access_suite().grant_rate > 0


class TestTheSuiteCanFail:
    @pytest.mark.parametrize("mutation", MUTATIONS)
    def test_disabling_any_gate_is_detected(self, mutation):
        """
        Break one gate; the suite must notice. A suite that still passes with
        the freshness gate removed is decorative — which is exactly how the
        inherited eval managed to rise from 90 to 91 while production fell to 84.
        """
        report = run_with_mutation(mutation)
        assert report.false_grants or report.mismatches, (
            f"the suite still passes with '{mutation}' disabled, so it does not "
            f"actually test that gate"
        )

    def test_trusting_the_model_is_detected(self):
        """
        The Director's request implemented literally: believe what the extractor
        returns. If this mutation survived, the injection cases would be
        decoration and the argument for the real design would be unsupported.
        """
        report = run_with_mutation("trust_the_model")
        assert report.false_grants

    def test_removing_the_payroll_classification_produces_false_grants(self):
        report = run_with_mutation("treat_restricted_as_standard")
        assert len(report.false_grants) >= 3


class TestCorpusHealth:
    def test_every_reason_code_is_exercised(self):
        """
        Couples the corpus to the enum on purpose. A new way to refuse should not
        ship without a case proving it refuses.
        """
        report = run_access_suite()
        covered = {o.actual_reason for o in report.outcomes if o.actual_reason is not None}
        assert covered == set(ReasonCode), set(ReasonCode) - covered

    def test_every_decision_outcome_is_exercised(self):
        report = run_access_suite()
        assert {o.actual_decision for o in report.outcomes if o.actual_decision} == set(Decision)

    def test_the_corpus_is_not_frozen_behind_the_shipped_release(self):
        """
        The inherited suite was built at launch and never updated, so for a v14
        error code its reference answer is now the wrong answer. This fails the
        build before that can happen again.
        """
        cases = list(DECISION_CASES) + list(PIPELINE_CASES)
        current = sum(1 for c in cases if c.product_version == CURRENT_VERSION)
        assert current / len(cases) >= 0.75

    def test_every_case_states_the_threat_it_guards(self):
        for case in list(DECISION_CASES) + list(PIPELINE_CASES):
            assert len(case.threat) > 30, case.id

    def test_case_ids_are_unique(self):
        ids = [c.id for c in list(DECISION_CASES) + list(PIPELINE_CASES)]
        assert len(set(ids)) == len(ids)

    def test_the_corpus_covers_both_sides_of_the_freshness_boundary(self):
        ages = {c.admin_age_days for c in DECISION_CASES}
        assert 90 in ages and 91 in ages, "an untested threshold is an unenforced one"


class TestReporting:
    def test_the_report_is_a_matrix_not_a_single_number(self):
        rendered = run_access_suite().render()
        assert "confusion matrix" in rendered
        assert "false grants:     0" in rendered
        assert "grant" in rendered

    def test_false_grants_are_named_with_the_threat_they_missed(self):
        rendered = run_with_mutation("treat_restricted_as_standard").render()
        assert "FALSE GRANTS:" in rendered
        assert "payroll" in rendered.lower()

    def test_automation_rate_is_reported_but_is_not_the_headline(self):
        rendered = run_access_suite().render()
        assert rendered.index("false grants") < rendered.index("automation rate")
