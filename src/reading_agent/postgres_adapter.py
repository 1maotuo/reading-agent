"""Small PostgreSQL adapter for the Stage 05 production-persistence boundary.

The adapter implements only the already frozen book/read/progress/publish ports,
the PostgreSQL evidence readback boundary, database-backed JobStore and answer
replay boundary. Authentication and object storage are separate adapters; the
runtime selects them explicitly rather than silently falling back.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from .contracts import (
    Block,
    Book,
    BookFormat,
    Chapter,
    Chunk,
    AnswerHistoryItem,
    AnswerRunStatus,
    ErrorCode,
    JobRecord,
    JobStage,
    JobStatus,
    JobType,
    ReadingPosition,
    ReadingProgress,
    EvidenceRef,
    SSEEnvelope,
    SSEEventType,
    ScopeContext,
    SourceLocator,
)
from .domain import ContractViolation
from .ports import BookRepositoryPort, JobStorePort, PublishTransactionPort


class PostgresAdapterError(RuntimeError):
    """Configuration or schema error in the production adapter."""


ConnectionFactory = Callable[..., Any]


class PostgresDatabase:
    """Own connections and transactions without leaking them to callers."""

    def __init__(self, dsn: str, *, connect_factory: ConnectionFactory | None = None) -> None:
        if not dsn:
            raise PostgresAdapterError("a non-empty database DSN is required")
        self.dsn = dsn
        self._connect_factory = connect_factory or psycopg.connect

    @contextmanager
    def connection(self) -> Iterator[Any]:
        connection = self._connect_factory(self.dsn, row_factory=dict_row)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self.connection() as connection:
            try:
                with connection.transaction():
                    yield connection
            except Exception:
                # The context manager rolls back before this point.  Do not
                # turn a database error into a successful partial operation.
                raise

    def apply_migration(self, migration_path: Path) -> None:
        sql = migration_path.read_text(encoding="utf-8")
        if "0002_stage05_postgres_bootstrap" in sql and "CREATE EXTENSION IF NOT EXISTS vector" not in sql:
            raise PostgresAdapterError("Stage 05 base migration must provision pgvector")
        if "INSERT INTO schema_migrations" not in sql or "ON CONFLICT (migration_id) DO NOTHING" not in sql:
            raise PostgresAdapterError("Stage 05 migration must record an idempotent migration ID")
        with self.transaction() as connection:
            connection.execute(sql)


def _scope_values(scope: ScopeContext) -> tuple[UUID, UUID, UUID, UUID]:
    return scope.user_id, scope.book_id, scope.book_version_id, scope.chapter_id  # type: ignore[return-value]


def _decode_json(value: Any, label: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise PostgresAdapterError(f"invalid {label} JSON") from exc
    return value


def _strict_uuid(value: Any, label: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PostgresAdapterError(f"invalid {label} UUID") from exc


def _position_model(value: Any) -> ReadingPosition:
    data = dict(_decode_json(value, "reading position"))
    for field in ("chapter_id", "block_id"):
        data[field] = _strict_uuid(data.get(field), f"position {field}")
    if isinstance(data.get("updated_at"), str):
        data["updated_at"] = datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))
    return ReadingPosition.model_validate(data)


_JOB_COLUMNS = """
    job_id, user_id, book_id, type, status, stage, attempts,
    lease_owner, lease_expires_at, heartbeat_at, idempotency_key,
    input_sha256, pipeline_version, checkpoint, cancel_requested,
    retryable, error_code, created_at, updated_at, row_version
