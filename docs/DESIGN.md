# SRA Access-Request Capability — Design

Author: Oguzhan Kara
Date: 2026-08-19
Status: approved for implementation

---

## 1. Scope and framing

SRA is a read-only Tier-1 support agent. The Director of Support has asked for it to grant module access inside customer environments. This is the first action SRA will take that changes state in a system we do not own and cannot roll back with a single call.

That single fact drives every decision below. A wrong answer is embarrassing and recoverable. A wrong grant is a data-access event on someone else's payroll data. These are not the same class of failure and must not share a safety mechanism.

**What I went deep on:** the authorization decision (Part 2) and the evaluation that proves it works (Part 3).

**What I handled lightly:** general answer quality, retrieval architecture, prompt engineering.

**Why:** every other defect in this system produces a bad reply, which a human notices and corrects. Only this one produces an irreversible act with no human in the path. Depth belongs where reversibility ends.

---

## 2. Evidence base

Separating what the artifacts prove from what I infer. This distinction governs how strongly each claim is stated in the writeup.

### Proven by the code

| Claim | Location |
|---|---|
| Context is rebuilt and re-appended every step; token growth is quadratic in steps | `sra_runtime.py:91-94` |
| `run()` never returns `body`; the eval harness grades the empty string | `sra_runtime.py:152` + `eval_harness.py:47` |
| Running the eval posts replies to real tickets and fills the real Tier-2 queue | `eval_harness.py:8,44` via `sra_runtime.py:132,135,143` |
| Safety rules exist only as prompt text, with no enforcing code | `sra_runtime.py:21-34` |
| Every exception is retried three times, including deterministic ones | `sra_runtime.py:71-81` |
| Confidence is checked before action, corrupting escalation-reason telemetry | `sra_runtime.py:125-129` |
| `decision["action"]` can raise an uncaught `KeyError` and kill the worker | `sra_runtime.py:128` |
| The trace records tool names but not arguments | `sra_runtime.py:105` |
| No cost or token accounting anywhere | `sra_runtime.py:85,155` |
| Retrieval carries no product version and no document age | `sra_runtime.py:47-60` |

### Proven by the reported data

- Error/troubleshooting accuracy fell 86% to 71%, coinciding with the v14 release (10 weeks ago) and a knowledge artifact last refreshed 4 months ago.
- Ticket reopen rate rose 7% to 13%.
- The offline eval rose 90 to 91 while production QA fell 90 to 84. The eval moves opposite to reality.
- Escalation rate fell 31% to 29% while accuracy fell. The agent escalates *less* as it becomes *more* wrong, which is the inverse of calibrated behaviour.

### Inferred, not proven — and how I would settle it

| Inference | Instrument |
|---|---|
| Stale docs *cause* the error-category collapse | Tag each answer with the KB chunk ids, their `last_refreshed`, and the customer's product version. Compare accuracy where version matches vs. mismatches. Decisive and cheap. |
| Self-reported confidence is uncalibrated | Bucket reported confidence, plot against QA-verified correctness. A flat reliability curve means `CONFIDENCE_THRESHOLD` is a placebo. One wrong 0.88 (Trace A) is a data point, not a curve. |
| Trace B's redundant search loop is representative | Log normalised tool-argument hashes; emit `duplicate_tool_call_count` per ticket. |
| Access requests are ~18% of volume, not the 40% claimed | Classify inbound tickets and publish the actual mix. This is the denominator of the entire business case. |
| Access-request accuracy is currently ~74% | Derived by solving the weighted category average against the 84% total, with one cell missing from the brief. Treated as an estimate, not a measurement. |
| Reopens are concentrated in auto-closed tickets | Segment reopen rate by outcome and category. |
| The weekly QA sample excludes escalated tickets | Ask Support leads. This is a question, not an instrument. |

### Not available

`eval_cases.json` was not provided. Every statement about the composition of the 240 cases is inference from the harness docstring, not inspection of the corpus.

### Statistical caution

The weekly QA sample is 50 tickets, roughly 8.3% of volume. At n=50 and p=0.84 the 95% confidence interval is about ±10 points, so no single month's figure is individually meaningful. Comparing pooled months 1-2 (n≈400, 90%) against month 5 (n≈200, 84%) gives roughly two standard errors, p ≈ 0.045 — right at the edge of significance. The decline is credible because it is monotonic across four periods *and* has a mechanism, not because any one number clears a threshold.

---

## 3. Design invariants

These are the properties the implementation must hold. Each is testable.

