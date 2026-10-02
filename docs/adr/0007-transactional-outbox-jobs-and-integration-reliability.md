
---

## Amendment — 2026-10-02: implemented in `src/outbox.py`

The decision above stopped being paper. What exists now, and what it proves:

| ADR-0007 requirement | Where it lives | Test that fails if it is removed |
|---|---|---|
| `outbox_events` committed in the same transaction as the business state | `src/outbox.py` `emit()` joins the caller's transaction (`_write_transaction`) | `test_the_event_commits_with_the_business_row_or_not_at_all` |
| Idempotent publisher | unique `(agency_id, idempotency_key)`; a replay returns the original event, different content is a conflict | `test_the_same_key_and_content_returns_the_original_event`, `..._is_a_conflict` |
| Delivery attempts + dead-letter | `outbox_deliveries` (PUBLISHED/FAILED/REQUEUED) + `DEAD_LETTER` after `max_attempts`, with increasing backoff | `test_an_event_stops_being_retried_and_becomes_a_dead_letter` |
| Failure must not roll back booking/payment truth | `publish_due()` records the failure and leaves business rows untouched | `test_a_transport_outage_does_not_touch_the_business_row` |
| Correlation ID propagation | `correlation_id` / `causation_id` on every event, self-causation refused | `test_a_child_carries_its_parents_correlation_and_causation` |
| Integration inbox for webhook idempotency | `integration_inbox`, unique `(agency, source, external_id)`; same id + new content refused | `test_the_same_webhook_twice_is_one_row_and_the_second_is_not_new` |

**Not built yet, and named rather than implied:** the background worker (a scheduler/systemd
unit calling `publish_due` on an interval) and the real transports (email, WhatsApp, webhook).
The module takes the publisher as a *callback* on purpose: the record and the retry discipline
are testable without a network, and the transport stays replaceable per tenant (ADR-0037).

**One addition §14 asked for and the table now enforces:** a stored event's *content* cannot be
rewritten — a trigger refuses any update touching type, aggregate, payload, actor, correlation
or idempotency key, while delivery state stays mutable. A queue whose letters can be edited
after the fact is not evidence.
