---
description: agent-principle.md을 우선 실무 적용 이건 참조용으로 일단 보관 하고있음
alwaysApply: false
---
# Engineering Principles

Shared development principles for individuals, teams, and AI agents.

---

## [Reliability] Guarantee Correct Outcomes

1. **SSOT** — Maintain a single source of truth (document/spec/data). Everyone works from that source only. Eliminate duplication.
2. **Contract Validation** — Define and validate contracts (inputs/outputs, schemas, constraints) to prevent misbehavior.
3. **Deterministic Workflow** — Design reproducible procedures where the same input always yields the same output. Minimize variability.

---

## [Failure Handling] Surface Problems Early and Handle Safely

4. **Fail-Fast** — At the first sign of abnormality, fail immediately and expose the cause. Prevent large costs at later stages.
5. **No Implicit Fallback** — Eliminate silent automatic fallbacks. Handle all failures and edge cases explicitly; on failure, stop or require a decision.
6. **Graceful Degradation** — Non-critical paths (notifications, analytics, location) must not block core flows (auth, order, payment). Isolate failure scope; core flows always apply Fail-Fast.
7. **Reversibility** — Design deployments, migrations, and structural changes to be easily reversible to lower the cost of mistakes.

---

## [State & Data] Handle State Changes Safely

8. **Explicit State** — Do not hide current state (progress, decisions, context). Record and share it explicitly.
9. **Immutability First** — Keep state and data immutable by default. Mutate only when necessary to reduce side effects.
10. **Idempotent Actions** — Design actions so that running them multiple times does not break the outcome. Make retries and automation safe.
11. **Statelessness** — Favor stateless components and APIs where possible. Easier to scale, test, and reason about.

---

## [Structure & Design] Maintainable Architecture

12. **Separation of Concerns** — Each module, function, and layer owns exactly one responsibility to make changes and tests easier.
13. **Simplicity First** — Prefer the simplest solution that works. Avoid over-engineering and premature abstraction. Add complexity only when justified.
14. **High Cohesion** — Keep related functionality together in a single module. Minimizes cross-module dependencies and clarifies ownership.
15. **Loose Coupling** — Communicate between modules through stable interfaces or events, not direct dependencies. Changes in one module should not ripple to others.
16. **Tool-First Architecture** — Prefer tool calls and structured interfaces over prompt persuasion to increase reliability and control.
17. **Explicit Dependencies** — Declare all dependencies (libraries, services, env vars). Never hide them so the environment is reproducible.

---

## [Security] Limit Access and Blast Radius

18. **Least Privilege** — Restrict permissions and access scope to the minimum so that mistakes or breaches have a narrow impact.
19. **Defense in Depth** — Layer multiple defenses rather than relying on a single line so that one breach minimizes damage.

---

## [Operations & Improvement] Continuously Understand and Improve the System

20. **Observability** — Make it possible to see "what happened" via logs, traces, and metrics to enable debugging and operations.
21. **Drift Detection** — Detect and alert when specs, code, environments, or data diverge over time. Correct the drift.
22. **Small Batches** — Deploy changes in small, frequent increments to reduce review, rollback, and debugging burden and shorten feedback loops.
23. **Test as Documentation** — Treat tests as living specifications that record "how the system should behave."
