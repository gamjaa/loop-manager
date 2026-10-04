---
id: G-134
title: Preserve Agent Invocation Outcomes and Evidence
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-063] Manager-Owned Deterministic Work and Minimal Model Use.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

Separate a completed physical Agent invocation from best-effort log persistence and align timeout classification with durable invocation evidence. The source review found that a post-exit log read failure can trigger another physical call, while `timed out` and `timeout` produce different result labels and a repeated finish can invalidate evidence for an already successful call.

## Acceptance Criteria

- A completed CLI call retains its actual result, session and usage observation when copying an auxiliary log fails; the copy failure is recorded without launching the same physical call again.
- Timeout classification and ledger kind agree for supported timeout messages. A missing session on the latest physical attempt never inherits an earlier session.
- Repeated or conflicting finish requests cannot leave a successful invocation row paired with invalidated isolation evidence. Existing invocation IDs, stored rows, result protocol, retry limits and Agent isolation gates remain valid.

## Implementation

- Keep CLI process outcome and auxiliary log persistence as separate effects at the provider adapter and invocation recording boundary. Make result classification shared by the caller and ledger writer, and make finish conditional on the persisted invocation state.
- Preserve existing storage schema and public Agent invocation interface; retain error evidence needed for recovery without copying prompts or secrets into logs.
- The responsibility boundary is the physical CLI outcome versus auxiliary observation and ledger finalization. Preserve user behavior, public contract and storage format by retaining invocation identity, result fields and existing retry decisions while isolating log-copy errors.

## Validation

- Worker runs targeted unit tests in `tests/test_agent_invocation.py`, `tests/test_agent_invocation_state.py` and `tests/test_agent_provider_usage.py` for post-exit log I/O failure, both timeout spellings, absent final session and conflicting finish replay.
- Full verify confirms normal CLI success, failure, fallback, isolation and usage recording still pass without duplicate physical invocation.

## Progress

- [ ] Separate physical result from auxiliary evidence failure and align durable classification.
- [ ] Add focused replay and failure regression tests.

## Decision Log

- Source revision `79dd511a962fa0d2ba2530554fa0d945d64b8f17`, manifest findings 0, 1 and 37 show one invocation-result boundary; finding 46 identifies a direct missing regression case. No storage-format or retry-policy change is intended.
- Observed responsibility paths: `loop_manager_internal/agents/provider_adapters.py`, `loop_manager_internal/agents/agent_invocation.py`, `loop_manager_internal/storage/agent_invocation_state.py`.
