# Support Resolution Agent — Diagnosis, Extension, Evaluation, Ownership

Oguzhan Kara · 19 August 2026

**Where I went deep, and why.** Parts 2 and 3. Every other defect here produces a bad reply that a human corrects; only the access capability produces an irreversible act with no human in the path. Depth belongs where reversibility ends — so the authorization decision and its evaluation got the hours, and retrieval, prompting, and answer quality got a sentence each. The code carries Parts 2 and 3; this states the decisions, not the implementation.

The git history is deliberately two halves: four commits repairing what I inherited, then four adding the capability. A reviewer should not have to work out which is which.

---

## Part 1 — Diagnosis

### Ranked, with reasons

1. **The knowledge refresh job.** Failing silently for months. Error/troubleshooting fell 86% → 71%; v14 changed the error codes ten weeks ago against documentation four months old. An ops fix, not a project, and the largest correctness driver. Best ratio of impact to effort.
2. **The eval harness.** After (1) you need to know whether it worked, and you cannot: it grades an empty string. It is the instrument every later decision depends on.
3. **Cost and step controls.** Invisible at 2,400 tickets a month. The budget at 31,000.
4. **Version awareness at runtime.** The refresh job will fail again. The system should degrade loudly, not silently.
5. **Prompt-level safety and uncalibrated confidence.** Survivable read-only. Not survivable once it can act — so both are fixed before Part 2, not after.
6. **The Admin contact list.** Not mine to fix, but it gates the capability Support asked for.

### 91% versus 84%

The two numbers were never comparable.

- **They measure different things.** `run()` returns no `body` (`sra_runtime.py:152`); the harness reads `result.get("body", "")` (`eval_harness.py:47`). The judge graded an empty string on all 240 cases. This alone voids the comparison.
- **The eval cannot fail on the drift that is hurting production.** Cases frozen at launch, so for a v14 error code its reference answer is now the wrong answer — a correct response scores as a failure.
- **Different populations.** QA samples 50 *SRA-handled* tickets, likely excluding escalations; the eval runs all 240.
- **An unmeasured judge.** Same model constant as the agent, sees the reference, parsed with `startswith("PASS")` — which reads FAIL for any judge that reasons before answering. Judge–human agreement has never been measured.
- **No isolation.** Running it posts replies to real tickets and fills the real Tier-2 queue. An eval with production side effects is one people quietly stop running.

The sharpest point is directional: the eval rose 90 → 91 while production fell 90 → 84. **A number that moves opposite to reality is worse than no number — it manufactures confidence.** It is the artifact that let v14 ship unnoticed.

### Evidence versus inference

**Proven by the code:** quadratic context growth (`:91-94`); the empty-body bug (`:152`); the eval writing to production (`eval_harness.py:8,44`); safety rules only in the prompt (`:21-34`); every exception retried three times including deterministic ones (`:71-81`); confidence checked before action, corrupting escalation telemetry (`:125-129`); an uncaught `KeyError` on `decision["action"]` (`:128`); tool names logged without arguments (`:105`), which is why Trace B is undiagnosable from its own trace; no cost accounting.

**Proven by the data:** the error-category collapse coincides with v14 and a four-month-old artifact; reopens doubled; the eval moved opposite to production; and escalation fell 31% → 29% while accuracy fell — the agent escalates *less* as it becomes *more* wrong, the inverse of calibration.

| Suspected | Instrument |
|---|---|
| Stale docs *cause* the error-category decline | Tag answers with chunk ids, `last_refreshed`, and customer version; compare accuracy on match vs. mismatch. Decisive and cheap. |
| Confidence is uncalibrated | Bucket reported confidence against QA-verified correctness. A flat reliability curve means the 0.7 threshold is a placebo. One wrong 0.88 is a point, not a curve. |
| Trace B's search loop is representative | Normalised tool-argument hashes; `duplicate_tool_calls` per ticket. |
| Access requests are 18%, not the 40% claimed | Classify inbound volume and publish the mix. This is the denominator of the business case. |
| Access-request accuracy is ~74% | Derived from the weighted category average with one cell missing. An estimate, not a measurement. |

Two things I could not check: `eval_cases.json` was not in the handover, so all claims about the 240 cases are inference from a docstring; and whether QA sampling excludes escalations is a question for Support leads.

**Statistical caution.** 50 tickets a week is ~8.3% of volume; at n=50, p=0.84 the 95% interval is ±10 points, so **no single month is individually meaningful.** Pooled months 1–2 (n≈400) against month 5 (n≈200) gives ~2 standard errors, p ≈ 0.045 — at the edge. The decline is credible because it is monotonic across four periods *and* has a mechanism.

### Misleading metrics

