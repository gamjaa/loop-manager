---
id: G-142
title: Separate Notification Schema and Incident Adoption
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-053] Timely Progress Notifications.md
depends_on:
  - G-158
complexity: medium
risk: medium
priority: normal
---

## Purpose

Make notification event projection and legacy incident adoption explicit. The notification repository's schema ensure path also interprets current planning/checkpoint state, while event rendering assembles a 13-column blocked-plan query by positional indexes. Schema maintenance can therefore change recovery policy or silently map the wrong event fields.

## Acceptance Criteria

- Schema preparation, legacy incident adoption and event projection have distinct callable responsibilities and preserve the current order and transaction conditions.
- Blocked-plan payload fields are named at query intake; a changed SELECT order cannot silently put a recovery, checkpoint or guidance value in the wrong notification field.
- Existing outbox and incident schema, event keys, delivery order, rendered user messages and duplicate suppression remain unchanged; restart neither drops nor duplicates an eligible Merge or blocked event.

## Implementation

- Keep storage schema in `notifications/repository.py`; move current-state incident adoption behind its existing lifecycle boundary and use a named row/projection at `notifications/events.py` before rendering.
- Reuse existing event keys and outbox persistence. No new notification event or external service is introduced.
- The responsibility boundary is schema setup versus incident adoption and named event projection. Preserve user behavior, public contract and storage format by retaining the same event keys, payload fields and outbox rows while moving policy decisions out of schema preparation.

## Validation

- Worker runs targeted unit tests in `tests/test_notification_events.py`, `tests/test_notification_incidents.py` and `tests/test_self_merge_handoff.py` for schema preparation, adoption interruption, blocked payload fields, persisted Merge outbox and replay.
- Full verify confirms notification delivery, restart and project-completion behavior without event duplication.

## Progress

- [ ] Separate schema, adoption and named event projection.
- [ ] Add field-order and persisted-outbox regression tests.

## Decision Log

- Manifest findings 20 and 21 identify the mixed notification responsibilities; finding 82 identifies an outbox assertion gap relevant to the same durable event boundary.
- Observed notification paths: `loop_manager_internal/notifications/events.py`, `loop_manager_internal/notifications/repository.py`.

- 2026-10-05 사용자 실행 순서 결정: G-158의 archive Requirement 감사·복구 준비 PR이 실제 Merge된 뒤 기존 세션과 후보를 보존하여 재개한다. 미충족 Requirement는 활성 입력으로 유지하고 조기 archive 강제를 완료 조건으로 적용하지 않는다. 재개 후 targeted test, Manager 전체 verify, Reviewer와 PR 게시를 통해 실제 복구를 확인하며 다른 안전 차단은 별도로 판단한다.