"""


def _job_model(row: Any) -> JobRecord:
    row["type"] = JobType(row["type"])
    row["status"] = JobStatus(row["status"])
    row["stage"] = JobStage(row["stage"])
    if row.get("error_code") is not None:
        row["error_code"] = ErrorCode(row["error_code"])
    row["checkpoint"] = _decode_json(row["checkpoint"], "job checkpoint")
    return JobRecord.model_validate(row)


def _job_params(job: JobRecord) -> tuple[Any, ...]:
    return (
        job.job_id,
        job.user_id,
        job.book_id,
        job.type.value,
        job.status.value,
        job.stage.value,
        job.attempts,
        job.lease_owner,
        job.lease_expires_at,
        job.heartbeat_at,
        job.idempotency_key,
        job.input_sha256,
        job.pipeline_version,
        json.dumps(job.checkpoint),
        job.cancel_requested,
        job.retryable,
        job.error_code.value if job.error_code is not None else None,
        job.created_at,
        job.updated_at,
        job.row_version,
    )


class PostgresJobStore(JobStorePort):
    """Persistent job rows with explicit optimistic and lease boundaries."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def get(self, job_id: UUID) -> JobRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE job_id = %s",
                (job_id,),
            ).fetchone()
        return None if row is None else _job_model(row)

    def list_for_book(self, user_id: UUID, book_id: UUID) -> list[JobRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE user_id = %s AND book_id = %s ORDER BY created_at",
                (user_id, book_id),
            ).fetchall()
        return [_job_model(row) for row in rows]

    def create(self, job: JobRecord) -> JobRecord:
        with self.database.transaction() as connection:
            try:
                row = connection.execute(
                    f"""
                    INSERT INTO jobs ({_JOB_COLUMNS})
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                    RETURNING {_JOB_COLUMNS}
                    """,
                    _job_params(job),
                ).fetchone()
            except psycopg.errors.UniqueViolation as exc:
                raise ContractViolation(ErrorCode.CONFLICT, "job already exists") from exc
        return _job_model(row)

    def compare_and_swap(self, old: JobRecord, new: JobRecord) -> JobRecord:
        if (
            new.job_id != old.job_id
            or new.user_id != old.user_id
            or new.book_id != old.book_id
            or new.row_version != old.row_version + 1
        ):
            raise ContractViolation(ErrorCode.VERSION_CONFLICT, "job replacement is not a single version step")
        with self.database.transaction() as connection:
            row = connection.execute(
                f"""
                UPDATE jobs
                SET user_id = %s, book_id = %s, type = %s, status = %s, stage = %s,
                    attempts = %s, lease_owner = %s, lease_expires_at = %s,
                    heartbeat_at = %s, idempotency_key = %s, input_sha256 = %s,
                    pipeline_version = %s, checkpoint = %s::jsonb,
                    cancel_requested = %s, retryable = %s, error_code = %s,
                    created_at = %s, updated_at = %s, row_version = %s
                WHERE job_id = %s AND user_id = %s AND book_id = %s AND row_version = %s
                RETURNING {_JOB_COLUMNS}
                """,
                (
                    new.user_id, new.book_id, new.type.value, new.status.value, new.stage.value,
                    new.attempts, new.lease_owner, new.lease_expires_at, new.heartbeat_at,
                    new.idempotency_key, new.input_sha256, new.pipeline_version,
                    json.dumps(new.checkpoint), new.cancel_requested, new.retryable,
                    new.error_code.value if new.error_code is not None else None,
                    new.created_at, new.updated_at, new.row_version,
                    old.job_id, old.user_id, old.book_id, old.row_version,
                ),
            ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.VERSION_CONFLICT, "job version conflict")
        return _job_model(row)

    def save(self, job: JobRecord) -> JobRecord:
        current = self.get(job.job_id)
        if current is None:
            return self.create(job)
        return self.compare_and_swap(current, job)

    def claim(self, job_id: UUID, worker_id: str, *, lease_seconds: int = 30) -> JobRecord:
        if not worker_id or lease_seconds <= 0:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid worker lease")
        with self.database.transaction() as connection:
            row = connection.execute(
                f"""
                UPDATE jobs
                SET status = 'running', attempts = attempts + 1,
                    lease_owner = %s,
                    lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                    heartbeat_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP,
                    row_version = row_version + 1
                WHERE job_id = %s AND attempts < 3
                  AND ((status IN ('queued', 'retry_wait') AND lease_owner IS NULL)
                       OR (status = 'running' AND lease_expires_at <= CURRENT_TIMESTAMP))
                RETURNING {_JOB_COLUMNS}
                """,
                (worker_id, lease_seconds, job_id),
            ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.CONFLICT, "job is not claimable")
        return _job_model(row)

    def claim_next(self, worker_id: str, *, lease_seconds: int = 30) -> JobRecord | None:
        """Claim one queued/expired job with PostgreSQL row locking."""

        if not worker_id or lease_seconds <= 0:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid worker lease")
        with self.database.transaction() as connection:
            row = connection.execute(
                f"""
                UPDATE jobs
                SET status = 'running', attempts = attempts + 1,
                    lease_owner = %s,
                    lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                    heartbeat_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP,
                    row_version = row_version + 1
                WHERE job_id = (
                    SELECT job_id FROM jobs
                    WHERE attempts < 3
                      AND ((status IN ('queued', 'retry_wait') AND lease_owner IS NULL)
                           OR (status = 'running' AND lease_expires_at <= CURRENT_TIMESTAMP))
                    ORDER BY created_at, job_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING {_JOB_COLUMNS}
                """,
                (worker_id, lease_seconds),
            ).fetchone()
        return None if row is None else _job_model(row)


