-- Stage 04 contract-only DDL draft.  STATIC: DO NOT EXECUTE.
-- The real database, pgvector extension, 10k representative run, p95 sample,
-- and EXPLAIN/index evidence are BLOCKED_BY_F03_02.
-- BLOCKED_BY_F03_02: no PostgreSQL/pgvector execution is claimed by this file.

-- BLOCKED_BY_F03_02: CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
    user_id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL,
    deleted_at timestamptz
);

CREATE TABLE sessions (
    session_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    token_hash char(64) NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);

CREATE TABLE books (
    book_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    title text NOT NULL,
    format text NOT NULL CHECK (format IN ('pdf', 'epub', 'txt', 'markdown')),
    active_version_id uuid,
    status text NOT NULL CHECK (status IN ('active', 'deleting', 'deleted')),
    created_at timestamptz NOT NULL,
    row_version bigint NOT NULL CHECK (row_version > 0),
    UNIQUE (user_id, book_id)
);

CREATE TABLE book_versions (
    book_version_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    book_id uuid NOT NULL,
    file_sha256 char(64) NOT NULL,
    pipeline_version text NOT NULL,
    status text NOT NULL CHECK (status IN ('building', 'ready', 'failed', 'deleting', 'deleted')),
    created_at timestamptz NOT NULL,
    published_at timestamptz,
    UNIQUE (user_id, book_id, book_version_id),
    FOREIGN KEY (user_id, book_id) REFERENCES books(user_id, book_id)
);

ALTER TABLE books
    ADD CONSTRAINT books_active_version_fk
    FOREIGN KEY (user_id, book_id, active_version_id)
    REFERENCES book_versions(user_id, book_id, book_version_id);

CREATE TABLE chapters (
    chapter_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    book_id uuid NOT NULL,
    book_version_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    title text NOT NULL,
    source_locator jsonb NOT NULL,
    UNIQUE (user_id, book_id, book_version_id, chapter_id),
    UNIQUE (book_version_id, ordinal),
    FOREIGN KEY (user_id, book_id, book_version_id)
        REFERENCES book_versions(user_id, book_id, book_version_id)
);

CREATE TABLE blocks (
    block_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    book_id uuid NOT NULL,
    book_version_id uuid NOT NULL,
    chapter_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    body text NOT NULL,
    text_sha256 char(64) NOT NULL,
    source_locator jsonb NOT NULL,
    UNIQUE (user_id, book_id, book_version_id, chapter_id, block_id),
    UNIQUE (chapter_id, ordinal),
    FOREIGN KEY (user_id, book_id, book_version_id, chapter_id)
        REFERENCES chapters(user_id, book_id, book_version_id, chapter_id)
);

CREATE TABLE chunks (
    chunk_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    book_id uuid NOT NULL,
    book_version_id uuid NOT NULL,
    chapter_id uuid NOT NULL,
    chunk_index integer NOT NULL CHECK (chunk_index >= 0),
    block_ids jsonb NOT NULL CHECK (jsonb_typeof(block_ids) = 'array' AND jsonb_array_length(block_ids) > 0),
    body text NOT NULL,
    text_sha256 char(64) NOT NULL,
    token_count integer NOT NULL CHECK (token_count >= 0),
    embedding_model text NOT NULL,
    chunker_version text NOT NULL,
    -- BLOCKED_BY_F03_02: embedding vector(1024) is not executable in this draft.
    UNIQUE (chapter_id, chunk_index),
    UNIQUE (user_id, book_id, book_version_id, chapter_id, chunk_id),
    FOREIGN KEY (user_id, book_id, book_version_id, chapter_id)
        REFERENCES chapters(user_id, book_id, book_version_id, chapter_id)
);

