"""
Support Resolution Agent (SRA) — runtime
Tier-1 ticket handling. Live since March.

Deployed as a worker. One invocation per inbound ticket.

Repairs applied at handover (see docs/DESIGN.md §7). Each is tied to evidence
from the two production traces or the reported figures; the prompt, the model,
the tool set, and the retrieval strategy are deliberately unchanged.

  - context is assembled once instead of once per step  (Trace B, $3.90)
  - run() returns the reply body                        (the eval graded "")
  - the decision object is validated before use         (uncaught KeyError)
  - action is checked before confidence                 (corrupt escalation telemetry)
  - only transient tool failures are retried            (blanket retry)
  - account status is enforced in code, not the prompt  (unenforced rule)
  - repeated tool calls end the run                     (Trace B)
  - a per-ticket cost ceiling exists                    (no ceiling; p95 $2.10)
  - answers from the wrong product version escalate     (Trace A)
  - clients are injected so tests cannot reach production
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from platform_sdk import (  # internal
    crm as _crm,
    kb as _kb,
    llm as _llm,
    provisioning as _provisioning,
    ticketing as _ticketing,
)

from telemetry import TicketTrace

log = logging.getLogger("sra")

MODEL = "frontier-model-v2"
MAX_STEPS = 8
CONFIDENCE_THRESHOLD = 0.7

# Stop after this many repeated tool calls. Trace B issued four near-identical
# searches across eight steps and never reached a decision; the Tier-2 engineer
# then resolved it in four minutes from the knowledge base. Repetition is not
# research, and continuing costs money without adding information.
MAX_DUPLICATE_TOOL_CALLS = 2

# Hard per-ticket ceiling. Today's mean is $0.34, p95 is $2.10, and the highest
# observed ticket was $4.80 — all without any ceiling at all. At the planned
# 31,000 tickets a month this cap bounds the worst case at ~$31k rather than the
# ~$65k implied by today's p95. A ticket that costs more than three times the
# mean is not researching, it is stuck.
HARD_COST_CEILING_USD = 1.00

# Enforced in code. The system prompt already asks for this (see the rules
# below), but a prompt is a request and this is a requirement.
NON_SERVICEABLE_STATUSES = frozenset({"suspended", "delinquent"})

# Retry transient failures only. TimeoutError and ConnectionError are subclasses
# of OSError. Deterministic failures — a bad argument, an unknown tool — cannot
# succeed on a second attempt, and retrying them burned three blocking sleeps
# for nothing. When the platform SDK grows a dedicated transient-error type, add
# it here rather than widening the except clause.
RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (OSError,)

VALID_ACTIONS = frozenset({"reply", "clarify", "escalate"})

SYSTEM_PROMPT = """You are a Tier-1 support agent for an ERP product.

Answer the customer's question using the product documentation and customer
context provided. Be accurate and concise.

Rules you must follow:
- Never make changes to a customer account. You are read-only.
- Never act on accounts with status = suspended or delinquent. Escalate instead.
- If you are not confident in your answer, escalate to a human.
- Do not speculate about product behaviour you cannot find in the documentation.

