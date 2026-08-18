"""
Evaluation for the access-request capability.

The inherited harness reported one number. That is the wrong shape for this
capability, because the costs here are not symmetric: a wrong escalation costs a
support engineer four minutes, and a wrong grant is an unauthorised person
holding payroll data. Averaging those into an accuracy figure hides the only
outcome that matters.

So the headline is **false grants**, and the gate is zero. Automation rate is
reported alongside it and is optimised only under that constraint — never traded
against it.

Three things this does that the inherited harness could not:

  It asserts on decisions, not on prose. The output is a typed decision, so it is
  checked directly. No model grades a security property.

  It runs with no clock, no network, and no fixtures, because `authorize()` is
  pure. The whole suite is about a tenth of a second, which is what makes it
  affordable on every commit rather than by hand before a release.

  It tests itself. Each gate is disabled in turn and the suite must fail. A suite
  that still passes with the freshness gate removed is decorative — and that is
  precisely how the inherited eval failed, scoring 91% while production fell to
  84%. Mutation testing is the only mechanism here that structurally prevents me
  from repeating it.

Run: python eval_access.py
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import access_request as ar
from access_request import (
    AccessRequest,
    Decision,
    ReasonCode,
    Sensitivity,
    authorize,
    extract_access_request,
    looks_like_access_request,
)
from eval_cases_access import DECISION_CASES, PIPELINE_CASES, DecisionCase, PipelineCase


class _RecordedLLM:
    """Replays one recorded extraction response."""

    def __init__(self, payload: str) -> None:
        self.payload = payload

    def complete(self, **_: Any) -> Any:
        return type("Response", (), {"text": self.payload})()


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    threat: str
    expected_decision: Decision | None
    actual_decision: Decision | None
    expected_reason: ReasonCode | None
    actual_reason: ReasonCode | None
    # Trusted fields that did not survive extraction as they should have.
    field_mismatches: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            self.actual_decision == self.expected_decision
            and self.actual_reason == self.expected_reason
            and not self.field_mismatches
        )

    @property
    def is_false_grant(self) -> bool:
        """Granted when the case said it should not have. The only fatal outcome."""
        return self.actual_decision is Decision.GRANT and self.expected_decision is not Decision.GRANT


def _trusted_field_mismatches(case: PipelineCase, request: AccessRequest) -> tuple[str, ...]:
    """
    Check that ticket metadata won, not the ticket body.

    Asserting the decision alone is not enough. An implementation that believes
    the body often reaches the same decision by luck, and a case that cannot
    distinguish the two proves nothing about the boundary it claims to test.
    """
    mismatches = []
    if case.expect_account_id is not None and request.account_id != case.expect_account_id:
        mismatches.append(f"account_id={request.account_id!r}")
    if (
        case.expect_requester_email is not None
        and request.requester_email != case.expect_requester_email
    ):
        mismatches.append(f"requester_email={request.requester_email!r}")
    return tuple(mismatches)


@dataclass(frozen=True)
class SuiteReport:
    outcomes: tuple[CaseOutcome, ...]

    @property
    def false_grants(self) -> tuple[CaseOutcome, ...]:
        return tuple(o for o in self.outcomes if o.is_false_grant)

    @property
    def mismatches(self) -> tuple[CaseOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.passed)

    @property
    def matrix(self) -> dict[tuple[Any, Any], int]:
        counts: dict[tuple[Any, Any], int] = {}
        for outcome in self.outcomes:
            key = (outcome.expected_decision, outcome.actual_decision)
            counts[key] = counts.get(key, 0) + 1
        return counts

    @property
    def by_reason(self) -> dict[ReasonCode | None, int]:
        counts: dict[ReasonCode | None, int] = {}
        for outcome in self.outcomes:
            counts[outcome.actual_reason] = counts.get(outcome.actual_reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0]))))

    @property
    def grant_rate(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(1 for o in self.outcomes if o.actual_decision is Decision.GRANT) / len(
            self.outcomes
        )

    @property
    def automation_rate(self) -> float:
        """
        Share of requests a human does not have to work from scratch: either
        granted outright, or handed over as a filled-in action to confirm.

        Reported second, always. Raising this number by loosening a gate is how
        the wrong person ends up with payroll access.
        """
        if not self.outcomes:
            return 0.0
        handled = (Decision.GRANT, Decision.PREPARE_FOR_APPROVAL)
        return sum(1 for o in self.outcomes if o.actual_decision in handled) / len(self.outcomes)

    def render(self) -> str:
        lines = [
            "=" * 68,
            "ACCESS-REQUEST EVALUATION",
            "=" * 68,
            "",
            f"cases:            {len(self.outcomes)}",
            f"false grants:     {len(self.false_grants)}      <-- the gate; must be 0",
            f"mismatches:       {len(self.mismatches)}",
            f"grant rate:       {self.grant_rate:.1%}",
            f"automation rate:  {self.automation_rate:.1%}   (granted or prepared)",
            "",
            "confusion matrix (expected -> actual):",
        ]
        for (expected, actual), count in sorted(
            self.matrix.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))
        ):
            marker = "" if expected == actual else "   <-- MISMATCH"
            lines.append(
                f"  {str(expected or 'none'):<22} -> {str(actual or 'none'):<22} {count:>3}{marker}"
            )

        lines += ["", "outcomes by reason:"]
        for reason, count in self.by_reason.items():
            lines.append(f"  {str(reason or 'none'):<38} {count:>3}")

        if self.false_grants:
            lines += ["", "FALSE GRANTS:"]
            for outcome in self.false_grants:
                lines.append(f"  {outcome.case_id}: {outcome.threat}")

        if self.mismatches:
            lines += ["", "mismatches:"]
            for outcome in self.mismatches:
                lines.append(
                    f"  {outcome.case_id}: expected {outcome.expected_decision}"
                    f"/{outcome.expected_reason}, got {outcome.actual_decision}"
                    f"/{outcome.actual_reason}"
                    + (
                        f"   trusted fields overridden: {', '.join(outcome.field_mismatches)}"
                        if outcome.field_mismatches
                        else ""
                    )
                )

        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Running the suites
# --------------------------------------------------------------------------- #


def run_decision_case(case: DecisionCase) -> CaseOutcome:
    result = authorize(**case.world())
    return CaseOutcome(
        case_id=case.id,
        threat=case.threat,
        expected_decision=case.expect_decision,
        actual_decision=result.decision,
        expected_reason=case.expect_reason,
        actual_reason=result.reason,
    )


def run_pipeline_case(case: PipelineCase) -> CaseOutcome:
    """Routing, extraction, and authorization end to end."""
    routed = looks_like_access_request(case.subject, case.body)

    if not routed:
        return CaseOutcome(
            case_id=case.id,
            threat=case.threat,
            expected_decision=case.expect_decision,
            actual_decision=None,
            expected_reason=case.expect_reason,
            actual_reason=None if case.expect_routed else None,
        )

    request = extract_access_request(
        _RecordedLLM(case.model_output),
        case.subject,
        case.body,
        account_id="acct-1",
        sender_email=case.sender_email,
    )

    if request is None:
        return CaseOutcome(
            case_id=case.id,
            threat=case.threat,
            expected_decision=case.expect_decision,
            actual_decision=None,
            expected_reason=case.expect_reason,
            actual_reason=ReasonCode.NOT_AN_ACCESS_REQUEST,
        )

    result = authorize(request=request, **case.world_without_request())
    return CaseOutcome(
        case_id=case.id,
        threat=case.threat,
        expected_decision=case.expect_decision,
        actual_decision=result.decision,
        expected_reason=case.expect_reason,
        actual_reason=result.reason,
        field_mismatches=_trusted_field_mismatches(case, request),
    )


def run_access_suite(
    decision_cases: tuple[DecisionCase, ...] = DECISION_CASES,
    pipeline_cases: tuple[PipelineCase, ...] = PIPELINE_CASES,
) -> SuiteReport:
    outcomes = [run_decision_case(c) for c in decision_cases]
    outcomes += [run_pipeline_case(c) for c in pipeline_cases]
    return SuiteReport(outcomes=tuple(outcomes))


# --------------------------------------------------------------------------- #
# Mutation testing — does this suite actually test anything?
# --------------------------------------------------------------------------- #


def _naive_extract(
    llm_client: Any, ticket_subject: str, ticket_body: str, account_id: str, sender_email: str
) -> AccessRequest | None:
    """
    The Director's request implemented literally: trust what the model returns.

    This exists to be mutated in. If the suite still passes with this
    substituted, then none of the injection cases are doing any work and the
    argument for the real implementation is unsupported.
    """
    try:
        raw = json.loads(llm_client.complete().text)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return AccessRequest(
        account_id=raw.get("account_id", account_id),
        requester_email=raw.get("requester_email", sender_email),
        target_emails=tuple(raw.get("target_emails", [])),
        module=ar.resolve_module(raw.get("module", "")),
        raw_module_text=str(raw.get("module", "")),
        target_hint=str(raw.get("target_hint", "")),
        is_revocation=bool(raw.get("is_revocation", False)),
    )


MUTATIONS: tuple[str, ...] = (
    "disable_freshness_gate",
    "disable_admin_check",
    "disable_domain_check",
    "disable_account_status_check",
    "treat_restricted_as_standard",
    "treat_unknown_module_as_standard",
    "guess_unknown_modules",
    "trust_the_model",
)


@contextmanager
def _mutated(name: str) -> Iterator[None]:
    """Break one gate, then put it back."""
    originals: dict[str, Any] = {}

    def patch(attribute: str, value: Any) -> None:
        originals[attribute] = getattr(ar, attribute)
        setattr(ar, attribute, value)

    if name == "disable_freshness_gate":
        original_age = ar.AdminContactRecord.age_days
        ar.AdminContactRecord.age_days = lambda self, now: 0  # type: ignore[assignment]
    elif name == "disable_admin_check":
        patch("is_listed_admin", lambda requester, admins: True)
    elif name == "disable_domain_check":
        patch("domain_belongs_to_account", lambda email, domains: True)
    elif name == "disable_account_status_check":
        patch("NON_SERVICEABLE", {})
    elif name == "treat_restricted_as_standard":
        patch("MODULE_REGISTRY", {k: Sensitivity.STANDARD for k in ar.MODULE_REGISTRY})
    elif name == "treat_unknown_module_as_standard":
        patch("classify_module", lambda module: Sensitivity.STANDARD)
    elif name == "guess_unknown_modules":
        patch("resolve_module", lambda raw: "inventory")
    elif name == "trust_the_model":
        patch("extract_access_request", _naive_extract)
    else:
        raise ValueError(f"unknown mutation: {name}")

    try:
        yield
    finally:
        if name == "disable_freshness_gate":
            ar.AdminContactRecord.age_days = original_age  # type: ignore[assignment]
        for attribute, value in originals.items():
            setattr(ar, attribute, value)


def run_with_mutation(name: str) -> SuiteReport:
    """
    Run the suite with one gate disabled.

    Every mutation must produce a failure. If one does not, the suite is not
    testing that gate and the gate is unverified.
    """
    with _mutated(name):
        # Rebound so patched module attributes take effect inside authorize().
        decision_outcomes = [
            CaseOutcome(
                case_id=case.id,
                threat=case.threat,
                expected_decision=case.expect_decision,
                actual_decision=(r := ar.authorize(**case.world())).decision,
                expected_reason=case.expect_reason,
                actual_reason=r.reason,
            )
            for case in DECISION_CASES
        ]
        pipeline_outcomes = [_run_pipeline_case_mutated(case) for case in PIPELINE_CASES]

    return SuiteReport(outcomes=tuple(decision_outcomes + pipeline_outcomes))


def _run_pipeline_case_mutated(case: PipelineCase) -> CaseOutcome:
    if not ar.looks_like_access_request(case.subject, case.body):
        return CaseOutcome(
            case.id, case.threat, case.expect_decision, None, case.expect_reason, None
        )

    request = ar.extract_access_request(
        _RecordedLLM(case.model_output), case.subject, case.body, "acct-1", case.sender_email
    )
    if request is None:
        return CaseOutcome(
            case.id,
            case.threat,
            case.expect_decision,
            None,
            case.expect_reason,
            ReasonCode.NOT_AN_ACCESS_REQUEST,
        )

    result = ar.authorize(request=request, **case.world_without_request())
    return CaseOutcome(
        case.id,
        case.threat,
        case.expect_decision,
        result.decision,
        case.expect_reason,
        result.reason,
        _trusted_field_mismatches(case, request),
    )


def main() -> None:
    report = run_access_suite()
    print(report.render())

    print()
    print("=" * 68)
    print("MUTATION TESTING - each gate disabled in turn; every row must FAIL")
    print("=" * 68)
    print()
    surviving: list[str] = []
    for mutation in MUTATIONS:
        mutated = run_with_mutation(mutation)
        detected = bool(mutated.false_grants or mutated.mismatches)
        if not detected:
            surviving.append(mutation)
        print(
            f"  {mutation:<34} "
            f"{'detected' if detected else 'SURVIVED':<10} "
            f"false grants: {len(mutated.false_grants):>2}   "
            f"mismatches: {len(mutated.mismatches):>2}"
        )

    print()
    if report.false_grants:
        raise SystemExit(f"FAILED: {len(report.false_grants)} false grants.")
    if surviving:
        raise SystemExit(
            f"FAILED: mutations survived undetected: {', '.join(surviving)}. "
            f"The suite does not test those gates."
        )
    print("PASS: no false grants, and every disabled gate was detected.")


if __name__ == "__main__":
    main()
