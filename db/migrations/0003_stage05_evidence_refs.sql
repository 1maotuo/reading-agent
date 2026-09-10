-- Stage 05 evidence readback boundary.  Keep evidence owned by the exact
-- user/book/version/chapter/chunk composite so answer gates never trust a
-- client-supplied reference.
CREATE TABLE IF NOT EXISTS evidence_refs (
    evidence_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    book_id uuid NOT NULL,
    book_version_id uuid NOT NULL,
    chapter_id uuid NOT NULL,
    chunk_id uuid NOT NULL,
    chunk_index integer NOT NULL CHECK (chunk_index >= 0),
    block_ids jsonb NOT NULL CHECK (jsonb_typeof(block_ids) = 'array'),
    quote text NOT NULL,
    content_sha256 char(64) NOT NULL,
    source_locator jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, book_id, book_version_id, evidence_id),
    FOREIGN KEY (user_id, book_id, book_version_id, chapter_id, chunk_id)
        REFERENCES chunks(user_id, book_id, book_version_id, chapter_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS stage05_evidence_scope_idx
    ON evidence_refs(user_id, book_id, book_version_id, chapter_id, created_at);

INSERT INTO schema_migrations(migration_id)
VALUES ('0003_stage05_evidence_refs')
ON CONFLICT (migration_id) DO NOTHING;
