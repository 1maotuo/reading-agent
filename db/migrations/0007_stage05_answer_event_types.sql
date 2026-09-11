-- Keep the durable answer ledger aligned with the public SSE contract.
-- This is additive: it only widens the event type check used by the
-- PostgreSQL answer replay store.
ALTER TABLE answer_events
    DROP CONSTRAINT IF EXISTS answer_events_event_type_check;

ALTER TABLE answer_events
    ADD CONSTRAINT answer_events_event_type_check
    CHECK (event_type IN (
        'accepted', 'status', 'tool_started', 'tool_finished', 'evidence',
        'answer_delta', 'completed', 'failed', 'cancelled', 'heartbeat'
    ));

INSERT INTO schema_migrations(migration_id)
VALUES ('0007_stage05_answer_event_types')
ON CONFLICT (migration_id) DO NOTHING;
