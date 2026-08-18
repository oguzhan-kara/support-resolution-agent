# Support Resolution Agent — Diagnosis, Extension, Evaluation, Ownership

Oguzhan Kara · 19 August 2026

**Where I went deep and why.** Parts 2 and 3. Every other defect in this system produces a bad reply, which a human reads and corrects. Only the access-request capability produces an irreversible act with no human in the path. Depth belongs where reversibility ends, so the authorization decision and the evaluation that proves it works got the hours, and general answer quality, retrieval architecture, and prompt engineering got a paragraph each. Part 1 is compressed to what the data supports. Part 4 is short because it is a set of commitments, not an essay.

The code is a git history in two halves: four `fix:` commits repairing what I inherited, then four `feat:` commits adding the capability. That split is deliberate — a reviewer can see exactly what is repair and what is extension.

---

## Part 1 — Diagnosis

### What I would address first, and why in that order

**1. The knowledge refresh job.** It has been failing silently for months. Error/troubleshooting accuracy fell 86% → 71%, and v14 — which changed the permissions model, the approval workflow engine, and several error codes — shipped ten weeks ago against documentation last refreshed four months ago. This is an operations fix, not an engineering project, and it is the largest single correctness driver in the system. It goes first because nothing else has this ratio of impact to effort.

**2. The evaluation harness.** After fixing (1) you need to know whether it worked, and today you cannot. `run()` never returned a `body`, and the harness read `result.get("body", "")` — so the judge graded an empty string on all 240 cases. Whatever 91% measured, it was not resolution correctness. Second, because it is the instrument every later decision depends on.

**3. Cost and step controls.** `load_context()` ran inside the step loop, re-appending the full ~180-chunk document set on every iteration. Token use grew quadratically in steps. There is no cost ceiling and no per-ticket tracking. At 2,400 tickets a month this is invisible; at 31,000 it is the budget.

**4. Version awareness at runtime.** The refresh job will fail again. The system should degrade loudly rather than silently: an answer drawn from documentation that does not match the customer's release should not auto-close.

**5. Prompt-level safety and uncalibrated confidence.** "Never act on suspended or delinquent accounts" existed only as prompt text with nothing enforcing it. Self-reported confidence gates auto-closure at 0.7, and Trace A was 0.88 and wrong. Both are survivable while the agent is read-only. Neither is once it can act — which is why they are fixed before Part 2 and not after.

**6. The Admin contact list.** Median age seven months, 22% over a year old. Not a defect I own, but it gates the capability Support asked for.

### Accounting for 91% versus 84%

The gap is not one gap, and the two numbers were never comparable.

**They measure different things.** The harness grades the empty string (`sra_runtime.py:152` + `eval_harness.py:47`). This alone invalidates the comparison.

**The eval cannot fail on the drift that is hurting production.** Its 240 cases were built at launch and never updated. For a v14 error code, its reference answer is now the wrong answer — so a correct v14 response would score as a failure and a v13 response would pass.

**Its population differs.** Weekly QA samples 50 *SRA-handled* tickets, which likely excludes escalations. The eval runs all 240 including cases the agent would escalate. Different denominators.

**Its judge is unmeasured.** Single judge, same model constant as the agent (`:10` and `:17`), sees the reference answer, and is parsed with `startswith("PASS")` — which scores FAIL for any judge that states a reason before its verdict. Nobody has measured judge–human agreement.

**And it has no isolation.** Running the eval calls the real `run()`, which posts replies to real tickets and fills the real Tier-2 queue. An eval with production side effects is an eval people quietly stop running, which fits "run manually before deploys."

The sharpest point is directional. The eval rose 90 → 91 while production fell 90 → 84. **A number that moves opposite to reality is worse than no number, because it manufactures confidence.** It is the artifact that let v14 ship unnoticed.

### What the evidence supports, and what it does not

**Proven by the code**, with locations: quadratic context growth (`:91-94`); the empty-body bug (`:152`); the eval writing to production (`eval_harness.py:8,44`); safety rules only in the prompt (`:21-34`); every exception retried three times including deterministic ones (`:71-81`); confidence checked before action, corrupting escalation-reason telemetry (`:125-129`); an uncaught `KeyError` on `decision["action"]` that kills the worker (`:128`); tool *names* logged without arguments (`:105`), which is why Trace B is undiagnosable from its own trace; no cost accounting anywhere.