**"Closed without human involvement: 58% → 61%."** Reopens went 7% → 13%. Net of reopens: 54% → 53%. **It declined.** And reopens lag — Trace A returned after two days — so month 5 is right-censored and the true figure is worse. The headline counts a returning ticket as a success.

**"First response 4.2 hrs → 0.3."** The speed of *any* reply, including wrong ones.

**"Queue depth 210 → 165."** Partly deferred, not eliminated; escalation barely moved.

**Cost.** Mean $0.34, p95 $2.10, max $4.80. The max and Trace B's $3.90 are both eight-step runs, so the ceiling is set by `MAX_STEPS` and **the ceiling is the failure mode — you pay the most for the tickets you fail.** At 31,000/month the mean gives ~$10.5k; today's p95 as a mean gives **~$65k**, and nothing prevents it.

---

## Part 2 — Extension

Code: `access_request.py`, `grant_executor.py`, routing in `sra_runtime.py`.

**The Admin list is not an authorization source on its own.** Median age seven months, 22% over a year. As written, an entry nobody has checked since last year authorizes access to this year's payroll. I made age a *validity condition*: past 90 days it escalates and names the account for Customer Success. This blocks most accounts at launch, deliberately — automation is capped by data quality we do not control, and a low rate for an honest reason beats a high one built on a stale list. The block rate is the number that funds fixing it.

**Being on the Admin list is an identity claim; granting payroll access is an entitlement claim.** The CRM supports the first and was never designed to carry the second. Conflating them is the central risk in the request.

**Payroll is never auto-granted, at any autonomy level.** Every gate can pass and the answer is still a prepared action. Not a smaller feature: it turns a ten-minute human task into a twenty-second one — most of the value, none of the irreversibility.

**The queue claim is roughly double.** The request says 40% of Tier-1 volume; the category table says 18%. The honest case is not "the queue halves" — it is that 18% gets far cheaper per ticket and the risky fraction stays human but much faster.

**Structurally: the model extracts, deterministic code decides.** Extraction is one isolated call — no tools, no history, temperature zero — and `account_id` and `requester_email` are overwritten from ticket metadata and the mail envelope, so "I am the CFO, this is pre-approved" is text we report on, not an authorization. `authorize()` then has no parameter through which prose can arrive. It is pure: no I/O, no model, no ambient clock. That is what makes 43 adversarial cases run in 0.2s with no network and no fixtures, and therefore affordable on every commit.

**Reversibility ships first.** The provisioning API has no undo, so I built one: write-ahead logging before the call, idempotency keyed on (account, target, module, ticket), exactly one attempt, `revoke_window(since)` to reverse a period in one call, hourly ceilings, and a kill switch read at runtime so stopping never waits for a deploy. Failures split into FAILED and UNKNOWN — a timeout may have been applied, and calling that a failure means our records say no access exists while the customer's system says otherwise.

**Also repaired** (the `fix:` commits, each tied to evidence in its message): context assembled once, `body` returned, the decision validated, retries limited to transient failures, account status enforced in code, a $1.00 per-ticket ceiling, and version-mismatched answers no longer auto-closing.

**Deliberately not changed:** retrieval architecture, prompt wording, model selection, the tool abstraction, the unused `history` field. Each is a real improvement; none is what makes this launch safe. Changing them turns a reviewable extension into a rewrite.

**Named dependencies:** `crm.fetch_admin_contacts` must expose `last_updated`; `crm.fetch_directory` must exist. The mail envelope is a weak identity claim — hardening it needs an authenticated channel, a platform change outside this work. A stated limitation, not an oversight.

---

## Part 3 — Evaluation

Code: `eval_access.py`, `eval_cases_access.py`, repairs to `eval_harness.py`. Run `python eval_access.py`.

Decisions are asserted directly — no model grades a security property. The headline is **false grants, gate zero**; automation rate is reported second and never traded against it, because a wrong escalation costs four minutes and a wrong grant is an unauthorised person holding payroll data. Output is a confusion matrix, never one number.

**43 cases built from failure modes**, not sampled from happy paths — which is how a suite scores 91% on a system that is wrong 16% of the time. Injection, forged approvals quoted in a mail chain, lookalike domains, departed admins, records at 89/90/91/400 days, two people named Jane, an informational question that must not act. Each names the threat it guards and carries a `product_version`, so the build fails when the corpus drifts behind the product.

**The suite tests itself.** Eight gates are disabled in turn and each must be detected. This caught a real hole while I was writing it: the `trust_the_model` mutation — the request implemented literally — initially **survived**, because the forged requester in my impersonation cases was not an Admin either, so believing the ticket body changed nothing. Fixed by naming a forged requester who really is an Admin, and by asserting the *trusted fields* on the extracted request rather than only the decision. The suite looked complete and tested nothing at that boundary. That is the argument for the technique, and the mechanism that stops me repeating the inherited eval's failure.