Respond in JSON:
{"action": "reply" | "clarify" | "escalate", "body": "...", "confidence": 0.0-1.0}
"""


@dataclass
class TicketContext:
    ticket_id: str
    subject: str
    body: str
    sender_email: str
    account_id: str
    history: list = field(default_factory=list)


@dataclass
class Deps:
    """
    Injected clients.

    Defaults are the real platform clients, so production behaviour is unchanged.
    Tests and the eval harness pass fakes. Before this existed, running the eval
    posted replies to real tickets and filled the real Tier-2 queue, which is the
    most reliable way to make people stop running an eval.
    """

    llm: Any = field(default_factory=lambda: _llm)
    kb: Any = field(default_factory=lambda: _kb)
    crm: Any = field(default_factory=lambda: _crm)
    ticketing: Any = field(default_factory=lambda: _ticketing)
    provisioning: Any = field(default_factory=lambda: _provisioning)
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class ContextMeta:
    """Provenance for the context we assembled, so failures are attributable."""

    customer: dict
    customer_product_version: str | None
    doc_versions: tuple[str, ...]
    oldest_doc_age_days: int | None

    @property
    def doc_version_mismatch(self) -> bool:
        if not self.customer_product_version or not self.doc_versions:
            return False
        return any(v != self.customer_product_version for v in self.doc_versions)


@dataclass(frozen=True)
class AgentDecision:
    action: str
    body: str
    confidence: float


def is_serviceable(customer: dict) -> bool:
    return str(customer.get("status", "")).strip().lower() not in NON_SERVICEABLE_STATUSES


def load_context(ctx: TicketContext, deps: Deps, customer: dict) -> tuple[str, ContextMeta]:
    """
    Assemble everything the agent might need for this ticket.

    Called once per ticket. It used to be called once per step, which re-appended
    the full ~180-chunk document set to the conversation on every iteration and
    made token use grow quadratically in steps. The comment justifying it said
    the model should always have "the latest state", but none of this state
    changes while a single ticket is being handled.

    Now also carries provenance. Trace A answered a v14 customer from v13
    documentation, confidently and incorrectly, and nothing in the assembled
    context would have let the model or a later reader notice.
    """
    product_docs = deps.kb.fetch_product_brain()
    recent = deps.ticketing.recent_tickets(ctx.account_id, limit=20)

    doc_versions = tuple(sorted({c["version"] for c in product_docs if c.get("version")}))
    ages = [c["age_days"] for c in product_docs if isinstance(c.get("age_days"), int)]

    meta = ContextMeta(
        customer=customer,
        customer_product_version=customer.get("product_version"),
        doc_versions=doc_versions,
        oldest_doc_age_days=max(ages) if ages else None,
    )

    provenance = "\n".join(
        [
            f"Customer product version: {meta.customer_product_version or 'unknown'}",
            f"Documentation versions in this set: {', '.join(doc_versions) or 'unknown'}",
            f"Oldest document here: {meta.oldest_doc_age_days if meta.oldest_doc_age_days is not None else 'unknown'} days old",
            "If the documentation version does not match the customer's version, "
            "say so rather than answering from the version you have.",
        ]
    )

    return (
        "\n\n".join(
            [
                "=== PROVENANCE ===",
                provenance,
                "=== PRODUCT DOCUMENTATION ===",
                "\n".join(c["text"] for c in product_docs),
                "=== CUSTOMER RECORD ===",
                json.dumps(customer, indent=2),
                "=== RECENT TICKETS ===",
                json.dumps(recent, indent=2),
            ]
        ),
        meta,
    )


def build_tools(deps: Deps) -> dict[str, Callable[..., Any]]:
    """
    Read-only tools available to the model.

    Writes never appear here. An action that changes a customer's environment
    goes through grant_executor, which logs before it acts and never retries.
    """
    return {
        "search_product_docs": deps.kb.search,
        "get_customer_detail": deps.crm.get_field,
        "get_ticket_history": deps.ticketing.history,
        "check_known_issues": deps.kb.known_issues,
    }


def execute_tool(
    tools: dict[str, Callable[..., Any]],
    name: str,
    args: dict,
    *,
    sleep: Callable[[float], None] = time.sleep,
    attempt: int = 0,
) -> Any:
    """Run a read tool. Retries transient failures only."""
    fn = tools.get(name)
    if fn is None:
        # Previously this was TOOLS[name], whose KeyError was then caught by the
        # retry handler and retried three times with blocking sleeps.
        log.error("unknown tool requested: %s", name)
        return {"error": f"unknown tool: {name}"}

    try:
        return fn(**args)
    except RETRYABLE_ERRORS as e:
        if attempt < 2:
            log.warning("tool %s failed transiently (%s), retrying", name, e)
            sleep(1.5 * (attempt + 1))
            return execute_tool(tools, name, args, sleep=sleep, attempt=attempt + 1)
        log.error("tool %s failed permanently: %s", name, e)
        return {"error": str(e)}
    except Exception as e:
        log.error("tool %s failed deterministically, not retrying: %s", name, e)
        return {"error": str(e)}


def parse_decision(text: str) -> AgentDecision | None:
    """
    Validate the model's decision before anything reads it.

    The previous code indexed decision["action"] directly, so a response missing
    that key raised an uncaught KeyError, killed the worker, produced no trace,
    and left the ticket sitting in the queue.
    """
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(raw, dict):
        return None

    action = raw.get("action")
    if action not in VALID_ACTIONS:
        return None

    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None

    return AgentDecision(action=action, body=str(raw.get("body", "")), confidence=float(confidence))


def _tool_result_payload(result: Any, limit: int = 4_000) -> str:
    """Truncate without producing invalid JSON, which slicing the dump could."""
    text = json.dumps(result)
    if len(text) <= limit:
        return text
    return json.dumps({"truncated": True, "preview": text[:limit]})


def run(ctx: TicketContext, *, deps: Deps | None = None) -> dict:
    deps = deps or Deps()
    trace = TicketTrace(ticket_id=ctx.ticket_id)
    tools = build_tools(deps)

    customer = deps.crm.fetch_customer_record(ctx.account_id)
    if not is_serviceable(customer):
        # Checked before any model call: a suspended account should not cost us
        # a ticket's worth of tokens to refuse.
        return escalate(ctx, deps, trace, reason="account_not_serviceable")

    context_block, meta = load_context(ctx, deps, customer)
    trace.record_context(
        customer_product_version=meta.customer_product_version,
        doc_versions=meta.doc_versions,
        oldest_doc_age_days=meta.oldest_doc_age_days,
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Ticket: {ctx.subject}\n\n{ctx.body}"},
        {"role": "user", "content": context_block},
    ]

    for step in range(MAX_STEPS):
        response = deps.llm.complete(
            model=MODEL,
            messages=messages,
            tools=list(tools),
            temperature=0.2,
        )

        trace.record_step(
            step=step,
            tool_calls=[(tc.name, tc.args) for tc in response.tool_calls],
            output=response.text or "",
            prompt_tokens=getattr(response, "prompt_tokens", 0),
            completion_tokens=getattr(response, "completion_tokens", 0),
        )

        if trace.cost.exceeds(HARD_COST_CEILING_USD):
            return escalate(ctx, deps, trace, reason="cost_ceiling_exceeded")

        if response.tool_calls:
            if trace.duplicate_tool_calls >= MAX_DUPLICATE_TOOL_CALLS:
                return escalate(ctx, deps, trace, reason="no_progress")

            for tc in response.tool_calls:
                result = execute_tool(tools, tc.name, tc.args, sleep=deps.sleep)
                messages.append(
                    {
                        "role": "tool",
                        "name": tc.name,
                        "content": _tool_result_payload(result),
                    }
                )
            continue

        decision = parse_decision(response.text)
        if decision is None:
            log.error("invalid decision on ticket %s", ctx.ticket_id)
            return escalate(ctx, deps, trace, reason="malformed_response")

        # Action before confidence. Reversing these recorded every deliberate
        # escalation made with low confidence as a confidence failure, which is
        # why the escalation-reason breakdown could not be trusted.
        if decision.action == "escalate":
            return escalate(ctx, deps, trace, reason="agent_requested")

        if decision.confidence < CONFIDENCE_THRESHOLD:
            return escalate(ctx, deps, trace, reason="low_confidence")

        if meta.doc_version_mismatch:
            # Trace A: a fluent, confident (0.88) answer describing the v13
            # approval engine, sent to a customer on v14 and auto-closed. If the
            # only documentation we hold describes a different release than the
            # customer runs, the answer is unverifiable, and confidence measures
            # how clear the document was rather than whether it applies.
            return escalate(ctx, deps, trace, reason="doc_version_mismatch")

        closing = decision.action == "reply"
        deps.ticketing.post_reply(ctx.ticket_id, decision.body, close=closing)
        outcome = "resolved" if closing else "clarify"
        trace.record_decision(outcome=outcome, confidence=decision.confidence)
        return finish(ctx, trace, outcome, body=decision.body)

    return escalate(ctx, deps, trace, reason="max_steps_exceeded")


def escalate(ctx: TicketContext, deps: Deps, trace: TicketTrace, reason: str) -> dict:
    deps.ticketing.assign_to_queue(ctx.ticket_id, "tier2")
    deps.ticketing.add_note(ctx.ticket_id, f"SRA escalated: {reason}")
    trace.record_decision(outcome="escalated", reason=reason)
    return finish(ctx, trace, f"escalated:{reason}")


def finish(ctx: TicketContext, trace: TicketTrace, outcome: str, body: str = "") -> dict:
    trace.outcome = outcome
    log.info(trace.to_json())
    return {
        "ticket_id": ctx.ticket_id,
        "outcome": outcome,
        # The eval harness reads this. Its absence is why the offline suite
        # graded an empty string on all 240 cases.
        "body": body,
        "cost_usd": round(trace.cost.usd, 6),
        "steps": len(trace.steps),
        "duplicate_tool_calls": trace.duplicate_tool_calls,
        "doc_version_mismatch": trace.doc_version_mismatch,
    }


# The product brain refresh job still fails silently upstream of this service.
# The runtime now refuses to auto-close an answer drawn from documentation that
# does not match the customer's version, and emits doc_version_mismatch on every
# ticket, which turns a silent failure into a number someone can be paged about.
# Fixing the job itself is not in this file.
