-- Persistent preview accounts and opaque sessions for Stage 05.
-- Passwords and session/CSRF tokens are stored only as one-way hashes.
CREATE TABLE IF NOT EXISTS user_accounts (
    user_id uuid PRIMARY KEY REFERENCES users(user_id),
    identifier text UNIQUE NOT NULL,
    password_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    token_sha256 char(64) UNIQUE NOT NULL,
    csrf_sha256 char(64) NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at timestamptz
);

CREATE INDEX IF NOT EXISTS stage05_auth_sessions_lookup_idx
    ON auth_sessions(token_sha256, expires_at)
    WHERE revoked_at IS NULL;

INSERT INTO schema_migrations(migration_id)
VALUES ('0006_stage05_auth_sessions')
ON CONFLICT (migration_id) DO NOTHING;
