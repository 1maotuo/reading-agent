-- Stage 05 production-persistence bootstrap for the already frozen contracts.
-- This migration is executable only against a dedicated project database.
-- It does not touch the Docker host, existing application containers, or data.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    user_id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL,
    deleted_at timestamptz
);

CREATE TABLE IF NOT EXISTS books (
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

CREATE TABLE IF NOT EXISTS book_versions (
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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'books_active_version_fk'
    ) THEN
        ALTER TABLE books
            ADD CONSTRAINT books_active_version_fk
            FOREIGN KEY (user_id, book_id, active_version_id)
            REFERENCES book_versions(user_id, book_id, book_version_id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS chapters (
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

CREATE TABLE IF NOT EXISTS blocks (
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

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    book_id uuid NOT NULL,
    book_version_id uuid NOT NULL,
    chapter_id uuid NOT NULL,
    chunk_index integer NOT NULL CHECK (chunk_index >= 0),
    block_ids jsonb NOT NULL CHECK (jsonb_typeof(block_ids) = 'array' AND jsonb_array_length(block_ids) > 0),
    text_sha256 char(64) NOT NULL,
    token_count integer NOT NULL CHECK (token_count >= 0),
    embedding_model text NOT NULL,
    chunker_version text NOT NULL,
    search_text text NOT NULL DEFAULT '',
    embedding vector(1024),
    UNIQUE (chapter_id, chunk_index),
    UNIQUE (user_id, book_id, book_version_id, chapter_id, chunk_id),
    FOREIGN KEY (user_id, book_id, book_version_id, chapter_id)
        REFERENCES chapters(user_id, book_id, book_version_id, chapter_id)
);

CREATE TABLE IF NOT EXISTS jobs (
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
    checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb,
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

CREATE TABLE IF NOT EXISTS reading_progress (
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

CREATE INDEX IF NOT EXISTS stage05_books_owner_idx
    ON books(user_id, book_id, status);
CREATE INDEX IF NOT EXISTS stage05_chapters_scope_idx
    ON chapters(user_id, book_id, book_version_id, ordinal);
CREATE INDEX IF NOT EXISTS stage05_blocks_scope_idx
    ON blocks(user_id, book_id, book_version_id, chapter_id, ordinal);
CREATE INDEX IF NOT EXISTS stage05_chunks_scope_idx
    ON chunks(user_id, book_id, book_version_id, chapter_id, chunk_index);
CREATE INDEX IF NOT EXISTS stage05_chunks_keyword_idx
    ON chunks USING gin (to_tsvector('simple', search_text));
CREATE INDEX IF NOT EXISTS stage05_chunks_vector_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS stage05_jobs_claim_idx
    ON jobs(status, lease_expires_at);

INSERT INTO schema_migrations(migration_id)
VALUES ('0002_stage05_postgres_bootstrap')
ON CONFLICT (migration_id) DO NOTHING;
