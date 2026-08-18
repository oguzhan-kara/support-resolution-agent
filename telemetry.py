"""
Per-ticket telemetry.

The inherited runtime carries two TODOs that turned out to be the same problem:
cost tracking was never built, and the knowledge refresh job fails silently.
Neither is visible in the trace, so neither shows up when you go looking.

Concretely, the existing trace (sra_runtime.py:103-107) records tool *names*
but not arguments, no token or cost figures, no document versions, and is
written only in finish() — so a run that crashes leaves nothing behind. The two
production traces we were handed are both diagnosable only because a human
watched them happen.

This module records what a query should have been able to answer:

  - which tool calls repeated, and how often          -> Trace B
  - what a ticket cost                                -> the $65k/month question
  - which document versions were used, and how old    -> Trace A

Standard library only. No platform dependency, so it is safe to import anywhere.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

_WHITESPACE = re.compile(r"\s+")


def normalise_tool_call(name: str, args: dict[str, Any]) -> str:
    """
    Return a stable hash for a tool call, collapsing trivial differences.

    Trace B issued four searches with "near-identical" queries. Whether those
    were byte-identical or merely similar, the agent had no way to notice it was
    repeating itself, and neither did anyone reading the trace afterwards.
    Normalising case and whitespace makes the common repetition detectable; it
    deliberately does not attempt semantic similarity, because a hash that
    sometimes collides unrelated calls would be worse than one that occasionally
    misses a near-duplicate.
    """
    canonical = {
        key: _WHITESPACE.sub(" ", value.strip().lower()) if isinstance(value, str) else value
        for key, value in sorted(args.items())
    }
    payload = json.dumps([name, canonical], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class CostMeter:
    """
    Token and dollar accounting for one ticket.

    The inherited system has no cost ceiling and no per-ticket tracking, which
    is survivable at 2,400 tickets a month and is not at 31,000. Today's p95 of
    $2.10, if it became the mean, is roughly $65k a month.
    """

    usd_per_1k_prompt: float
    usd_per_1k_completion: float
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def usd(self) -> float:
        return (
            self.prompt_tokens / 1_000 * self.usd_per_1k_prompt
            + self.completion_tokens / 1_000 * self.usd_per_1k_completion
        )

    def exceeds(self, limit_usd: float) -> bool:
        return self.usd > limit_usd


@dataclass
class TicketTrace:
    """
    One ticket's execution record.

    Serialisable at any point, not only at the end, so a run that dies still
    leaves an artifact behind.
    """

    ticket_id: str
    started_at: float = field(default_factory=time.time)
    steps: list[dict[str, Any]] = field(default_factory=list)
    cost: CostMeter = field(
        default_factory=lambda: CostMeter(usd_per_1k_prompt=0.003, usd_per_1k_completion=0.015)
    )

    customer_product_version: str | None = None
    doc_versions: tuple[str, ...] = ()
    oldest_doc_age_days: int | None = None

    outcome: str | None = None
    reason: str | None = None
    confidence: float | None = None

    _call_hashes: Counter[str] = field(default_factory=Counter, repr=False)

    def record_step(
        self,
        step: int,
        tool_calls: list[tuple[str, dict[str, Any]]],
        output: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        for name, args in tool_calls:
            self._call_hashes[normalise_tool_call(name, args)] += 1

        self.cost.add(prompt_tokens, completion_tokens)
        self.steps.append(
            {
                "step": step,
                # Arguments, not just names. Without these Trace B is
                # undiagnosable from its own trace.
                "tool_calls": [{"name": name, "args": args} for name, args in tool_calls],
                "output": output[:500],
                "cost_usd_cumulative": round(self.cost.usd, 6),
            }
        )

    def record_context(
        self,
        customer_product_version: str | None,
        doc_versions: tuple[str, ...],
        oldest_doc_age_days: int | None,
    ) -> None:
        self.customer_product_version = customer_product_version
        self.doc_versions = tuple(doc_versions)
        self.oldest_doc_age_days = oldest_doc_age_days

    def record_decision(
        self, outcome: str, reason: str | None = None, confidence: float | None = None
    ) -> None:
        self.outcome = outcome
        self.reason = reason
        self.confidence = confidence

    @property
    def duplicate_tool_calls(self) -> int:
        """Total calls minus distinct calls."""
        return sum(self._call_hashes.values()) - len(self._call_hashes)

    @property
    def doc_version_mismatch(self) -> bool:
        """
        True when the agent answered from documentation that does not match the
        customer's product version.

        This is the single field that would have caught Trace A: a confident,
        well-written answer describing the v13 approval engine for a customer on
        v14. Aggregated across production it also settles whether stale
        documentation *causes* the error-category decline from 86% to 71%, which
        today is a correlation and an anecdote.
        """
        if self.customer_product_version is None or not self.doc_versions:
            return False
        return any(version != self.customer_product_version for version in self.doc_versions)

    def to_json(self) -> str:
        return json.dumps(
            {
                "ticket_id": self.ticket_id,
                "started_at": self.started_at,
                "duration_s": round(time.time() - self.started_at, 3),
                "steps": self.steps,
                "step_count": len(self.steps),
                "duplicate_tool_calls": self.duplicate_tool_calls,
                "cost_usd": round(self.cost.usd, 6),
                "tokens": self.cost.tokens,
                "customer_product_version": self.customer_product_version,
                "doc_versions": list(self.doc_versions),
                "oldest_doc_age_days": self.oldest_doc_age_days,
                "doc_version_mismatch": self.doc_version_mismatch,
                "outcome": self.outcome,
                "reason": self.reason,
                "confidence": self.confidence,
            }
        )
