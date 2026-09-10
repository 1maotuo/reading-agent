-- Stage 05 local production-preview state bridge.
-- The normalized domain tables remain the long-term source of truth.  This
-- table lets the current vertical-slice runtime restart against PostgreSQL
-- while its in-memory contract objects are migrated incrementally.
CREATE TABLE IF NOT EXISTS stage05_preview_state (
    singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
    schema_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 CHAR(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (migration_id)
VALUES ('0005_stage05_preview_state')
ON CONFLICT (migration_id) DO NOTHING;