**Proven by the reported data**: the error-category collapse coincides with v14 and a four-month-old knowledge artifact; reopens doubled 7% → 13%; the eval moved opposite to production; and escalation fell 31% → 29% while accuracy fell — the agent escalates *less* as it becomes *more* wrong, which is the inverse of calibrated behaviour.

**What I suspect, and what I would instrument.** That stale documentation *causes* the error-category decline is a correlation and one trace, not a proof. **Instrument:** tag every answer with the knowledge chunks used, their `last_refreshed`, and the customer's product version; compare accuracy where versions match against where they do not. Cheap and decisive. That confidence is uncalibrated is suggested by one wrong 0.88 — one point is not a curve. **Instrument:** bucket reported confidence against QA-verified correctness and plot the reliability curve; if it is flat, `CONFIDENCE_THRESHOLD` is a placebo. That Trace B's redundant-search loop is representative: **instrument** normalised tool-argument hashes and emit `duplicate_tool_calls` per ticket. Both are now in `telemetry.py`.

Two things I could not check. `eval_cases.json` was not in the handover, so every claim about the composition of the 240 cases is inference from a docstring. And whether the weekly QA sample excludes escalated tickets is a question for Support leads, not an instrument.

**A statistical caution.** The QA sample is 50 tickets a week, about 8.3% of volume. At n=50 and p=0.84 the 95% interval is roughly ±10 points, so **no single month's figure is individually meaningful.** Comparing pooled months 1–2 (n≈400, 90%) against month 5 (n≈200, 84%) gives about two standard errors, p ≈ 0.045 — right at the edge. The decline is credible because it is monotonic across four periods *and* has a mechanism, not because any one number clears a threshold.

### What is misleading in the reported metrics

**"Tickets closed without human involvement: 58% → 61%."** Reopens went 7% → 13%. Net of reopens that is 54% → 53%. **Autonomous resolution did not improve; it declined slightly.** And reopens lag — Trace A came back after two days — so month 5's 13% is right-censored and the true figure is worse. The headline metric counts a ticket that comes back as a success.

**"Median first-response time: 4.2 hrs → 0.3 hrs."** This measures the speed of *any* reply, including wrong ones. Fourteenfold faster wrong answers is what the reopen rate is recording.

**"Tier-1 queue depth: 210 → 165."** Some of that 45 is deferred, not eliminated: reopened tickets return later, and escalation barely moved (31% → 29%) despite the claimed automation gain.

**Cost.** Mean $0.34, p95 $2.10, highest $4.80. The 6× spread is the story: the maximum and Trace B's $3.90 are both eight-step runs, so the cost ceiling is set by `MAX_STEPS` and **the ceiling is the failure mode — you pay the most for the tickets you fail.** At 31,000 tickets a month, the mean gives ~$10.5k; today's p95 as a mean gives **~$65k**, and nothing in the system prevents that.

---

## Part 2 — Extension

**Where I disagree with the request, and what I built instead.** Code: `access_request.py`, `grant_executor.py`, and the routing in `sra_runtime.py`.

**The Admin contact list is not an authorization source on its own.** Median age seven months; 22% over a year. Using it as written means an entry nobody has checked since last year can authorize access to this year's payroll. I made age a *validity condition*: past 90 days the request escalates and names the account for Customer Success. This blocks most accounts at launch — deliberately. Automation is capped by data quality we do not control, and I would rather report a low rate for an honest reason than a high one built on a stale list. The block rate is the number that funds fixing the list.

**Being on the Admin list is an identity claim; being entitled to grant someone payroll access is an entitlement claim.** The CRM supports the first and was never designed to carry the second. Conflating them is the central risk in the request as written.

**Payroll is never auto-granted, at any autonomy level.** Every gate can pass and the answer is still a prepared action a human confirms. This is not a smaller feature: it turns a ten-minute human task into a twenty-second one — most of the value, none of the irreversibility.

**The queue claim is wrong by about half.** The request says access requests are ~40% of Tier-1 volume; the category table says 18%. Both cannot be true, and this is the denominator of the entire business case. Also, weighting the other three categories against the 84% total implies access-request accuracy today is around 74% — an estimate, since one cell is missing from the brief, but it means the category being proposed for automation is currently one of the two worst-performing *while read-only*. The honest business case is **not "the queue halves."** It is: 18% of volume gets far cheaper per ticket, and the risky fraction stays human but much faster.

