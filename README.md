# Support Resolution Agent — diagnosis and access-request capability

Submission for the Principal AI-Native Engineer assignment.

## Read in this order

1. **[`docs/WRITEUP.md`](docs/WRITEUP.md)** — the written response. Parts 1–4 and the AI usage disclosure.
2. **[`access_request.py`](access_request.py)** — the capability. Types, module registry, the pure `authorize()`, and the extraction boundary. If you read one file, read this one.
3. **[`eval_access.py`](eval_access.py)** — the evaluation, including the mutation testing of the suite itself.
4. **[`docs/RUNBOOK.md`](docs/RUNBOOK.md)** — first 60 minutes after a wrong payroll grant, and how the capability is retired.
5. **[`docs/DESIGN.md`](docs/DESIGN.md)** — the design document written before the code, if you want the reasoning in full.

## Running it

No dependencies beyond `pytest`. Nothing to configure.

```bash
python -m pytest tests/ -q     # 194 tests, ~0.2s
python eval_access.py          # confusion matrix + mutation testing
```

The security-critical core runs for real. `access_request.py`, `telemetry.py`, and the evaluation import nothing outside the standard library — `authorize()` is a pure function, so the whole adversarial suite runs with no clock, no network, and no fixtures. That is the reason the design is shaped this way, not a side effect of it.

`sra_runtime.py` and `eval_harness.py` still import `platform_sdk`, which is internal and not part of this repository. `platform_sdk.py` here is a clearly-labelled test double whose every function raises `NotImplementedError`; it exists only so the repository can be imported and tested.

## The diff, in two halves

```
git log --oneline
```

| | |
|---|---|
| `baseline:` | the inherited files, unmodified, so everything after is reviewable as a diff |
| `docs:` | design document and implementation plan, written before the code |
| `chore:` `fix:` × 3 | **repairs to what I inherited** — telemetry, the runtime, the eval harness |
| `feat:` × 4 | **the new capability** — policy and `authorize()`, the grant executor, the eval, the routing |

The split is deliberate: repair and extension are separable, and a reviewer should not have to work out which is which. Every commit message states the evidence for the change — a line number, a trace, or a figure from the brief.

## What is here

| File | |
|---|---|
| `access_request.py` | Types, module registry, policy, the pure `authorize()`, extraction |
| `grant_executor.py` | Write-ahead log, idempotency, single-attempt execution, `revoke_window`, rate ceilings |
| `telemetry.py` | Trace schema: cost, tokens, tool arguments, document versions |
| `eval_access.py` | Decision-level harness, confusion matrix, mutation testing |
| `eval_cases_access.py` | 43 adversarial cases, each naming the threat it guards |
| `sra_runtime.py` | Inherited — repaired, then routed |
| `eval_harness.py` | Inherited — repaired |
| `platform_sdk.py` | Test double for the internal SDK |
| `tests/` | 194 tests |

## Three things worth knowing before you read the code

**The model extracts; deterministic code decides.** Extraction is one isolated call with no tools and no history, and `account_id` and `requester_email` are overwritten from ticket metadata and the mail envelope. `authorize()` then has no parameter through which attacker-controlled prose can arrive.

**Payroll is never auto-granted, at any autonomy level.** Every gate can pass and the answer is still a prepared action for a human. The reasoning is in the writeup; briefly, the Admin contact list has a median age of seven months and was never designed to carry entitlement claims.

**The evaluation tests itself.** Eight gates are disabled in turn and each must be detected. This caught a real hole while it was being written — the writeup says which one, and why that incident is the whole argument for the technique.