class PostgresBookRepository(BookRepositoryPort):
    """Owner- and version-scoped repository using parameterized SQL."""

    def __init__(self, database: PostgresDatabase, *, cursor_secret: bytes) -> None:
        if len(cursor_secret) < 16:
            raise PostgresAdapterError("cursor secret is too short")
        self.database = database
        self.cursor_secret = cursor_secret

    def create_book(self, book: Book) -> Book:
        now = book.created_at
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO users(user_id, created_at) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
                (book.user_id, now),
            )
            row = connection.execute(
                """
                INSERT INTO books(book_id, user_id, title, format, active_version_id,
                                  status, created_at, row_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING book_id, user_id, title, format, active_version_id,
                          status, created_at, row_version
                """,
                (
                    book.book_id, book.user_id, book.title, book.format.value,
                    book.active_version_id, book.status, book.created_at, book.row_version,
                ),
            ).fetchone()
        row["format"] = BookFormat(row["format"])
        return Book.model_validate(row)

    def persist_book_content(
        self,
        *,
        version: Any,
        chapters: Iterable[Chapter],
        blocks: Iterable[Block],
        chunks: Iterable[Chunk],
        chunk_texts: dict[UUID, str],
        progress: Iterable[ReadingProgress],
        embeddings: dict[UUID, list[float]] | None = None,
    ) -> None:
        """Write one parsed version and all dependent rows in one transaction."""

        chapters = list(chapters)
        blocks = list(blocks)
        chunks = list(chunks)
        progress = list(progress)
        embeddings = embeddings or {}
        for value in embeddings.values():
            from .retrieval import validate_embeddings
            validate_embeddings([value], expected_count=1)
        if not chapters or not blocks or not chunks:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "book content bundle is empty")
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO book_versions
                    (book_version_id, user_id, book_id, file_sha256, pipeline_version, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    version.book_version_id, version.user_id, version.book_id,
                    version.file_sha256, version.pipeline_version, version.status.value,
                    version.created_at,
                ),
            )
            for chapter in chapters:
                connection.execute(
                    """
                    INSERT INTO chapters
                        (chapter_id, user_id, book_id, book_version_id, ordinal, title, source_locator)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        chapter.chapter_id, chapter.user_id, chapter.book_id,
                        chapter.book_version_id, chapter.ordinal, chapter.title,
                        json.dumps(chapter.source_locator.model_dump(mode="json")),
                    ),
                )
            for block in blocks:
                connection.execute(
                    """
                    INSERT INTO blocks
                        (block_id, user_id, book_id, book_version_id, chapter_id, ordinal,
                         body, text_sha256, source_locator)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        block.block_id, block.user_id, block.book_id, block.book_version_id,
                        block.chapter_id, block.ordinal, block.text, block.text_sha256,
                        json.dumps(block.source_locator.model_dump(mode="json")),
                    ),
                )
            block_ids = {block.block_id for block in blocks}
            for chunk in chunks:
                if any(block_id not in block_ids for block_id in chunk.block_ids):
                    raise ContractViolation(ErrorCode.INVALID_INPUT, "chunk references a block outside the import bundle")
                text = chunk_texts.get(chunk.chunk_id)
                if not text:
                    raise ContractViolation(ErrorCode.INVALID_INPUT, "chunk text is missing")
                connection.execute(
                    """
                    INSERT INTO chunks
                        (chunk_id, user_id, book_id, book_version_id, chapter_id, chunk_index,
                         block_ids, text_sha256, token_count, embedding_model, chunker_version, search_text, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        chunk.chunk_id, chunk.user_id, chunk.book_id, chunk.book_version_id,
                        chunk.chapter_id, chunk.chunk_index,
                        json.dumps([str(value) for value in chunk.block_ids]), chunk.text_sha256,
                        chunk.token_count, chunk.embedding_model, chunk.chunker_version, text,
                        ("[" + ",".join(str(value) for value in embeddings[chunk.chunk_id]) + "]"
                         if chunk.chunk_id in embeddings else None),
                    ),
                )
            for item in progress:
                connection.execute(
                    """
                    INSERT INTO reading_progress
                        (user_id, book_id, book_version_id, chapter_id, last_chunk_index,
                         furthest_chunk_index, position, updated_at, device_id, row_version)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                    """,
                    (
                        item.user_id, item.book_id, item.book_version_id, item.chapter_id,
                        item.last_chunk_index, item.furthest_chunk_index,
                        json.dumps(item.position.model_dump(mode="json")), item.updated_at,
                        item.position.device_id, item.row_version,
                    ),
                )

    def get_book(self, scope: ScopeContext) -> Book | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT book_id, user_id, title, format, active_version_id, status,
                       created_at, row_version
                FROM books
                WHERE user_id = %s AND book_id = %s AND status = 'active'
                """,
                (scope.user_id, scope.book_id),
            ).fetchone()
        if row is None:
            return None
        row["format"] = BookFormat(row["format"])
        return Book.model_validate(row)

    def _cursor(self, scope: ScopeContext, collection: str, offset: int) -> str:
        payload = {
            "v": 1,
            "collection": collection,
            "user_id": str(scope.user_id),
            "book_id": str(scope.book_id),
            "book_version_id": str(scope.book_version_id),
            "chapter_id": str(scope.chapter_id) if scope.chapter_id else None,
            "offset": offset,
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        signature = hmac.new(self.cursor_secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{encoded}.{signature}"

    def _offset(self, scope: ScopeContext, collection: str, cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            encoded, signature = cursor.split(".", 1)
            expected = hmac.new(self.cursor_secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("cursor signature")
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode((encoded + padding).encode("ascii")))
            expected_scope = {
                "collection": collection,
                "user_id": str(scope.user_id),
                "book_id": str(scope.book_id),
                "book_version_id": str(scope.book_version_id),
                "chapter_id": str(scope.chapter_id) if scope.chapter_id else None,
            }
            if any(payload.get(key) != value for key, value in expected_scope.items()):
                raise ValueError("cursor scope")
            offset = payload["offset"]
            if payload.get("v") != 1 or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ValueError("cursor offset")
            return offset
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "cursor is invalid") from exc

    def list_chapters(self, scope: ScopeContext, cursor: str | None, limit: int) -> tuple[list[Chapter], str | None]:
        if not 1 <= limit <= 100:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "limit is outside the allowed range")
        offset = self._offset(scope, "chapters", cursor)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT c.chapter_id, c.book_id, c.user_id, c.book_version_id, c.ordinal, c.title, c.source_locator
                FROM chapters AS c
                JOIN book_versions AS v ON v.user_id = c.user_id
                    AND v.book_id = c.book_id AND v.book_version_id = c.book_version_id
                JOIN books AS b ON b.user_id = c.user_id AND b.book_id = c.book_id
                WHERE c.user_id = %s AND c.book_id = %s AND c.book_version_id = %s
                  AND v.status = 'ready' AND b.status = 'active' AND b.active_version_id = c.book_version_id
                ORDER BY ordinal
                LIMIT %s OFFSET %s
                """,
                (scope.user_id, scope.book_id, scope.book_version_id, limit, offset),
            ).fetchall()
        items = [Chapter.model_validate(row) for row in rows]
        return items, self._cursor(scope, "chapters", offset + limit) if len(items) == limit else None

    def list_blocks(self, scope: ScopeContext, chapter_id: UUID, cursor: str | None, limit: int) -> tuple[list[Block], str | None]:
        if not 1 <= limit <= 100:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "limit is outside the allowed range")
        if scope.chapter_id is not None and chapter_id != scope.chapter_id:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        scoped = scope.model_copy(update={"chapter_id": chapter_id})
        offset = self._offset(scoped, "blocks", cursor)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT bl.block_id, bl.chapter_id, bl.book_id, bl.user_id, bl.book_version_id, bl.ordinal,
                       bl.body AS text, bl.text_sha256, bl.source_locator
                FROM blocks AS bl
                JOIN book_versions AS v ON v.user_id = bl.user_id
                    AND v.book_id = bl.book_id AND v.book_version_id = bl.book_version_id
                JOIN books AS b ON b.user_id = bl.user_id AND b.book_id = bl.book_id
                WHERE bl.user_id = %s AND bl.book_id = %s AND bl.book_version_id = %s AND bl.chapter_id = %s
                  AND v.status = 'ready' AND b.status = 'active' AND b.active_version_id = bl.book_version_id
                ORDER BY ordinal
                LIMIT %s OFFSET %s
                """,
                (*_scope_values(scoped), limit, offset),
            ).fetchall()
        items = [Block.model_validate(row) for row in rows]
        return items, self._cursor(scoped, "blocks", offset + limit) if len(items) == limit else None

    def read_chunks(self, scope: ScopeContext, chunk_ids: list[UUID]) -> list[Chunk]:
        if not chunk_ids or len(chunk_ids) > 100:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "chunk_ids are outside the allowed range")
        placeholders = ", ".join(["%s"] * len(chunk_ids))
        scope_clause = "c.user_id = %s AND c.book_id = %s AND c.book_version_id = %s"
        scope_params: list[Any] = [scope.user_id, scope.book_id, scope.book_version_id]
        if scope.chapter_id is not None:
            scope_clause += " AND c.chapter_id = %s"
            scope_params.append(scope.chapter_id)
        values = (*scope_params, *chunk_ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT c.chunk_id, c.chapter_id, c.book_id, c.user_id, c.book_version_id, c.chunk_index,
                       c.block_ids, c.text_sha256, c.token_count, c.embedding_model, c.chunker_version
                FROM chunks AS c
                JOIN book_versions AS v ON v.user_id = c.user_id
                    AND v.book_id = c.book_id AND v.book_version_id = c.book_version_id
                JOIN books AS b ON b.user_id = c.user_id AND b.book_id = c.book_id
                WHERE {scope_clause} AND v.status = 'ready'
                  AND b.status = 'active' AND b.active_version_id = c.book_version_id
                  AND c.chunk_id IN ({placeholders})
                ORDER BY chunk_index
                """,
                values,
            ).fetchall()
        result = []
        for row in rows:
            row["block_ids"] = [
                _strict_uuid(item, "chunk block_id")
                for item in _decode_json(row["block_ids"], "chunk block_ids")
            ]
            result.append(Chunk.model_validate(row))
        return result

    def search_embeddings(self, scope: ScopeContext, query_embedding: list[float], *, top_k: int = 40,
                          chapter_ids: Iterable[UUID] | None = None) -> list[tuple[float, UUID]]:
        """Scoped pgvector cosine search; callers still validate local scope."""
        from .retrieval import validate_embeddings
        validate_embeddings([query_embedding], expected_count=1)
        if not 1 <= top_k <= 100:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "top_k is outside the allowed range")
        allowed = list(chapter_ids or [])
        clause = "c.user_id = %s AND c.book_id = %s AND c.book_version_id = %s AND c.embedding IS NOT NULL"
        params: list[Any] = [scope.user_id, scope.book_id, scope.book_version_id]
        if scope.chapter_id is not None:
            clause += " AND c.chapter_id = %s"
            params.append(scope.chapter_id)
        if allowed:
            clause += " AND c.chapter_id = ANY(%s)"
            params.append(allowed)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT c.chunk_id, 1 - (c.embedding <=> %s::vector) AS score FROM chunks c "
                f"JOIN book_versions v ON v.user_id = c.user_id AND v.book_id = c.book_id "
                f"AND v.book_version_id = c.book_version_id JOIN books b ON b.user_id = c.user_id "
                f"AND b.book_id = c.book_id WHERE {clause} AND v.status = 'ready' "
                f"AND b.status = 'active' AND b.active_version_id = c.book_version_id "
                "ORDER BY c.embedding <=> %s::vector, c.chunk_id LIMIT %s",
                (str(query_embedding), *params, str(query_embedding), top_k),
            ).fetchall()
        return [(float(row["score"]), _strict_uuid(row["chunk_id"], "embedding chunk_id"))
                for row in rows if float(row["score"]) > 0]

    def read_blocks_by_ids(self, scope: ScopeContext, block_ids: list[UUID]) -> list[Block]:
        if not block_ids or len(block_ids) > 100:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "block_ids are outside the allowed range")
        placeholders = ", ".join(["%s"] * len(block_ids))
        scope_clause = "bl.user_id = %s AND bl.book_id = %s AND bl.book_version_id = %s"
        scope_params: list[Any] = [scope.user_id, scope.book_id, scope.book_version_id]
        if scope.chapter_id is not None:
            scope_clause += " AND bl.chapter_id = %s"
            scope_params.append(scope.chapter_id)
        values = (*scope_params, *block_ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT bl.block_id, bl.chapter_id, bl.book_id, bl.user_id, bl.book_version_id, bl.ordinal,
                       bl.body AS text, bl.text_sha256, bl.source_locator
                FROM blocks AS bl
                JOIN book_versions AS v ON v.user_id = bl.user_id
                    AND v.book_id = bl.book_id AND v.book_version_id = bl.book_version_id
                JOIN books AS b ON b.user_id = bl.user_id AND b.book_id = bl.book_id
                WHERE {scope_clause} AND v.status = 'ready'
                  AND b.status = 'active' AND b.active_version_id = bl.book_version_id
                  AND bl.block_id IN ({placeholders})
                ORDER BY ordinal
                """,
                values,
            ).fetchall()
        return [Block.model_validate(row) for row in rows]

    def put_evidence(self, scope: ScopeContext, evidence: EvidenceRef) -> EvidenceRef:
        """Persist a server-created evidence ref under its full composite scope."""

        if (
            evidence.user_id != scope.user_id
            or evidence.book_id != scope.book_id
            or evidence.book_version_id != scope.book_version_id
            or (scope.chapter_id is not None and evidence.chapter_id != scope.chapter_id)
        ):
            raise ContractViolation(ErrorCode.FORBIDDEN, "evidence is outside the authorized scope")
        expected_hash = hashlib.sha256(evidence.quote.encode("utf-8")).hexdigest()
        if expected_hash != evidence.content_sha256:
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "evidence content hash does not match quote")
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                INSERT INTO evidence_refs
                    (evidence_id, user_id, book_id, book_version_id, chapter_id, chunk_id,
                     chunk_index, block_ids, quote, content_sha256, source_locator)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
                ON CONFLICT (evidence_id) DO UPDATE SET
                    block_ids = EXCLUDED.block_ids,
                    quote = EXCLUDED.quote,
                    content_sha256 = EXCLUDED.content_sha256,
                    source_locator = EXCLUDED.source_locator
                WHERE evidence_refs.user_id = EXCLUDED.user_id
                  AND evidence_refs.book_id = EXCLUDED.book_id
                  AND evidence_refs.book_version_id = EXCLUDED.book_version_id
                  AND evidence_refs.chapter_id = EXCLUDED.chapter_id
                  AND evidence_refs.chunk_id = EXCLUDED.chunk_id
                RETURNING evidence_id, user_id, book_id, book_version_id, chapter_id, chunk_id,
                          chunk_index, block_ids, quote, content_sha256, source_locator
                """,
                (
                    evidence.evidence_id,
                    evidence.user_id,
                    evidence.book_id,
                    evidence.book_version_id,
                    evidence.chapter_id,
                    evidence.chunk_id,
                    evidence.chunk_index,
                    json.dumps([str(value) for value in evidence.block_ids]),
                    evidence.quote,
                    evidence.content_sha256,
                    json.dumps(evidence.source_locator.model_dump(mode="json")),
                ),
            ).fetchone()
            if row is None:
                raise ContractViolation(ErrorCode.CONFLICT, "evidence identity conflicts with an existing row")
        return self._evidence_model(row)

    @staticmethod
    def _evidence_model(row: Any) -> EvidenceRef:
        row["block_ids"] = [
            _strict_uuid(item, "evidence block_id")
            for item in _decode_json(row["block_ids"], "evidence block_ids")
        ]
        row["source_locator"] = SourceLocator.model_validate(
            _decode_json(row["source_locator"], "evidence source_locator")
        )
        return EvidenceRef.model_validate(row)

    def read_evidence(self, scope: ScopeContext, evidence_id: UUID) -> EvidenceRef | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT e.evidence_id, e.user_id, e.book_id, e.book_version_id, e.chapter_id,
                       e.chunk_id, e.chunk_index, e.block_ids, e.quote, e.content_sha256,
                       e.source_locator
                FROM evidence_refs AS e
                JOIN book_versions AS v ON v.user_id = e.user_id
                    AND v.book_id = e.book_id AND v.book_version_id = e.book_version_id
                JOIN books AS b ON b.user_id = e.user_id AND b.book_id = e.book_id
                WHERE e.evidence_id = %s
                  AND e.user_id = %s AND e.book_id = %s AND e.book_version_id = %s
                  AND (%s IS NULL OR e.chapter_id = %s)
                  AND v.status = 'ready' AND b.status = 'active'
                  AND b.active_version_id = e.book_version_id
                """,
                (
                    evidence_id,
                    scope.user_id,
                    scope.book_id,
                    scope.book_version_id,
                    scope.chapter_id,
                    scope.chapter_id,
                ),
            ).fetchone()
        return None if row is None else self._evidence_model(row)

    def get_progress(self, scope: ScopeContext) -> ReadingProgress | None:
        if scope.chapter_id is None:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "chapter scope is required for progress")
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT user_id, book_id, book_version_id, chapter_id, last_chunk_index,
                       furthest_chunk_index, position, updated_at, row_version
                FROM reading_progress
                WHERE user_id = %s AND book_id = %s AND book_version_id = %s AND chapter_id = %s
                """,
                _scope_values(scope),
            ).fetchone()
        if row is None:
            return None
        row["position"] = _position_model(row.pop("position"))
        return ReadingProgress.model_validate(row)

    def put_progress(self, scope: ScopeContext, progress: ReadingProgress, expected_row_version: int) -> ReadingProgress:
        if scope.chapter_id != progress.chapter_id:
            raise ContractViolation(ErrorCode.FORBIDDEN, "progress is outside the authorized chapter")
        if expected_row_version < 0:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "row version is invalid")
        now = datetime.now(timezone.utc)
        position = progress.position.model_copy(update={"updated_at": now})
        position_json = position.model_dump(mode="json")
        with self.database.transaction() as connection:
            if expected_row_version == 0:
                row = connection.execute(
                    """
                    INSERT INTO reading_progress
                        (user_id, book_id, book_version_id, chapter_id, last_chunk_index,
                         furthest_chunk_index, position, updated_at, device_id, row_version)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, 1)
                    ON CONFLICT (user_id, book_id, book_version_id, chapter_id) DO NOTHING
                    RETURNING user_id, book_id, book_version_id, chapter_id, last_chunk_index,
                              furthest_chunk_index, position, updated_at, row_version
                    """,
                    (*_scope_values(scope), progress.last_chunk_index, progress.furthest_chunk_index,
                     json.dumps(position_json), now, position.device_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    UPDATE reading_progress
                    SET last_chunk_index = %s,
                        furthest_chunk_index = GREATEST(furthest_chunk_index, %s),
                        position = %s::jsonb,
                        updated_at = %s,
                        device_id = %s,
                        row_version = row_version + 1
                    WHERE user_id = %s AND book_id = %s AND book_version_id = %s AND chapter_id = %s
                      AND row_version = %s
                    RETURNING user_id, book_id, book_version_id, chapter_id, last_chunk_index,
                              furthest_chunk_index, position, updated_at, row_version
                    """,
                    (progress.last_chunk_index, progress.furthest_chunk_index, json.dumps(position_json), now,
                     position.device_id, *_scope_values(scope), expected_row_version),
                ).fetchone()
            if row is None:
                raise ContractViolation(ErrorCode.VERSION_CONFLICT, "reading progress version conflict")
        row["position"] = _position_model(row.pop("position"))
        return ReadingProgress.model_validate(row)