1. **No attacker-controlled text is in scope when the authorization decision is made.** `authorize()` receives validated structured fields. It never receives the ticket body, subject, or any LLM free text.
2. **The LLM proposes; deterministic code disposes.** The model's only job is extraction — turning prose into a typed request. It has no authority.
3. **Fail closed.** Unknown module, unknown user, unparseable request, missing data, engaged kill switch: all escalate. Nothing defaults to granting.
4. **No irreversible action without a built reversal path.** The provisioning API has no undo, so we build one before we make the first call.
5. **Write actions are never automatically retried.** Read tools may retry; a grant is attempted exactly once and its outcome is recorded before and after.
6. **Autonomy is configuration, not code.** Rollout and shutoff are config changes readable at runtime, so neither requires a deploy.

---

## 4. Architecture

```
ticket
  |
  v
[classify]  --- not an access request ---> existing SRA path (unchanged)
  |
  v
[extract]   isolated LLM call. Input: ticket text only. Output: typed AccessRequest.
  |         Untrusted text stops here.
  v
[resolve]   validate against CRM: account status, target user, module enum,
  |         requester identity, admin record + its age
  v
[authorize] PURE FUNCTION. No I/O, no LLM, no ambient clock. Deterministic.
  |         Time and runtime state arrive as parameters, never as lookups.
  |         -> Decision + ReasonCode + evidence
  v
[execute]   only if Decision.GRANT. Write-ahead log, idempotency key,
            single attempt, reversal path registered.
```

`account_id` comes from ticket metadata, never from the body. `requester` comes from the mail envelope, never from a name claimed in the body. `module` resolves against a closed enum; free text never reaches the provisioning API.

### Components

| File | Responsibility | Runnable |
|---|---|---|
| `access_request.py` | Types, module registry, policy, and the pure `authorize()` | yes, stdlib only |
| `grant_executor.py` | Write-ahead log, idempotency, single-attempt execution, revoke, breaker | yes, with injected client |
| `telemetry.py` | Trace schema: cost, tokens, tool arguments, document versions, reason codes | yes, stdlib only |
| `eval_access.py` | Decision-level harness, fakes, effect assertions, mutation tests | yes |
| `eval_cases_access.py` | The adversarial corpus | yes |
| `sra_runtime.py` | Patched: repairs plus routing to the new path | no — needs `platform_sdk` |
| `eval_harness.py` | Patched: empty-body bug, test seam, category breakdown | no — same |

`access_request.py` imports nothing outside the standard library. This is not a convenience; it is the reason the design is defensible. The authorization decision is testable in complete isolation, with no network, no model, and no fixtures.

---

## 5. The authorization decision

### Outcomes

```
GRANT                 execute now
PREPARE_FOR_APPROVAL  produce a filled-in action a human confirms in one click
CLARIFY               ask the customer for a missing field
ESCALATE              hand to a human with the reasoning already done
```

There is deliberately no `DENY`. A refusal is a negative decision communicated to a customer, and it belongs to a human. Escalation carries the completed analysis, so a human decides in seconds rather than minutes.

### Evaluation order

Checks are evaluated first-match-wins in a fixed, documented order so that the emitted reason code is deterministic and aggregatable. Ordering runs from account-level blockers, through identity, to module sensitivity.

### Reason codes

`KILL_SWITCH_ENGAGED`, `ACCOUNT_SUSPENDED`, `ACCOUNT_DELINQUENT`, `ADMIN_RECORD_STALE`, `REQUESTER_NOT_ADMIN`, `REQUESTER_DOMAIN_MISMATCH`, `TARGET_USER_UNKNOWN`, `TARGET_USER_INACTIVE`, `TARGET_USER_AMBIGUOUS`, `MODULE_UNKNOWN`, `BULK_REQUEST_UNSUPPORTED`, `REVOCATION_NOT_SUPPORTED`, `ALREADY_HAS_ACCESS`, `RATE_LIMIT_EXCEEDED`, `MODULE_SENSITIVE_REQUIRES_APPROVAL`, `AUTHORIZED`.

Reason codes are the unit of production monitoring. Aggregating them tells us why the capability is or is not automating, which a single accuracy number cannot.

### Keeping `authorize()` pure while it decides on runtime state

Two of those reason codes — `KILL_SWITCH_ENGAGED` and `RATE_LIMIT_EXCEEDED` — describe runtime conditions, and a pure function cannot look them up. The caller resolves them first and passes a `RuntimeGuards` snapshot in alongside `now`.

This keeps the decision total and deterministic: everything `authorize()` needs is an argument, so the same inputs always produce the same decision, and the suite can construct any condition — an engaged kill switch, an exhausted rate limit, an admin record 400 days old — without a clock, a network, or a fixture. Purity here is not stylistic. It is what makes an exhaustive adversarial suite cheap enough to actually run on every commit.

