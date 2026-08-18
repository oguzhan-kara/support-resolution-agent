# Runbook — SRA access-request capability

Written before launch, because an incident is the worst possible time to design a response.

Owner: Oguzhan Kara. The kill switch is mine and I use it without asking.

---

## Kill switch

```
sra.access.kill_switch = true
```

Read at runtime on every request. **No deploy, no restart.** `authorize()` checks it before any other gate, so it stops a request that is otherwise flawless.

Turning it **on** is unilateral and needs no approval. Turning it **off** requires the Director of Support and the owner of security or compliance. The asymmetry is deliberate.

---

## What pages at 2am

Only conditions where the system is actively doing irreversible things, or where our records may already disagree with a customer's reality.

| Condition | Why it cannot wait |
|---|---|
| `GrantLog.needing_reconciliation()` is non-empty | A grant's outcome is UNKNOWN. We may have changed a customer's environment and not know it. |
| Circuit breaker tripped (hourly grant ceiling) | Either a fault is repeating or someone is driving it. Either way it is still running. |
| Any grant on a `RESTRICTED` module reached provisioning | This must be structurally impossible. If it happened, a control failed. |
| Provisioning error rate > 5% over 15 minutes | We are making irreversible calls into a system that is not answering properly. |
| `revoke_module_access` failing | The undo is broken while the do still works. |
| Cost burn > $50/hour | At 31,000 tickets a month this is the runaway shape. |

## What waits until morning

Accuracy drift. Escalation-rate drift. A single wrong reply. Knowledge-freshness warnings under 30 days. Queue depth. Individual QA failures. A rise in `staleness_block_rate` — that is Customer Success's queue, not an incident.

The distinction is not severity. It is reversibility. A wrong answer is embarrassing and fixable in business hours. A grant is not.

---

## Incident: SRA granted the wrong person access to payroll

**Accountability.** Mine. Not the model, not the Director who requested the capability, not the Customer Success team whose contact list went stale. I shipped a system that used that list as an authorization source.

Note first: **"granted" and "accessed" are different incidents.** Establish which one this is early, because it changes who must be told.

### First 60 minutes

**0–5 min — Stop.**
```
sra.access.kill_switch = true
```
Confirm it took effect: the next inbound access request should record `KILL_SWITCH_ENGAGED`. Do not investigate first. Stopping is cheap and reversible; another wrong grant is not.

**5–15 min — Reverse.**
```python
executor.revoke_window(since=<start of exposure window>)
```
Start the window generously — err toward revoking access someone still needs over leaving access someone should not have. A redundant revoke is a support ticket; a missed one is the incident continuing. This also reverses UNKNOWN records, which may have taken effect.

Verify each revoke returned successfully. Any that did not go on a list and get done by hand now.

**15–30 min — Bound the blast radius.**
From the grant log, answer:
- Which grants used the same code path or reason code since launch?
- Same account? Same module? Same admin record?
- Is this one bad record or a systematic gate failure?

The reason code on each grant is what makes this a query rather than an archaeology project. If the answer is "one stale admin record", the blast radius is one account. If it is `AUTHORIZED` across many accounts, a gate failed and the exposure is everything since deployment.

**30–45 min — Establish whether data was actually viewed.**
Pull provisioning and product audit logs for the granted user over the exposure window. Did they open the module? Export anything? A grant that was never used is a control failure. A grant that was used is a data incident with a different obligation.

**45–60 min — Notify.**
- The account's **actual** admin — from the CRM, not from whoever filed the ticket. If the ticket requester was the problem, they are not the contact.
- Internal security and legal. Payroll is compensation data; this may be reportable, and jurisdictions have clocks. Sitting on it while we "understand it better" makes it worse, not better.
- The Director of Support, so Tier-1 is not learning about it from a customer.

Say what is known and what is not. Do not estimate scope before the blast-radius query has run.

**Then — write the timeline while it is fresh.** Times, decisions, what was known at each point. Not for blame; because in three days nobody will remember the order.

### Before it comes back on

1. The specific failure has a case in `eval_cases_access.py` that **fails against the old code**. A fix without a failing test is a hypothesis.
2. The full suite passes, including all eight mutations.
3. Autonomy returns to `PREPARE_ONLY` regardless of where it was. Autonomy is re-earned, not restored.
4. The Director of Support and security sign off.

---

## Incident: grants stuck in UNKNOWN

`needing_reconciliation()` is non-empty. Our write-ahead log says we attempted a grant; the API never confirmed.

1. **Do not retry.** The request may have been applied and a retry may be a second grant.
2. Query the provisioning system for the current state of each (account, user, module).
3. If applied: append a `GRANTED` entry so the log matches reality, then decide whether it should have been.
4. If not applied: append `FAILED`, and handle the ticket by hand.
5. If provisioning cannot say: revoke, and tell the customer. An unknown grant is worse than a removed one.

Repeated UNKNOWNs are a platform problem, not a capability problem. Escalate to whoever owns provisioning.

---

## Degradation before shutdown

At 100% of the monthly cost ceiling the system degrades rather than stopping:

- full-context loading disabled, retrieval only
- step budgets shortened
- sensitive modules forced to `PREPARE_ONLY`
- access requests continue — they are the cheapest path in the system

A support agent that is slower is usable. One that is off is an outage, and the queue does not pause while we fix the budget.

---

## Retiring the capability

If this is switched off permanently, the grants it made **do not disappear**. Retirement is not a config change.

1. Export the full grant log — every account, user, module, and date this system provisioned.
2. Hand it to Customer Success with a re-verification list: every SRA-issued grant still active needs a human to confirm it is still appropriate. Some will have outlived the person who needed them.
3. Set autonomy to `OFF` rather than deleting the code, so the decision path keeps running in shadow. Its reason codes remain the cheapest available measurement of how bad the Admin contact list is.
4. Keep the grant log for the audit retention period. It is the only record that these grants were made by a system rather than a person, and someone will eventually need to know that.
5. Tell the accounts. An access grant made by an automated system that has since been withdrawn is something a customer's own auditors may ask about.

The capability can be retired. Its consequences cannot be, which is the same reason the reversal path was built before the first grant.
