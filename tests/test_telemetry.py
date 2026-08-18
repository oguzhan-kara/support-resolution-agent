"""
Telemetry the inherited runtime does not emit.

Trace B made four near-identical searches and cost $3.90, yet its own trace
records tool names only (sra_runtime.py:105) and no cost at all
(sra_runtime.py:85). Both failures are invisible in the artifact that exists
to explain them. These tests pin down the trace that would have shown it.
"""

from __future__ import annotations

import json

import pytest

from telemetry import CostMeter, TicketTrace, normalise_tool_call


class TestToolCallNormalisation:
    def test_equivalent_calls_collide_so_repeats_are_detectable(self):
        a = normalise_tool_call("search_product_docs", {"query": "Payroll Export "})
        b = normalise_tool_call("search_product_docs", {"query": "payroll  export"})
        assert a == b

    def test_key_order_does_not_change_the_hash(self):
        a = normalise_tool_call("search", {"query": "x", "limit": 5})
        b = normalise_tool_call("search", {"limit": 5, "query": "x"})
        assert a == b

    def test_different_queries_do_not_collide(self):
        a = normalise_tool_call("search_product_docs", {"query": "payroll export"})
        b = normalise_tool_call("search_product_docs", {"query": "approval chain"})
        assert a != b

    def test_the_same_query_to_a_different_tool_does_not_collide(self):
        a = normalise_tool_call("search_product_docs", {"query": "timeout"})
        b = normalise_tool_call("check_known_issues", {"query": "timeout"})
        assert a != b


class TestDuplicateDetection:
    def test_trace_counts_repeated_tool_calls(self):
        """Trace B: four near-identical searches across eight steps."""
        trace = TicketTrace(ticket_id="49104")
        for step in range(4):
            trace.record_step(
                step=step,
                tool_calls=[("search_product_docs", {"query": "payroll export timeout"})],
                output="",
            )
        assert trace.duplicate_tool_calls == 3

    def test_distinct_calls_are_not_counted_as_duplicates(self):
        trace = TicketTrace(ticket_id="1")
        trace.record_step(step=0, tool_calls=[("search_product_docs", {"query": "a"})], output="")
        trace.record_step(step=1, tool_calls=[("check_known_issues", {"query": "b"})], output="")
        assert trace.duplicate_tool_calls == 0

    def test_the_trace_records_arguments_not_only_names(self):
        trace = TicketTrace(ticket_id="1")
        trace.record_step(step=0, tool_calls=[("search_product_docs", {"query": "payroll"})], output="")
        recorded = json.loads(trace.to_json())
        assert recorded["steps"][0]["tool_calls"][0]["args"] == {"query": "payroll"}


class TestCostMeter:
    def test_reports_dollars_for_tokens_consumed(self):
        meter = CostMeter(usd_per_1k_prompt=0.003, usd_per_1k_completion=0.015)
        meter.add(prompt_tokens=100_000, completion_tokens=2_000)
        assert meter.usd == pytest.approx(0.33)
        assert meter.tokens == 102_000

    def test_accumulates_across_steps(self):
        meter = CostMeter(usd_per_1k_prompt=0.003, usd_per_1k_completion=0.015)
        meter.add(prompt_tokens=1_000, completion_tokens=0)
        meter.add(prompt_tokens=1_000, completion_tokens=0)
        assert meter.usd == pytest.approx(0.006)

    def test_enforces_a_ceiling(self):
        """There is no cost ceiling in the inherited system. p95 is $2.10."""
        meter = CostMeter(usd_per_1k_prompt=0.003, usd_per_1k_completion=0.015)
        meter.add(prompt_tokens=100_000, completion_tokens=2_000)
        assert meter.exceeds(0.25) is True
        assert meter.exceeds(1.50) is False


class TestTraceOutput:
    def test_the_trace_carries_the_fields_needed_to_diagnose_trace_a_and_b(self):
        """
        Trace A answered from v13 documentation for a v14 customer. Nothing in
        the existing trace records which documents were used or how old they
        were, so that failure cannot be found by querying traces.
        """
        trace = TicketTrace(ticket_id="48812")
        trace.record_context(
            customer_product_version="v14",
            doc_versions=("v13",),
            oldest_doc_age_days=124,
        )
        trace.record_decision(outcome="resolved", reason="agent_replied", confidence=0.88)
        recorded = json.loads(trace.to_json())

        assert recorded["customer_product_version"] == "v14"
        assert recorded["doc_versions"] == ["v13"]
        assert recorded["oldest_doc_age_days"] == 124
        assert recorded["doc_version_mismatch"] is True
        assert recorded["confidence"] == 0.88
        assert "cost_usd" in recorded
        assert "duplicate_tool_calls" in recorded

    def test_matching_versions_are_not_flagged_as_a_mismatch(self):
        trace = TicketTrace(ticket_id="1")
        trace.record_context(
            customer_product_version="v14", doc_versions=("v14",), oldest_doc_age_days=3
        )
        assert json.loads(trace.to_json())["doc_version_mismatch"] is False

    def test_a_trace_is_serialisable_before_the_ticket_finishes(self):
        """
        The inherited runtime logs the trace only in finish(), so a crashed run
        leaves no trace at all (sra_runtime.py:151).
        """
        trace = TicketTrace(ticket_id="1")
        assert json.loads(trace.to_json())["outcome"] is None