### Module sensitivity

| Class | Examples | Launch behaviour |
|---|---|---|
| `RESTRICTED` | payroll, finance, hr_records, audit_log | never auto-granted; `PREPARE_FOR_APPROVAL` at best |
| `STANDARD` | inventory, projects, reporting, crm | auto-grant when all gates pass |
| `UNKNOWN` | anything absent from the registry | escalate — never inferred |

### Admin-record freshness gate

An admin contact record older than `ADMIN_RECORD_MAX_AGE_DAYS` (90) is not a valid authorization source.

The median record is about 210 days old, so this gate will block most accounts at launch. That is intended and I accept the cost. The list is currently treated as authoritative while nobody knows how stale any given entry is; the gate converts an invisible data-quality problem into a visible queue of specific accounts for Customer Success to re-verify. The metric `staleness_block_rate` is the measurement of how bad the problem actually is, and it is the number that justifies fixing it.

The trade-off is explicit: automation rate at launch is capped by data quality we do not control. I would rather report a low automation rate for an honest reason than a high one built on a seven-month-old list.

### A distinction the request conflates

Being listed as an Admin contact is an **identity** claim. Being entitled to grant another person access to payroll data is an **entitlement** claim. The CRM supports the first and was never designed to carry the second. Treating one as the other is the central risk in the request as written.

---

## 6. Reversibility and containment

- **Write-ahead log.** The intent is recorded before the API call. If the call succeeds but the response is lost, we still know what we did. That state is `UNKNOWN` and it pages, because it is the only state where our records and the customer's reality may disagree.
- **Idempotency key.** `sha256(account_id | target_email | module | ticket_id)`. A replayed ticket cannot double-grant.
- **`revoke_window(since)`.** Reverse every grant SRA made in a time window with one call. This is what an incident actually needs; revoking one at a time is not a plan.
- **Rate limits and a circuit breaker.** Per-account and global ceilings. Breaching the global ceiling disables the capability automatically and pages.
- **Kill switch.** Read at runtime, not deploy time. Checked before every grant.

---

## 7. Repairs to the existing runtime (patch 1)

Delivered as a separate commit from the new capability so a reviewer can see exactly what is repair and what is extension. Each change is tied to specific evidence.

| Change | Evidence |
|---|---|
| Move `load_context()` out of the step loop | `L91-94`; Trace B cost $3.90 |
| Carry product version and document age into context; refuse high confidence on stale docs | Trace A; v14 shipped 10 weeks ago, docs 4 months old |
| Return `body` from `finish()` | `L152` makes the eval grade `""` |
| Split retryable reads from non-retryable writes | `L71-81` blanket retry |
| Check action before confidence | `L125-129` telemetry corruption |
| Validate the decision object; never index a raw key | `L128` uncaught `KeyError` |
| Log normalised tool arguments and per-ticket cost | `L85,105`; Trace B is undiagnosable from its own trace |
| Detect duplicate tool calls and stop | Trace B: four near-identical searches |
| Enforce account status in code, not prompt | `L28` is unenforced text |
| Inject clients so the eval cannot touch production | `eval_harness.py:44` |

### Deliberately not changed

Retrieval architecture, the system prompt's wording, model selection, the tool abstraction layer, and the `history` field that is defined but unused. Each is a real improvement and none of them is what makes this launch safe. Changing them would convert a reviewable extension into a rewrite, and would mix speculative gains into a diff whose purpose is to be verifiable.

---

## 8. Evaluation

### What is wrong with the inherited harness

It grades an empty string. It writes to production. It reports one aggregate number with no category, cost, or latency breakdown. Its cases are frozen at launch, so for a v14 error code its reference answer is now the wrong answer, and it is structurally incapable of detecting the drift that is actually hurting production. Its judge shares a model family with the agent, sees the reference answer, and is parsed with `startswith("PASS")`. It is run manually before deploys, and running it has a production side effect — which is the most common reason an eval stops being run.

### What replaces it for this capability

**Decision-level, not text-level.** The output is a typed decision. It is asserted directly. No judge is involved in grading a security property.

**Asymmetric metric.** Accuracy is the wrong headline because the costs are not symmetric. The gate is **zero false grants on the adversarial set**; automation rate is optimised only under that constraint. Results are reported as a confusion matrix over the four outcomes, never as one number.

**Effect assertions.** A recording fake provisioning client captures every call. Tests assert on what would actually have happened, not on what the agent said.

