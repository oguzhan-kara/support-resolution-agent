"""
SRA offline evaluation.
Built at launch. Run manually before deploys.

Repaired at handover. The inherited version reported 91% while production QA
reported 84%, and — more damningly — it rose from 90 to 91 over the same five
months that production fell from 90 to 84. A number that moves opposite to
reality is worse than no number, because it manufactures confidence. It is the
artifact that let v14 ship without anyone noticing.

Four separate defects produced that:

  1. It graded the empty string. run() returned no `body`, and line 47 read
     `result.get("body", "")`. Whatever 91% measured, it was not resolution
     correctness. Nothing else in this list matters until that is fixed.
  2. Running it was a production event. It called the real run(), which posts
     replies to real tickets and fills the real Tier-2 queue. An eval with side
     effects is an eval people stop running.
  3. It reported one aggregate number. No category breakdown, no cost, no
     latency, no escalation analysis — so a category collapsing from 86% to 71%
     could hide inside a stable total.
  4. Its cases are frozen at launch, so for a v14 error code its reference
     answer is now the wrong answer, and its judge was parsed with
     `startswith("PASS")`, which reads FAIL for any judge that reasons first.

This file fixes the mechanics. It does not fix the corpus — that needs new
cases, and `eval_cases.json` was not part of the handover. `stale_case_share`
exists to make the corpus's age visible until it is.

For the access-request capability, see eval_access.py. Decisions with
irreversible consequences are asserted directly and are not put to a judge.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sra_runtime import Deps, TicketContext, run

MODEL = "frontier-model-v2"

# The judge should not be the same model as the agent. A model asked to grade
# its own output family has a measurable preference for it, and the inherited
# harness used one constant for both (eval_harness.py:10 and sra_runtime.py:17).
JUDGE_MODEL = "frontier-model-v1-instruct"

CURRENT_PRODUCT_VERSION = "v14"

# Fail the build when too much of the corpus predates the shipped release.
MAX_STALE_CASE_SHARE = 0.25

JUDGE_PROMPT = """You are grading a support agent's response to a customer ticket.

Ticket: {ticket}
Agent response: {response}
Reference answer: {reference}

Grade against this rubric. The response fails if any of the following is true:
- it contradicts the reference answer on a factual point
- it describes behaviour from a different product version than the customer runs
- it is confident about something the reference does not support
- it would not actually resolve the customer's problem if followed

Wording may differ from the reference. Judge the substance, not the phrasing.

