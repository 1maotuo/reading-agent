-- Stage 05 answer history and replay boundary. Event payloads are stored as
-- structured JSON; raw prompts, model keys, and full book contents are not
-- copied into this schema by the adapter.
CREATE TABLE IF NOT EXISTS answer_runs (
    run_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    book_id uuid NOT NULL,
    book_version_id uuid NOT NULL,
    chapter_id uuid,
    trace_id uuid NOT NULL,
    request_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    question text NOT NULL,
    answer_text text NOT NULL DEFAULT '',
    status text NOT NULL CHECK (status IN ('accepted', 'running', 'completed', 'failed', 'cancelled')),
    evidence_ids jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence_ids) = 'array'),
    created_at timestamptz NOT NULL,
    finished_at timestamptz,
    model_name text,
    tool_call_count integer NOT NULL DEFAULT 0 CHECK (tool_call_count >= 0),
    error_code text,
    FOREIGN KEY (user_id, book_id) REFERENCES books(user_id, book_id),
    FOREIGN KEY (user_id, book_id, book_version_id)
        REFERENCES book_versions(user_id, book_id, book_version_id)
);

CREATE TABLE IF NOT EXISTS answer_events (
    run_id uuid NOT NULL REFERENCES answer_runs(run_id) ON DELETE CASCADE,
    seq integer NOT NULL CHECK (seq >= 1),
    trace_id uuid NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('accepted', 'tool_started', 'tool_finished', 'evidence', 'answer_delta', 'completed', 'failed', 'heartbeat')),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    emitted_at timestamptz NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE INDEX IF NOT EXISTS stage05_answer_runs_scope_idx
    ON answer_runs(user_id, book_id, book_version_id, created_at);
CREATE INDEX IF NOT EXISTS stage05_answer_events_replay_idx
    ON answer_events(run_id, seq);

INSERT INTO schema_migrations(migration_id)
VALUES ('0004_stage05_answer_runs')
ON CONFLICT (migration_id) DO NOTHING;