**Adversarial corpus (~35 cases)** built from failure modes rather than sampled happy paths: prompt injection in the ticket body; forged approval quoted in an email chain; sender domain mismatch; requester absent from the admin list; requester listed but since departed; admin record 91 days and 400 days old; ambiguous target ("Jane", two matches, no email); module name collisions ("Payroll" vs "Payroll Reports"); suspended and delinquent accounts; bulk requests; a revocation phrased as a grant; an informational question that must not trigger an action ("how do I give Jane access?"); duplicate ticket idempotency; provisioning timeout and partial failure.

**Mutation testing of the suite itself.** Each gate is programmatically disabled and the suite must fail. A suite that still passes with the freshness gate removed is decorative. This exists specifically because the inherited eval failed in exactly that way — and it is the only mechanism that structurally prevents me from repeating it.

**Case metadata and a staleness gate.** Every case carries `product_version` and `created_at`. CI fails when too large a share of the corpus predates the current major version. The frozen-eval failure is prevented by construction rather than by discipline.

**Cost and steps are first-class outputs**, with regression gates on p95.

### What offline evaluation structurally cannot catch

1. **`grant_reversal_rate_30d`** — the share of SRA grants revoked within 30 days. This is the only ground truth for whether a grant was actually wanted, and it can only come from the future.
2. **`approval_dwell_time_p50`** — if the median human approval takes four seconds, the human-in-the-loop is theatre. Pairs with `approval_override_rate`: if overrides are near zero, dwell time disambiguates "we are right" from "they are rubber-stamping".
3. **`staleness_block_rate`** — drift in a data source we do not control.
4. **`doc_version_mismatch_rate`** — answers citing documentation whose version differs from the customer's.
5. **`reopen_rate_by_outcome`** — whether the answer actually worked.
6. **Adversarial adaptation.** Real attackers change; a frozen corpus does not.
7. **Real provisioning behaviour** — latency, partial failure, rate limits, concurrency.
8. **Cost distribution at scale**, including tail behaviour a fixture cannot produce.

### Replay evaluation

Nightly, re-run the last 30 days of production tickets against the current build and diff the decisions. This is what would have caught v14 without anyone writing a new test case.

---

## 9. Cost as a design constraint

| Scenario | Monthly |
|---|---|
| Today, 2,400 tickets at $0.34 mean | ~$816 — nobody cares |
| 31,000 tickets, mean holds | ~$10.5k |
| 31,000 tickets at today's p95 of $2.10 | **~$65k** |

The mean will not hold on its own. Three portfolio companies means a larger combined corpus, and because `load_context()` runs inside the step loop, cost scales with corpus size on every step. Nothing in the system prevents the third row.

Note the shape of the distribution: observed maximum $4.80 and Trace B at $3.90 are both eight-step runs. The cost ceiling is set by `MAX_STEPS`, and the ceiling is the failure mode. **The most expensive tickets are the ones the system fails to resolve.**

Controls: a hard per-ticket ceiling, a soft ceiling at which the agent must stop researching and either answer or escalate, a smaller step budget for access requests (classification, not research), and budget alerting that triggers automatic degradation rather than automatic shutdown.

---

## 10. Rollout

| Stage | Autonomy | Gate to advance |
|---|---|---|
| Day 0 | `OFF` — shadow; decide, act on nothing | decisions diffed against human outcomes |
| Days 1-2 | `PREPARE_ONLY` — every grant human-confirmed | override rate and dwell time reviewed |
| Days 3-7 | `STANDARD_AUTO` — non-sensitive modules only, daily cap | every grant reviewed individually |
| Beyond | `RESTRICTED` remains manual | requires explicit sign-off, never implicit |

Thresholds are registered before launch, not chosen afterward from whatever the data turned out to be.

---

## 11. Platform dependencies and assumptions

Named explicitly rather than assumed silently.

- `crm.fetch_admin_contacts(account_id)` must expose a `last_updated` timestamp. The brief cites a median age across accounts, so this value exists somewhere; surfacing it through the SDK may be work for another team.
- `provisioning.revoke_module_access(...)` is stated to exist as a separate call.
- The requester identity comes from the mail envelope, which is a weak identity claim. Hardening it requires an authenticated channel and is a platform change outside this work. It is a known, stated limitation, not an oversight.
- Module identifiers must be enumerable. If provisioning accepts free-text module names, the closed enum lives here and unknown values escalate.

---

## 12. Accountability

If SRA grants the wrong person access to payroll data, I am accountable. Not the model, not the Director who asked for it, not the Customer Success team whose list went stale. I shipped a system that used that list as an authorization source.

The kill switch is mine and I use it without asking. Re-enabling requires the Director of Support and the owner of security or compliance. The asymmetry is deliberate: stopping should be fast and unilateral, restarting should be slow and shared.

The first-hour procedure is written before launch, in `RUNBOOK.md`, because an incident is the worst possible time to design a response.
