# Walkthrough script — 7 minutes

**Format:** screen share, voiceover. Camera optional.
**Structure:** beats, not a script to read aloud. Reading verbatim sounds dead. The **bold lines are the ones that must land word for word** — everything else, say in your own words.

**Before recording**
- Two terminal tabs: one on the repo, one ready to run `python eval_access.py`.
- Editor font large enough to read at 720p.
- Open with `sra_runtime.py` and `eval_harness.py` side by side, scrolled to lines 152 and 47.
- Do one timed rehearsal. 7:00 is a ceiling, not a target — aim for 6:30.

---

### 0:00–0:40 · Open on the sharpest finding

No introduction. No "I'm going to cover four parts." Straight in.

> **"The offline eval went from 90 to 91 over the same five months production went from 90 to 84. A number that moves opposite to reality is worse than no number, because it manufactures confidence. Here's why it did that."**

Point at the two files on screen.

- `sra_runtime.py:152` — `finish()` returns `ticket_id` and `outcome`. No `body`.
- `eval_harness.py:47` — `result.get("body", "")`.

> **"The judge graded an empty string. On all 240 cases. Whatever 91% measured, it wasn't resolution correctness — so 91 and 84 were never comparable quantities."**

---

### 0:40–2:00 · Diagnosis, three things, in order

**One — stale knowledge, and no way to notice.** Show `load_context`, the original version.

Nothing in the assembled context carries a product version or a document age. That's Trace A: a clear, confident 0.88 answer describing the v13 approval engine, sent to a customer on v14, auto-closed, reopened two days later. The agent couldn't have known.

**Two — the eval structurally cannot catch it.** The 240 cases were built at launch and never updated. v14 changed the error codes ten weeks ago. So for a v14 error code, the suite's reference answer is now the *wrong* answer — a correct response would score as a failure.

**Three — cost.** Scroll to line 91.

> **"`load_context` is inside the step loop. The full 180-chunk document set gets re-appended on every step. That's Trace B's $3.90."**

Then the number that matters:

> **"Mean is 34 cents, p95 is $2.10. At 31,000 tickets a month, if today's p95 became the mean, that's about $65,000 a month — and there is nothing in this system that prevents it. The observed maximum and Trace B are both eight-step runs, which means the cost ceiling is set by MAX_STEPS. You pay the most for the tickets you fail."**

---

### 2:00–2:50 · The metrics are misleading

Show the business metrics table.

> **"Leadership reports autonomous resolution went from 58 to 61 percent. Reopens went from 7 to 13. Net of reopens that's 54 to 53. It went down."**

And:

> **"Reopens lag — Trace A came back after two days — so month five is right-censored. The real number is worse than 13."**

One more, quickly: first-response time went from 4.2 hours to 0.3. That measures the speed of *any* reply, including the wrong ones.

---

### 2:50–4:20 · Where I disagree with the Director

This is the segment that differentiates the submission. Don't rush it.

**The number is wrong.**
> **"The request says access requests are 40% of Tier-1 volume. Your own category table says 18%. Both can't be true, and that's the denominator of the entire business case."**

**The authorization source is wrong.**
> **"The plan is to check whether the requester is on the Admin contact list. That list has a median age of seven months and 22% of accounts haven't been touched in over a year. That's not an authorization source, it's a stale cache."**

Add the distinction — say it slowly:
> **"Being on the Admin list is an identity claim. Being entitled to grant someone access to payroll data is an entitlement claim. The CRM supports the first. It was never designed to carry the second."**

**But I'm not saying no.** Reframe:
> **"18% of volume gets a lot cheaper per ticket, and the risky fraction stays human but goes from ten minutes to twenty seconds. That's the honest case. Not 'the queue halves.'"**

**The architecture.** Show the `authorize()` signature.

> **"The model extracts. Deterministic code decides. The invariant is that no attacker-controlled text is in scope at the point the authorization decision is made — look at the parameters, there's no argument through which prose can arrive."**

Mention in one breath: `account_id` comes from ticket metadata, requester from the mail envelope. "I am the CFO, this is pre-approved" is text we report on, not an authorization.

And: payroll is never auto-granted at any autonomy level. Every gate can pass and it's still a prepared action for a human.

---

### 4:20–5:30 · Show it running

This is the only segment where you show rather than tell. Worth more than three minutes of narration.

```
python -m pytest tests/ -q
```
→ 194 passed, ~0.2 seconds.

> **"That's 194 tests with no clock, no network, and no fixtures — because the authorization decision is a pure function. That's not a style choice. It's what makes it affordable to run this on every commit instead of by hand before a release."**

```
python eval_access.py
```

Point at two lines in the output:
- `false grants: 0` — the gate
- `automation rate` — reported second, never traded against the first

> **"Accuracy is the wrong headline here. A wrong escalation costs a support engineer four minutes. A wrong grant is an unauthorised person holding payroll data. Averaging those hides the only outcome that matters."**

Then scroll to the mutation table — eight gates, each disabled in turn, each detected.

> **"And this is the part I'd want you to look at. Each gate gets disabled and the suite has to fail. When I first ran this, `trust_the_model` — the Director's request implemented literally — survived. My injection tests were proving nothing, because the forged requester I'd written wasn't an admin either, so believing the ticket body changed nothing. The suite looked complete and tested nothing at that boundary."**

> **"That's the whole argument. The eval I inherited failed in exactly that way, and mutation testing is the only mechanism that structurally stops me repeating it."**

---

### 5:30–6:40 · Ownership

Rapid, no hedging.

**2am vs morning.**
> **"Only things actively doing irreversible damage page me: the grant breaker tripping, a sensitive-module grant that reached provisioning, or a grant stuck in UNKNOWN — where our records and the customer's system may already disagree. Accuracy drift waits until morning. The distinction isn't severity, it's reversibility."**

**Cost.** Hard cap $1.00 per ticket, $12k monthly ceiling. At 100%, it degrades — shorter step budgets, sensitive modules forced to prepare-only — it doesn't switch off. A slower support agent is usable; an absent one is an outage.

**The hard question.**
> **"If it grants the wrong person payroll access, I'm accountable. Not the model, not the Director who asked for it, not the Customer Success team whose list went stale — I shipped a system that used that list as an authorization source."**

First hour, four beats: kill switch, `revoke_window()`, bound the blast radius from the grant log, then audit logs — because "granted" and "accessed" are different incidents — then security and legal, because it may be reportable.

> **"And I hold the kill switch alone. Turning it back on needs the Director plus security. Stopping should be fast and unilateral; restarting slow and shared."**

---

### 6:40–7:00 · Close on what you cut

> **"I went deep on the authorization decision and the eval, and deliberately light on everything else. Every other defect in this system produces a bad reply that a human reads and corrects. Only this one produces an irreversible act with no human in the path. Depth belongs where reversibility ends."**

Stop. Don't add a summary.

---

## Cut list, if you run long

In this order:
1. The first-response-time point at 2:50 (the reopen figure carries the argument alone).
2. The `pytest` run at 4:20 — keep `eval_access.py` and the mutation table, they are the payload.
3. Diagnosis point two, the frozen corpus (it reappears in the mutation segment anyway).

Never cut: the opening 40 seconds, the Director disagreement, or the mutation-test story.