class PostgresPublishTransaction(PublishTransactionPort):
    """Commit verified version publication and job completion together."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def publish_verified_version(self, *, scope: ScopeContext, job: JobRecord) -> JobRecord:
        version_text = job.checkpoint.get("book_version_id")
        if not isinstance(version_text, str):
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "publish checkpoint lacks book_version_id")
        try:
            version_id = UUID(version_text)
        except ValueError as exc:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "publish checkpoint version is invalid") from exc
        if job.user_id != scope.user_id or job.book_id != scope.book_id:
            raise ContractViolation(ErrorCode.FORBIDDEN, "job is outside the authorized scope")
        now = datetime.now(timezone.utc)
        with self.database.transaction() as connection:
            version = connection.execute(
                """
                UPDATE book_versions
                SET status = 'ready', published_at = %s
                WHERE book_version_id = %s AND user_id = %s AND book_id = %s AND status = 'building'
                RETURNING book_version_id
                """,
                (now, version_id, scope.user_id, scope.book_id),
            ).fetchone()
            if version is None:
                raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "version is not publishable")
            book = connection.execute(
                """
                UPDATE books
                SET active_version_id = %s, row_version = row_version + 1
                WHERE user_id = %s AND book_id = %s AND status = 'active'
                RETURNING book_id
                """,
                (version_id, scope.user_id, scope.book_id),
            ).fetchone()
            if book is None:
                raise ContractViolation(ErrorCode.BOOK_NOT_READY, "book is not publishable")
            row = connection.execute(
                """
                UPDATE jobs
                SET status = 'succeeded', stage = 'publish', lease_owner = NULL,
                    lease_expires_at = NULL, heartbeat_at = NULL, updated_at = %s,
                    row_version = row_version + 1
                WHERE job_id = %s AND user_id = %s AND book_id = %s
                  AND status = 'running' AND cancel_requested = false
                RETURNING job_id, user_id, book_id, type, status, stage, attempts,
                          lease_owner, lease_expires_at, heartbeat_at, idempotency_key,
                          input_sha256, pipeline_version, checkpoint, cancel_requested,
                          retryable, error_code, created_at, updated_at, row_version
                """,
                (now, job.job_id, scope.user_id, scope.book_id),
            ).fetchone()
        if row is None:
                raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "job is not publishable")
        row["checkpoint"] = _decode_json(row["checkpoint"], "job checkpoint")
        row["type"] = JobType(row["type"])
        row["status"] = JobStatus(row["status"])
        row["stage"] = JobStage(row["stage"])
        return JobRecord.model_validate(row)


class PostgresAnswerStore:
    """Persist answer history and replayable SSE envelopes by exact scope."""

    def __init__(self, database: PostgresDatabase, *, repository: PostgresBookRepository) -> None:
        self.database = database
        self.repository = repository

    def create_run(
        self,
        *,
        scope: ScopeContext,
        run_id: UUID,
        conversation_id: UUID,
        question: str,
        created_at: datetime,
    ) -> AnswerHistoryItem:
        with self.database.transaction() as connection:
            try:
                row = connection.execute(
                    """
                    INSERT INTO answer_runs
                        (run_id, user_id, book_id, book_version_id, chapter_id, trace_id,
                         request_id, conversation_id, question, status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'accepted', %s)
                    RETURNING run_id, trace_id, question, answer_text, status,
                              evidence_ids, created_at, finished_at, chapter_id
                    """,
                    (
                        run_id, scope.user_id, scope.book_id, scope.book_version_id,
                        scope.chapter_id, scope.trace_id, scope.request_id, conversation_id,
                        question, created_at,
                    ),
                ).fetchone()
            except psycopg.errors.UniqueViolation as exc:
                raise ContractViolation(ErrorCode.CONFLICT, "answer run already exists") from exc
        return self._history_model(row, scope)

    def append_event(self, scope: ScopeContext, event: SSEEnvelope) -> SSEEnvelope:
        if event.trace_id != scope.trace_id:
            raise ContractViolation(ErrorCode.FORBIDDEN, "answer event is outside the authorized trace")
        if (event.seq == 1) != (event.type is SSEEventType.ACCEPTED):
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "answer events must start with accepted")
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                INSERT INTO answer_events(run_id, seq, trace_id, event_type, payload, emitted_at)
                SELECT run_id, %s, %s, %s, %s::jsonb, %s
                FROM answer_runs
                WHERE run_id = %s AND user_id = %s AND book_id = %s AND book_version_id = %s
                  AND (chapter_id IS NOT DISTINCT FROM %s)
                  AND (%s = 1 OR EXISTS (
                      SELECT 1 FROM answer_events AS previous
                      WHERE previous.run_id = answer_runs.run_id AND previous.seq = %s
                  ))
                ON CONFLICT (run_id, seq) DO UPDATE SET
                    trace_id = EXCLUDED.trace_id,
                    event_type = EXCLUDED.event_type,
                    payload = EXCLUDED.payload,
                    emitted_at = EXCLUDED.emitted_at
                WHERE answer_events.trace_id = EXCLUDED.trace_id
                  AND answer_events.event_type = EXCLUDED.event_type
                  AND answer_events.payload = EXCLUDED.payload
                  AND answer_events.emitted_at = EXCLUDED.emitted_at
                RETURNING run_id, seq, trace_id, event_type, payload, emitted_at
                """,
                (
                    event.seq, event.trace_id, event.type.value, json.dumps(event.payload),
                    event.emitted_at, event.run_id, scope.user_id, scope.book_id,
                    scope.book_version_id, scope.chapter_id, event.seq, event.seq - 1,
                ),
            ).fetchone()
            if row is None:
                raise ContractViolation(ErrorCode.CONFLICT, "answer event conflicts with scope or existing sequence")
            next_status = (
                AnswerRunStatus.ACCEPTED.value
                if event.type is SSEEventType.ACCEPTED
                else AnswerRunStatus.RUNNING.value
            )
            connection.execute(
                """
                UPDATE answer_runs
                SET status = %s
                WHERE run_id = %s AND user_id = %s AND book_id = %s AND book_version_id = %s
                  AND (chapter_id IS NOT DISTINCT FROM %s)
                """,
                (next_status, event.run_id, scope.user_id, scope.book_id, scope.book_version_id, scope.chapter_id),
            )
        return SSEEnvelope.model_validate(row | {"type": SSEEventType(row.pop("event_type"))})

    def read_events(self, scope: ScopeContext, run_id: UUID, *, after_seq: int = 0) -> list[SSEEnvelope]:
        if after_seq < 0:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "event sequence is invalid")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT e.run_id, e.seq, e.trace_id, e.event_type, e.payload, e.emitted_at
                FROM answer_events AS e
                JOIN answer_runs AS r ON r.run_id = e.run_id
                WHERE e.run_id = %s AND r.user_id = %s AND r.book_id = %s
                  AND r.book_version_id = %s AND (r.chapter_id IS NOT DISTINCT FROM %s)
                  AND e.seq > %s
                ORDER BY e.seq
                """,
                (run_id, scope.user_id, scope.book_id, scope.book_version_id, scope.chapter_id, after_seq),
            ).fetchall()
        events: list[SSEEnvelope] = []
        for row in rows:
            event_type = SSEEventType(row.pop("event_type"))
            events.append(SSEEnvelope.model_validate(row | {"type": event_type}))
        return events

    def save_terminal(
        self,
        *,
        scope: ScopeContext,
        run_id: UUID,
        status: AnswerRunStatus,
        answer_text: str,
        evidence_ids: list[UUID],
        conversation_id: UUID,
        finished_at: datetime,
        model_name: str | None,
        tool_call_count: int,
        error_code: ErrorCode | None = None,
    ) -> AnswerHistoryItem:
        if status not in {AnswerRunStatus.COMPLETED, AnswerRunStatus.FAILED, AnswerRunStatus.CANCELLED}:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "answer status is not terminal")
        if status is AnswerRunStatus.COMPLETED and not evidence_ids:
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "completed answer requires evidence")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "answer evidence must be unique")
        if evidence_ids:
            with self.database.connection() as connection:
                row = connection.execute(
                    """
                    SELECT count(*) AS count
                    FROM evidence_refs AS e
                    JOIN book_versions AS v ON v.user_id = e.user_id
                        AND v.book_id = e.book_id AND v.book_version_id = e.book_version_id
                    JOIN books AS b ON b.user_id = e.user_id AND b.book_id = e.book_id
                    WHERE e.evidence_id = ANY(%s)
                      AND e.user_id = %s AND e.book_id = %s AND e.book_version_id = %s
                      AND (%s IS NULL OR e.chapter_id = %s)
                      AND v.status = 'ready' AND b.status = 'active'
                      AND b.active_version_id = e.book_version_id
                    """,
                    (
                        evidence_ids, scope.user_id, scope.book_id, scope.book_version_id,
                        scope.chapter_id, scope.chapter_id,
                    ),
                ).fetchone()
            if row["count"] != len(evidence_ids):
                raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "answer evidence is outside the authorized scope")
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                UPDATE answer_runs
                SET status = %s, answer_text = %s, evidence_ids = %s::jsonb,
                    conversation_id = %s, finished_at = %s, model_name = %s,
                    tool_call_count = %s, error_code = %s
                WHERE run_id = %s AND user_id = %s AND book_id = %s AND book_version_id = %s
                  AND (chapter_id IS NOT DISTINCT FROM %s)
                  AND status IN ('accepted', 'running')
                RETURNING run_id, trace_id, question, answer_text, status,
                          evidence_ids, created_at, finished_at, chapter_id
                """,
                (
                    status.value, answer_text, json.dumps([str(value) for value in evidence_ids]),
                    conversation_id, finished_at, model_name, tool_call_count,
                    error_code.value if error_code is not None else None,
                    run_id, scope.user_id, scope.book_id, scope.book_version_id, scope.chapter_id,
                ),
            ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.CONFLICT, "answer run is missing or already terminal")
        return self._history_model(row, scope)

    def list_history(self, scope: ScopeContext, *, limit: int = 100) -> list[AnswerHistoryItem]:
        if not 1 <= limit <= 100:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "history limit is outside the allowed range")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT run_id, trace_id, question, answer_text, status,
                       evidence_ids, created_at, finished_at, chapter_id
                FROM answer_runs
                WHERE user_id = %s AND book_id = %s AND book_version_id = %s
                  AND (chapter_id IS NOT DISTINCT FROM %s)
                ORDER BY created_at, run_id
                LIMIT %s
                """,
                (scope.user_id, scope.book_id, scope.book_version_id, scope.chapter_id, limit),
            ).fetchall()
        return [self._history_model(row, scope) for row in rows]

    def read_history(self, scope: ScopeContext, run_id: UUID) -> AnswerHistoryItem | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT run_id, trace_id, question, answer_text, status,
                       evidence_ids, created_at, finished_at, chapter_id
                FROM answer_runs
                WHERE run_id = %s AND user_id = %s AND book_id = %s AND book_version_id = %s
                  AND (chapter_id IS NOT DISTINCT FROM %s)
                """,
                (run_id, scope.user_id, scope.book_id, scope.book_version_id, scope.chapter_id),
            ).fetchone()
        return None if row is None else self._history_model(row, scope)

    def _history_model(self, row: Any, scope: ScopeContext) -> AnswerHistoryItem:
        evidence_ids = [
            _strict_uuid(item, "answer evidence_id")
            for item in _decode_json(row["evidence_ids"], "answer evidence_ids")
        ]
        history_scope = scope.model_copy(update={"chapter_id": row.get("chapter_id")})
        evidence = [
            value
            for evidence_id in evidence_ids
            if (value := self.repository.read_evidence(history_scope, evidence_id)) is not None
        ]
        return AnswerHistoryItem(
            run_id=row["run_id"], trace_id=row["trace_id"], question=row["question"],
            answer=row["answer_text"], status=AnswerRunStatus(row["status"]), evidence=evidence,
            created_at=row["created_at"], finished_at=row["finished_at"],
        )