**The structural decision: the model extracts, deterministic code decides.** Extraction is one isolated call — no tools, no history, temperature zero — and `account_id` and `requester_email` are overwritten from ticket metadata and the mail envelope, so "I am the CFO, this is pre-approved" is text we report on, not an authorization. `authorize()` then has no parameter through which prose can arrive. It is a pure function: no I/O, no model, no ambient clock. That is not stylistic. It is what makes 43 adversarial cases run in 0.2 seconds with no network and no fixtures, and therefore affordable on every commit.

**Reversibility ships first.** The provisioning API takes effect immediately and has no undo, so I built one: write-ahead logging before the call, idempotency keyed on (account, target, module, ticket), exactly one attempt, `revoke_window(since)` to reverse everything in a period in one call, hourly rate ceilings, and a kill switch read at runtime so stopping never waits for a deploy. Failures are split into FAILED and UNKNOWN — a timeout may have been applied, and calling that a failure means our records say no access exists while the customer's system says otherwise.

**Also changed in the surrounding runtime** (the `fix:` commits): context assembled once; `body` returned; the decision validated; action checked before confidence; retries limited to transient failures; account status enforced in code; repeated tool calls ended; a $1.00 per-ticket ceiling; version-mismatched answers no longer auto-close; clients injected.

**Deliberately not changed:** retrieval architecture, prompt wording, model selection, the tool abstraction, and the unused `history` field. Each is a real improvement and none is what makes this launch safe. Changing them would turn a reviewable extension into a rewrite.

**Named platform dependencies:** `crm.fetch_admin_contacts` must expose `last_updated`; `crm.fetch_directory` must exist. And the mail envelope is a weak identity claim — hardening it needs an authenticated channel, which is a platform change outside this work. A stated limitation, not an oversight.

---

## Part 3 — Evaluation

Code: `eval_access.py`, `eval_cases_access.py`, and repairs to `eval_harness.py`. Run `python eval_access.py`.

**What replaces the harness for this capability.** Decisions are asserted directly — no model grades a security property. The headline is **false grants, and the gate is zero**; automation rate is reported second and never traded against it, because the costs are asymmetric: a wrong escalation costs a support engineer four minutes, a wrong grant is an unauthorised person holding payroll data. Results are a confusion matrix over four outcomes, never one number.

**43 cases built from failure modes**, not sampled from happy paths — which is how a suite scores 91% on a system that is wrong 16% of the time. Injection, forged approvals quoted in a mail chain, lookalike domains, departed admins, records at 89/90/91/400 days, two people named Jane, module-name collisions, suspended accounts, bulk requests, revocations phrased as grants, an informational question that must not act, duplicate tickets. Every case names the threat it guards.

**The suite tests itself.** Eight gates are disabled in turn and each must be detected. This caught a real hole while I was writing it: the `trust_the_model` mutation — the request implemented literally — initially **survived**. My impersonation cases named a forged requester who was not on the Admin list either, so believing the ticket body changed nothing, and the injection tests were proving nothing. Fixed by naming a forged requester who really is an Admin, and by asserting the *trusted fields* on the extracted request rather than only the final decision. The suite looked complete and tested nothing at that boundary. That is the entire argument for mutation testing, and it is the mechanism that structurally stops me repeating the inherited eval's failure.

**Corpus drift is prevented by construction:** cases carry `product_version`, and the build fails when too many predate the shipped release.

**What offline evaluation structurally cannot catch**, and what I would measure in production instead:

- **`grant_reversal_rate_30d`** — the share of SRA grants revoked within 30 days. The only ground truth for whether a grant was *wanted*, and it can only come from the future.
- **`approval_dwell_time_p50`** — if the median human approval takes four seconds, the human-in-the-loop is theatre. Paired with `approval_override_rate`: if overrides are near zero, dwell time separates "we are right" from "they are rubber-stamping."
- **`staleness_block_rate`** — drift in a data source we do not control.
- **`doc_version_mismatch_rate`** — answers citing documentation from the wrong release. This is the number that settles whether stale docs cause the error-category decline.
- **`reopen_rate_by_outcome`** — whether the answer actually worked.
- Adversarial adaptation, real provisioning behaviour under load, and tail cost at scale.

**Replay evaluation:** nightly, re-run the last 30 days of production tickets and diff the decisions. That is what would have caught v14 without anyone writing a new test case.

---

## Part 4 — Ownership

