---
name: reading-agent-dev-loop
description: Implement focused Reading Agent product changes with bounded planning, targeted verification, and strict time and model-cost control. Use for feature, fix, and refactor work in this repository; do not use for open-ended product discussion.
---

# Reading Agent Development Loop

Keep the product centered on helping readers understand a book and feel continuous companionship. Prefer a small fixed runtime and observable learning loop over general agent machinery.

## Work in bounded batches

1. Define one user-visible outcome, its smallest coherent slices, and acceptance evidence once per batch.
2. Implement slices incrementally. Preserve existing contracts unless changing one is required for the outcome.
3. After each slice, run only tests directly affected by changed behavior. Do not rerun an unchanged command when neither code nor fixtures changed.
4. At the batch boundary, review the diff and fresh targeted test output once. Perform at most one focused repair pass.
5. Save each accepted feature as a small Git checkpoint before starting a materially different module.

## Control time and cost

- Reuse the already established code map; inspect only callers, contracts, and tests needed for the active change.
- Do not add frameworks, dependencies, generic abstractions, broad documentation, or speculative production infrastructure.
- Prefer deterministic tests and existing fakes. Use paid model calls only for a few end-to-end cases after local behavior passes.
- Do not run the full suite, browser E2E, PostgreSQL/MinIO integration, or live-vector checks unless the changed boundary makes them necessary.

## Recover from failure

- Reproduce and localize before changing code; test one root-cause hypothesis at a time.
- Preserve working fallback paths. A failed optional model, vector, memory, or companion step must not discard a valid user question or verified book evidence.
- If the focused repair still fails, keep the last accepted checkpoint intact and report the concrete blocker instead of broadening the change.

Pause for external accounts, new paid services, destructive data changes, production cutovers, or major product decisions. Continue through ordinary implementation and local failures.