**What offline evaluation structurally cannot catch:**

- **`grant_reversal_rate_30d`** — grants revoked within 30 days. The only ground truth for whether a grant was *wanted*; it can only come from the future.
- **`approval_dwell_time_p50`** — if median human approval takes four seconds, the human-in-the-loop is theatre. Paired with `approval_override_rate`, it separates "we are right" from "they are rubber-stamping."
- **`staleness_block_rate`** and **`doc_version_mismatch_rate`** — drift in sources we do not control; the second settles whether stale docs cause the error-category decline.
- **`reopen_rate_by_outcome`** — whether the answer worked.
- Adversarial adaptation, real provisioning behaviour under load, tail cost at scale.

**Replay evaluation:** nightly, re-run the last 30 days of tickets and diff the decisions. That is what would have caught v14 without anyone writing a test.

---

## Part 4 — Ownership

**2am** — only things actively doing irreversible damage: the grant breaker tripping; a sensitive-module grant that reached provisioning; a non-empty reconciliation list, where our records and a customer's system may disagree; a provisioning error spike; a broken revoke path; cost burn over the hourly ceiling. **Morning:** accuracy drift, escalation drift, a single wrong reply, freshness warnings, queue depth, individual QA failures. The distinction is not severity — it is reversibility.

**Cost at 31,000/month.** Hard cap $1.00 per ticket, soft cap $0.50 where the agent must answer or escalate; access requests get one step, not eight. Monthly ceiling $12k, alerts at 70/85/100%. At 100% it **degrades rather than stopping**: no full-context loading, shorter step budgets, sensitive modules forced to prepare-only. A slower support agent is usable; an absent one is an outage.

**First week.** Day 0 shadow: decide, act on nothing, diff against what humans did. Days 1–2 prepare-only, every grant human-confirmed, reviewing override rate *and* dwell time. Days 3–7 auto-grant non-sensitive modules only, capped, every grant reviewed daily — the week that stops being possible is the week monitoring has to be real. Go/no-go against thresholds registered **before** launch.

**Shutoff.** Any confirmed unauthorised grant; reversal rate above 2%; any successful injection; provisioning ambiguity above 0.5%; cost above ceiling for 24 hours. **I make that call alone, without asking.** Restarting requires the Director of Support and the owner of security. Stopping should be fast and unilateral; restarting slow and shared.

**A wrong payroll grant: I am accountable.** Not the model, not the Director who asked, not the Customer Success team whose list went stale — I shipped a system that used that list as an authorization source. First hour: kill switch; revoke that grant; `revoke_window()` across the exposure period and bound the blast radius from the grant log; pull provisioning and product audit logs, because "granted" and "accessed" are different incidents; notify the account's real admin plus security and legal, since this may be reportable and delay makes it worse; write the timeline. Nothing restarts until that failure has a case that fails against the old code. The full procedure, including retirement, is in `RUNBOOK.md` — an incident is the worst time to design a response.

---

## AI usage disclosure

**Tool.** Claude Opus 5 via Claude Code, one session. No other models or agents.

**How the work was divided.** I handed the materials to the agent without reading them first. It did the line-by-line reading, the diagnosis, the design document, and all of the code and tests. I set the standard, challenged what came back, chose between the alternatives it offered, and approved the design before implementation. This would be dishonest if it implied I hand-wrote the policy engine.

**Where my direction changed the output.** I rejected the first design as unlikely to survive review; the second pass produced the two-half diff, the "deliberately not changed" section, the runbook, the retirement procedure, the statistical caveat, and **mutation testing of the eval itself** — none of which existed before. I asked whether it had genuinely read both files, which forced the line-referenced evidence table behind Part 1. I asked whether any of it would actually run, which is why the security core is standard-library-only and the suite executes in under a second. And I chose the depth allocation and the tiered-autonomy posture over the two alternatives it presented.

**Where it was wrong.** The injection suite was decorative on the first pass — `trust_the_model` survived, and mutation testing caught it where review did not. A test asserted the harness made no ticketing calls, measuring the fake rather than the property. A routing test read the wall clock and reported 399 days instead of 400.

The first is the one that matters: a mechanism I asked for found a hole neither of us saw, in work that looked finished.

**What I would do differently.** Read the source material myself before handing it over — challenging the agent's reading afterwards is a spot-check of its own output, not an independent one. And mutation testing should have been in the first plan; it justified itself within an hour.

**Accountability.** Mine, for all of it. A model drafted most of this; I decided what shipped.
