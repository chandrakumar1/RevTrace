# RevTrace — Working Agreement for Coding Agents

Read this before touching anything. It is not a specification of what to build;
it is a description of what already exists and what you must not break.

The product documentation is [README.md](README.md). The architecture record is
[docs/architecture.md](docs/architecture.md). This file is about *how to work in
this repository*.

---

## 1. What RevTrace is

An AI revenue recovery system for the Razorpay Buildathon (Track 03) that decides
**when payment recovery is worth attempting**, measures whether the attempt
caused the recovery, and refuses to act when it cannot tell.

Three properties define it, and every one of them is easy to destroy by
accident:

- **Deterministic code owns the money.** Revenue, risk, uplift, policy and
  execution authorisation are ordinary tested arithmetic.
- **Recovery is measured against a randomised holdout**, so "we caused this" is
  a checkable claim.
- **Abstaining is a valid outcome**, recorded with a named reason.

## 2. The authority boundary — non-negotiable

**The LLM is not the authority over money.**

| The model may | Deterministic code must own |
|---|---|
| Diagnose, interpret evidence, reason | Revenue and risk calculations |
| Explain | Expected-recovery calculations |
| Recommend among *permitted* actions | Policy enforcement, limits, retry counts |
| Draft communication | Stopping rules, execution authorisation, verification, metrics |

A model output that is a number must be **checked against a computed value**
before it is believed. That is what `engine/falsification.py` is for. A model
must always be able to answer "insufficient evidence" instead of fabricating
certainty.

An agent module that imports a payment-provider client is a bug.

## 3. Protected surfaces

**Do not modify these without an explicit human instruction naming the file.**
They are load-bearing for claims the project makes publicly.

| Surface | Why |
|---|---|
| `app/causal/*` | Every uplift, Qini, quadrant and power figure |
| `app/reporting/evaluation.py` | The sole reader of simulator ground truth |
| `app/engine/falsification.py` | What makes a model claim checkable |
| `app/engine/policy_engine.py` | The act/abstain/escalate decision |
| `app/repositories/experiment_result_repository.py` | Result persistence |
| `alembic/versions/*` | Applied migrations are history, not source |
| Simulator truth generation | The answer key |
| `docs/EXPERIMENT_DESIGN.md` | A **pre-registration**. Never amend it after the fact |
| `docs/EVALUATION.md`, `docs/evaluation.json` | Generated artifacts — regenerate, never hand-edit |
| `frontend/src/fixtures/*.json` | Committed backend output; hand-editing makes them evidence of nothing |

**Run the full test suite before and after touching any of them.** The current
baseline is **4,573 passed, 0 failed, 0 skipped**.

## 4. Rules that are not style preferences

**No floating-point arithmetic in money or probability paths.** Money is an
integer count of minor units; rates and effects are integer basis points out of
10,000. This holds on both sides of the wire — the frontend formats by slicing
the decimal string, never by dividing. See ADR 0001.

**`null` is never `0`.** An undefined Qini coefficient is not a coefficient of
zero. Rendering absence as zero turns a missing measurement into a measurement.

**`truth_*` fields are simulator-only ground truth.** They must never appear in
`app/causal/` or `app/engine/`. AST-based guards enforce this; do not weaken them
to make a name fit — rename your variable instead.

**`as_of` is injected, never read from a clock.** A run that read the system
clock would not be reproducible, and reproducibility is the basis of the audit
trail.

**Refuse rather than guess.** An unmapped provider event, an absent ladder rung,
a response that is not the shape it claims — all raise. Mapping the unrecognised
to something plausible puts an invented meaning into evidence.

**Services do not commit.** The caller owns the transaction. A repository or
service that committed would decide on the caller's behalf that a partial run
should survive.

## 5. Databases — the separation is load-bearing

| Database | Role | Rule |
|---|---|---|
| `revtrace_test` | Ephemeral test and demo database | **Do not write persistent data to it.** Every test and the demo run inside a transaction that is always rolled back. Committing here has broken 11 test modules before. |
| `revtrace_hypothesis_test` | Persistent canonical benchmark population | **Do not modify casually.** Materialising it is a deliberate operator step, never a side effect. |
| `revtrace_dev` | The application database | **Never touched by tests or the demo.** Refused by name in both. |

Never drop, reset, truncate or recreate any of them. Never modify PostgreSQL
roles or server configuration.

If you must audit blast radius before a database change, search for *every* kind
of dependency — not just the obvious call sites. A previous audit searched
`materialise(` call sites, found six modules, and missed five more that merely
counted rows.

## 6. Razorpay is demo-only

- **No real Razorpay transaction has ever been processed**, and none should be.
- **No real credentials exist in this repository**, and none should be added.
  All provider settings default to empty.
- The demo provider is `app/integrations/razorpay/demo.py` — deterministic,
  offline, synthetic. Every identifier carries a `DEMO` marker.
- **Never present a synthetic provider response as real Razorpay data.** Use
  "Razorpay adapter interface", "offline demo provider", "synthetic webhook",
  "synthetic provider data", "production verification code path". Never "real
  Razorpay payment", "real transaction", or "live Razorpay webhook".
- Provider code stays in `app/integrations/razorpay/`. Nothing outside that
  package may see a Razorpay request or response shape.
- No Razorpay secret ever reaches the frontend.

The demo must remain: deterministic in structure and outcome, non-destructive,
credential-free, network-free, and always rolled back. Tests assert all of it,
including that the demo path cannot open a socket. **Do not weaken those tests to
make a change fit.**

## 7. AI provider rules

OpenRouter only, free models only, **zero paid-model fallback**. The free-only
lock is fail-closed: freeness is an explicit declaration defaulting to false, and
the `:free` suffix only corroborates it. `free_only_chain()` is the only
production constructor.

Never log or print a credential value, a prefix, a length, a hash, or any partial
value. Never log raw model content, reasoning text, payloads, customer data or
`truth_*` fields. Diagnostics are shapes and counts.

## 8. Git — human action only

**Perform no Git operation without an explicit human instruction to do so.** No
`add`, `commit`, `push`, `pull`, `fetch`, `checkout`, `reset`, `branch`, `merge`,
`stash`, `clean`, `tag`, and no `gh` or GitHub API call.

Never commit `.env`. Only `.env.example` is tracked.

## 9. How to work

**Milestones, not big-bang.** Inspect the existing code, implement only the
agreed scope, run the tests, fix failures, update the documentation, explain what
changed architecturally, and verify existing behaviour still works.

**Audit before you change.** For anything touching a protected surface or a
database, do a read-only audit first and report it before editing.

**Do not make unrelated architectural changes.** Do not invent external API
behaviour — use the official documentation. Do not introduce microservices; this
is a modular monolith and should stay one.

**When a test fails, decide honestly whether the code or the test is wrong.**
Both happen. Changing a test to make code pass is only correct when the test
encoded the wrong expectation, and you should say so explicitly.

**Report outcomes faithfully.** If tests fail, say so with the output. If a step
was skipped, say that. Do not describe planned work as completed work.

## 10. Commands

```bash
cd backend
.venv/bin/python -m pytest                 # 4,573 passed
.venv/bin/ruff check app tests ../simulator
.venv/bin/ruff format --check app tests ../simulator
.venv/bin/mypy app

cd ../frontend
npm run typecheck && npm run build
```

`alembic/` is deliberately excluded from lint paths: applied migrations are
history and must not be reformatted.

Setup, run commands and the demo are in [README.md](README.md#9-running-it-locally).