CREATE TABLE jobs (
    job_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    book_id uuid NOT NULL,
    type text NOT NULL CHECK (type IN ('import_book', 'delete_book')),
    status text NOT NULL CHECK (status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancel_requested', 'cancelled')),
    stage text NOT NULL CHECK (stage IN ('validate', 'stage_object', 'parse', 'build_blocks', 'build_chunks', 'embed', 'verify', 'publish', 'purge')),
    attempts integer NOT NULL CHECK (attempts BETWEEN 0 AND 3),
    lease_owner text,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    idempotency_key text NOT NULL,
    input_sha256 char(64) NOT NULL,
    pipeline_version text NOT NULL,
    checkpoint jsonb NOT NULL DEFAULT '{}',
    cancel_requested boolean NOT NULL DEFAULT false,
    retryable boolean NOT NULL DEFAULT false,
    error_code text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    row_version bigint NOT NULL CHECK (row_version > 0),
    UNIQUE (user_id, type, idempotency_key),
    FOREIGN KEY (user_id, book_id) REFERENCES books(user_id, book_id),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);

CREATE TABLE reading_progress (
    user_id uuid NOT NULL REFERENCES users(user_id),
    book_id uuid NOT NULL,
    book_version_id uuid NOT NULL,
    chapter_id uuid NOT NULL,
    last_chunk_index integer NOT NULL CHECK (last_chunk_index >= 0),
    furthest_chunk_index integer NOT NULL CHECK (furthest_chunk_index >= last_chunk_index),
    position jsonb NOT NULL,
    updated_at timestamptz NOT NULL,
    device_id text NOT NULL,
    row_version bigint NOT NULL CHECK (row_version > 0),
    PRIMARY KEY (user_id, book_id, book_version_id, chapter_id),
    FOREIGN KEY (user_id, book_id, book_version_id, chapter_id)
        REFERENCES chapters(user_id, book_id, book_version_id, chapter_id)
);

CREATE TABLE highlights (
    highlight_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    book_id uuid NOT NULL,
    book_version_id uuid NOT NULL,
    chapter_id uuid NOT NULL,
    start_endpoint jsonb NOT NULL,
    end_endpoint jsonb NOT NULL,
    exact_quote text NOT NULL,
    prefix text NOT NULL,
    suffix text NOT NULL,
    text_sha256 char(64) NOT NULL,
    orphaned boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL,
    UNIQUE (user_id, book_id, book_version_id, highlight_id),
    FOREIGN KEY (user_id, book_id, book_version_id, chapter_id)
        REFERENCES chapters(user_id, book_id, book_version_id, chapter_id)
);

CREATE TABLE conversations (
    conversation_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    book_id uuid NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (user_id, book_id, conversation_id),
    FOREIGN KEY (user_id, book_id) REFERENCES books(user_id, book_id)
);

CREATE TABLE conversation_turns (
    turn_id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations(conversation_id),
    user_id uuid NOT NULL REFERENCES users(user_id),
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    answer_run_id uuid,
    evidence_ids jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL
);

CREATE TABLE answer_runs (
    run_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    book_id uuid NOT NULL,
    book_version_id uuid NOT NULL,
    trace_id uuid NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('accepted', 'running', 'completed', 'failed', 'cancelled')),
    created_at timestamptz NOT NULL,
    FOREIGN KEY (user_id, book_id, book_version_id)
        REFERENCES book_versions(user_id, book_id, book_version_id)
);

CREATE TABLE answer_events (
    run_id uuid NOT NULL REFERENCES answer_runs(run_id),
    seq bigint NOT NULL CHECK (seq > 0),
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    emitted_at timestamptz NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE traces (
    trace_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES answer_runs(run_id),
    request_id uuid NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    model_name text,
    tool_call_count integer NOT NULL CHECK (tool_call_count >= 0),
    error_code text
);

CREATE TABLE idempotency_keys (
    user_id uuid NOT NULL REFERENCES users(user_id),
    operation text NOT NULL,
    key text NOT NULL,
    request_sha256 char(64) NOT NULL,
    book_id uuid NOT NULL,
    job_id uuid NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (user_id, operation, key)
);

-- These are an auditable index draft only.  Execution and planner evidence
-- remain BLOCKED_BY_F03_02 until the representative PostgreSQL run exists.
CREATE INDEX stage04_blocks_scope_idx
    ON blocks(user_id, book_id, book_version_id, chapter_id, ordinal);
CREATE INDEX stage04_chunks_scope_idx
    ON chunks(user_id, book_id, book_version_id, chapter_id, chunk_index);
CREATE INDEX stage04_jobs_claim_idx
    ON jobs(status, lease_expires_at);
CREATE INDEX stage04_answer_events_replay_idx
    ON answer_events(run_id, seq);

-- BLOCKED_BY_F03_02: expected composite indexes, 10,000 representative
-- chunks, repeated p95 SLO sampling, and EXPLAIN plans require the real DB.
-- No Seq Scan/zero-index/low-sample result from this static draft is evidence.