Reply with exactly one line: PASS or FAIL, followed by one sentence of reason.
"""

_VERDICT = re.compile(r"\b(PASS|FAIL)\b")


def load_cases(path: str = "eval_cases.json") -> list[dict[str, Any]]:
    """
    240 cases sampled from tickets at launch, with reference answers written by
    two senior support engineers.

    Every case should carry `category`, `product_version`, and `created_at`. The
    inherited corpus carries none of them, which is why nobody could tell that
    it had aged out from under the product.
    """
    with open(path) as f:
        return json.load(f)


def judge_verdict(text: str | None) -> bool | None:
    """
    Read a verdict from the judge's reply.

    The previous implementation was `verdict.text.strip().startswith("PASS")`,
    which silently scores FAIL for any judge that states a reason before its
    answer, or that quotes its verdict. Returning None for an unreadable reply
    keeps those cases countable instead of quietly folding them into the failure
    rate — the proper fix is a structured verdict from the judge, which needs a
    platform change to the completion API.
    """
    if not text:
        return None
    first_line = next((line for line in text.strip().splitlines() if line.strip()), "")
    match = _VERDICT.search(first_line) or _VERDICT.search(text)
    if match is None:
        return None
    return match.group(1) == "PASS"


def judge(case: dict[str, Any], response: str, judge_llm: Any) -> bool | None:
    verdict = judge_llm.complete(
        model=JUDGE_MODEL,
        messages=[
            {
                "role": "user",
                "content": JUDGE_PROMPT.format(
                    ticket=case["ticket"]["body"],
                    response=response,
                    reference=case["reference_answer"],
                ),
            }
        ],
        temperature=0.0,
    )
    return judge_verdict(verdict.text)


def cohens_kappa(judge_labels: list[bool], human_labels: list[bool]) -> float:
    """
    Agreement between the LLM judge and a human grader, corrected for chance.

    A judge nobody has checked is an opinion with a percentage sign on it. Double
    grade a sample of cases and report this alongside the score; below about 0.7
    the headline number should not be quoted to anyone. Raw agreement is not
    enough — on a suite that is 90% passes, a judge that always says PASS gets
    90% agreement and knows nothing.
    """
    if len(judge_labels) != len(human_labels) or not judge_labels:
        raise ValueError("label lists must be non-empty and the same length")

    n = len(judge_labels)
    observed = sum(a == b for a, b in zip(judge_labels, human_labels)) / n

    judge_pass = sum(judge_labels) / n
    human_pass = sum(human_labels) / n
    expected = judge_pass * human_pass + (1 - judge_pass) * (1 - human_pass)

    if expected == 1.0:
        return 1.0
    return (observed - expected) / (1 - expected)


@dataclass(frozen=True)
class CategoryResult:
    passed: int = 0
    total: int = 0
    unreadable: int = 0

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


@dataclass(frozen=True)
class SuiteResult:
    by_category: dict[str, CategoryResult]
    outcomes: Counter
    costs: list[float]
    stale_case_share: float
    unreadable_verdicts: int = 0

    @property
    def total(self) -> int:
        return sum(c.total for c in self.by_category.values())

    @property
    def overall(self) -> float:
        passed = sum(c.passed for c in self.by_category.values())
        return passed / self.total if self.total else 0.0

    @property
    def cost_p50(self) -> float:
        return statistics.median(self.costs) if self.costs else 0.0

    @property
    def cost_p95(self) -> float:
        if not self.costs:
            return 0.0
        ordered = sorted(self.costs)
        # Nearest-rank, so small suites report the observed tail rather than an
        # interpolated value that never occurred.
        index = min(len(ordered) - 1, int(round(0.95 * len(ordered) + 0.5)) - 1)
        return ordered[index]

    def render(self) -> str:
        lines = [
            f"cases: {self.total}    overall: {self.overall:.1%}",
            f"cost p50: ${self.cost_p50:.4f}    cost p95: ${self.cost_p95:.4f}",
            f"corpus predating {CURRENT_PRODUCT_VERSION}: {self.stale_case_share:.0%}"
            + ("   <-- ABOVE THRESHOLD" if self.stale_case_share > MAX_STALE_CASE_SHARE else ""),
            "",
            "by category:",
        ]
        for name in sorted(self.by_category):
            result = self.by_category[name]
            lines.append(f"  {name:<16} {result.rate:>6.1%}  ({result.passed}/{result.total})")
        lines.append("")
        lines.append("outcomes:")
        for outcome, count in sorted(self.outcomes.items()):
            lines.append(f"  {outcome:<40} {count}")
        if self.unreadable_verdicts:
            lines.append("")
            lines.append(f"unreadable judge verdicts: {self.unreadable_verdicts}")
        return "\n".join(lines)


class RecordingTicketing:
    """
    Reads pass through to the real client; writes are recorded and discarded.

    Reading production data during an eval is legitimate and often necessary.
    Writing to it never is. Splitting the two along that line is what makes it
    safe to run this on a schedule rather than by hand before a deploy.
    """

    def __init__(self, reader: Any = None) -> None:
        self._reader = reader
        self.post_reply_calls: list[dict[str, Any]] = []
        self.add_note_calls: list[tuple[str, str]] = []
        self.assign_to_queue_calls: list[tuple[str, str]] = []

    def recent_tickets(self, account_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self._reader.recent_tickets(account_id, limit) if self._reader else []

    def history(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self._reader.history(*args, **kwargs) if self._reader else []

    def post_reply(self, ticket_id: str, body: str, close: bool = False) -> None:
        self.post_reply_calls.append({"ticket_id": ticket_id, "body": body, "close": close})

    def add_note(self, ticket_id: str, note: str) -> None:
        self.add_note_calls.append((ticket_id, note))

    def assign_to_queue(self, ticket_id: str, queue: str) -> None:
        self.assign_to_queue_calls.append((ticket_id, queue))


def _refuse_production_clients(deps: Deps) -> None:
    """
    Refuse to run the suite against clients that can write to production.

    This is the inherited harness's worst property expressed as a guard rather
    than as a comment. Wrap production reads in RecordingTicketing instead.
    """
    import platform_sdk

    for name in ("ticketing", "provisioning"):
        if getattr(deps, name) is getattr(platform_sdk, name):
            raise RuntimeError(
                f"run_suite() was handed the production {name} client. This eval "
                f"used to post replies to real tickets and fill the real Tier-2 "
                f"queue. Pass RecordingTicketing(platform_sdk.ticketing) instead."
            )


def run_suite(cases: list[dict[str, Any]], *, deps: Deps, judge_llm: Any) -> SuiteResult:
    """
    Run the suite against injected clients.

    `deps` and `judge_llm` are required rather than defaulted, so that pointing
    this at production is something you have to type on purpose — and then it is
    refused anyway.
    """
    _refuse_production_clients(deps)

    tallies: dict[str, dict[str, int]] = {}
    outcomes: Counter = Counter()
    costs: list[float] = []
    unreadable = 0

    for case in cases:
        ctx = TicketContext(**case["ticket"])
        result = run(ctx, deps=deps)

        outcomes[result["outcome"]] += 1
        costs.append(result["cost_usd"])

        # Grade the actual reply. The inherited harness read a key run() never
        # returned, so this was "" on every case.
        verdict = judge(case, result["body"], judge_llm)
        if verdict is None:
            unreadable += 1

        category = case.get("category", "uncategorised")
        tally = tallies.setdefault(category, {"passed": 0, "total": 0, "unreadable": 0})
        tally["total"] += 1
        tally["passed"] += 1 if verdict else 0
        tally["unreadable"] += 1 if verdict is None else 0

    stale = sum(1 for c in cases if c.get("product_version") != CURRENT_PRODUCT_VERSION)

    return SuiteResult(
        by_category={name: CategoryResult(**counts) for name, counts in tallies.items()},
        outcomes=outcomes,
        costs=costs,
        stale_case_share=stale / len(cases) if cases else 0.0,
        unreadable_verdicts=unreadable,
    )


def main() -> None:
    import platform_sdk

    cases = load_cases()
    # Real reads, discarded writes.
    deps = Deps(ticketing=RecordingTicketing(platform_sdk.ticketing))
    result = run_suite(cases, deps=deps, judge_llm=platform_sdk.llm)
    print(result.render())

    if result.stale_case_share > MAX_STALE_CASE_SHARE:
        raise SystemExit(
            f"{result.stale_case_share:.0%} of the corpus predates "
            f"{CURRENT_PRODUCT_VERSION}. Refresh the cases before trusting this "
            f"score — a suite frozen at launch cannot fail on the drift that is "
            f"hurting production."
        )


if __name__ == "__main__":
    main()
