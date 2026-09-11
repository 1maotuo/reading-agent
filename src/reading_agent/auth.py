"""PostgreSQL-backed preview authentication with opaque durable sessions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from datetime import timedelta
from uuid import UUID, uuid4

from .contracts import ErrorCode
from .domain import ContractViolation, utc_now
from .postgres_adapter import PostgresDatabase


_PBKDF2_ITERATIONS = 240_000


def _password_hash(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        padding = "=" * (-len(salt_text) % 4)
        salt = base64.urlsafe_b64decode((salt_text + padding).encode("ascii"))
        expected = base64.urlsafe_b64decode(
            (digest_text + "=" * (-len(digest_text) % 4)).encode("ascii")
        )
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, binascii.Error):
        return False


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PostgresAuth:
    """Store only password hashes and opaque session hashes in PostgreSQL."""

    def __init__(self, database: PostgresDatabase, *, session_hours: int = 8) -> None:
        self.database = database
        self.session_hours = session_hours

    def ensure_account(self, identifier: str, password: str, user_id: UUID | None = None) -> UUID:
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT user_id FROM user_accounts WHERE identifier = %s",
                (identifier,),
            ).fetchone()
            if existing is not None:
                return UUID(str(existing["user_id"] if isinstance(existing, dict) else existing[0]))
            account_id = user_id or uuid4()
            now = utc_now()
            connection.execute(
                "INSERT INTO users(user_id, created_at) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
                (account_id, now),
            )
            connection.execute(
                "INSERT INTO user_accounts(user_id, identifier, password_hash) VALUES (%s, %s, %s)",
                (account_id, identifier, _password_hash(password)),
            )
            return account_id

    def login(self, identifier: str, password: str):
        from .api import SessionView

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT user_id, password_hash FROM user_accounts WHERE identifier = %s",
                (identifier,),
            ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid credentials")
        stored_hash = row["password_hash"] if isinstance(row, dict) else row[1]
        if not _password_matches(password, stored_hash):
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid credentials")
        user_value = row["user_id"] if isinstance(row, dict) else row[0]
        user_id = UUID(str(user_value))
        session_id = uuid4()
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = utc_now()
        expires_at = now + timedelta(hours=self.session_hours)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions
                    (session_id, user_id, token_sha256, csrf_sha256, expires_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (session_id, user_id, _sha256(token), _sha256(csrf), expires_at, now),
            )
        return token, SessionView(session_id=session_id, user_id=user_id, expires_at=expires_at), csrf

    def authenticate(self, token: str | None):
        from .api import SessionView

        if not token:
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "authentication required")
        now = utc_now()
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT session_id, user_id, expires_at
                FROM auth_sessions
                WHERE token_sha256 = %s AND revoked_at IS NULL AND expires_at > %s
                """,
                (_sha256(token), now),
            ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "authentication required")
        values = row if isinstance(row, dict) else {
            "session_id": row[0], "user_id": row[1], "expires_at": row[2]
        }
        return SessionView(
            session_id=UUID(str(values["session_id"])),
            user_id=UUID(str(values["user_id"])),
            expires_at=values["expires_at"],
        )

    def csrf_for(self, token: str | None) -> str | None:
        # The raw CSRF token is returned only by login and kept in the client
        # cookie; subsequent checks use validate_csrf against its hash.
        return None

    def validate_csrf(self, token: str | None, csrf: str | None) -> bool:
        if not token or not csrf:
            return False
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT csrf_sha256 FROM auth_sessions WHERE token_sha256 = %s AND revoked_at IS NULL AND expires_at > %s",
                (_sha256(token), utc_now()),
            ).fetchone()
        if row is None:
            return False
        stored = row["csrf_sha256"] if isinstance(row, dict) else row[0]
        return hmac.compare_digest(str(stored), _sha256(csrf))

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked_at = %s WHERE token_sha256 = %s AND revoked_at IS NULL",
                (utc_now(), _sha256(token)),
            )