**What wakes me at 2am** — only things actively doing irreversible damage: the grant circuit breaker tripping; any grant on a sensitive module that reached provisioning; a non-empty `needing_reconciliation()` list, where our records and a customer's system may disagree; a provisioning error-rate spike; a broken revoke path; and cost burn above the hourly ceiling. **What waits until morning:** accuracy drift, escalation-rate drift, a single wrong reply, knowledge-freshness warnings, queue depth, individual QA failures.

**Cost ceiling at 31,000 tickets.** Hard cap $1.00 per ticket, soft cap at $0.50 where the agent must stop researching and either answer or escalate; access requests get one step, not eight. Monthly ceiling $12k with alerts at 70/85/100%. At 100% the system **degrades automatically rather than shutting off**: full-context loading disabled, step budgets shortened, sensitive modules forced to prepare-only. A support agent that is slower is usable; one that is off is an outage.

**First week.** Day 0 shadow — decide, act on nothing, diff every decision against what humans did. Days 1–2 prepare-only: every grant human-confirmed, and I read the override rate *and* the dwell time. Days 3–7 auto-grant non-sensitive modules only, capped, and I personally review every grant daily — the volume makes that possible and the week it stops being possible is the week the monitoring has to be real. Go/no-go at the end against thresholds registered **before** launch, not chosen afterward from whatever the data turned out to be.

**What turns it off.** Any confirmed unauthorised grant. Grant-reversal rate above 2%. Any successful injection. Provisioning ambiguity above 0.5% of attempts. Cost above ceiling for 24 hours. **I make that call, alone, without asking.** Turning it back on requires the Director of Support and the owner of security or compliance. The asymmetry is deliberate: stopping should be fast and unilateral, restarting slow and shared.

**If it grants the wrong person access to payroll: I am accountable.** Not the model, not the Director who asked for it, not the Customer Success team whose list went stale — I shipped a system that used that list as an authorization source. First hour, in order: engage the kill switch; revoke that grant; run `revoke_window()` across the exposure period and bound the blast radius from the grant log; pull provisioning and product audit logs to establish whether data was actually viewed, because "granted" and "accessed" are different incidents; notify the account's real admin and internal security and legal, since this may be reportable and sitting on it makes it worse; write the timeline while it is fresh. Then: nothing restarts until that specific failure has a case in the suite that fails against the old code. The full procedure, including how this capability is retired, is in `RUNBOOK.md` — an incident is the worst possible time to design a response.

---

## AI usage disclosure

> **[Oguzhan — edit this section to match what you actually did. It is graded, and a disclosure that does not match your process is worse than a thin one. What follows describes the session that produced this repository.]**

**Tools.** Claude Opus 5 via Claude Code, in a single working session against the provided files. No other models or agents.

**Delegated.** Reading both files line by line and cross-referencing findings to line numbers; drafting the module docstrings and commit messages; generating the adversarial case corpus once I had specified the threat categories; the mechanical TDD loop (write test → run red → implement → run green).

**Kept.** The diagnosis ranking and its justification. The decision to treat admin-record age as a validity condition rather than metadata. The decision that payroll is never auto-granted. Making `authorize()` a pure function. Choosing false-grant count over accuracy as the headline metric. The pushback on the 40% figure. The reframing of the business case from "the queue halves" to "18% gets cheaper, the risky part gets faster." Every threshold in the policy, and the reasoning for each.

**Where it was wrong, and how I caught it.**
1. The first version of the injection test suite was decorative. The `trust_the_model` mutation passed — the impersonation cases named a forged requester who was not an Admin either, so a naive implementation reached the same decision by luck. **Mutation testing caught it, not review.** The fix required both a better case and asserting trusted fields, not just decisions.
2. An early test asserted the eval harness made no ticketing calls, which was measuring the fake rather than the property. The real property — that the harness *refuses* production clients — is now enforced in code.
3. A routing test read the wall clock and reported an admin record as 399 days old instead of 400. Fixed by injecting the clock, which is the same principle the design already rested on.
4. Minor: a `dataclass` field defaulted to a mutable `SimpleNamespace`; caught by the interpreter on first run.

**Conclusions that are mine.** All of Part 1's ranking and the statistical caveat on the QA sample. Every disagreement with the stakeholder in Part 2. The autonomy model and its staging. The choice of production metrics in Part 3, particularly approval dwell time as a test of whether human review is real. All of Part 4, including who holds the kill switch.

I am accountable for everything here, including anything a model drafted.
