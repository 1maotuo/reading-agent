"""Bounded SQLite persistence for the local Stage 05 preview.

This is a development adapter, not the production PostgreSQL repository.  It
stores one versioned, checksummed JSON snapshot in a SQLite transaction so the
existing in-memory contract objects can be restored without pickle or a new
runtime dependency.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel

from .api import ApiServices, _AnswerRecord
from .book_memory import BookMemoryStore
from .contracts import (
    AnswerRunStatus,
    Block,
    Book,
    BookVersion,
    Chapter,
    Chunk,
    ErrorCode,
    EvidenceBundle,
    EvidenceRef,
    HighlightAnchor,
    JobRecord,
    JobStatus,
    ReadingProgress,
    ScopeContext,
    Tombstone,
    ToolName,
    ContextSource,
    DialogueGoal,
    DialogueRelation,
    DialogueRoute,
    IntentFrame,
)
from .domain import AnswerEventLedger, issue_verified_evidence_token, sha256_text
from .retrieval import validate_embeddings, EMBEDDING_DIMENSION


SCHEMA_VERSION = 1
TModel = TypeVar("TModel", bound=BaseModel)


class StateStoreError(RuntimeError):
    """The persisted preview state is corrupt or incompatible."""


def _model_json(value: BaseModel) -> dict[str, Any]:
    return value.model_dump(mode="json")


def _restore_model(model: type[TModel], value: Any) -> TModel:
    try:
        return model.model_validate_json(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except Exception as exc:  # pragma: no cover - exact Pydantic message is not part of the contract
        raise StateStoreError(f"invalid persisted {model.__name__}") from exc


def _restore_scope(value: Any) -> ScopeContext:
    if not isinstance(value, dict):
        raise StateStoreError("invalid persisted ScopeContext")
    converted = dict(value)
    # A restored history record receives a fresh internal session identity;
    # authentication tokens and original session IDs are never durable.
    converted["session_id"] = uuid4()
    for field in ("user_id", "book_id", "book_version_id", "request_id", "trace_id"):
        converted[field] = _uuid(converted.get(field), f"scope {field}")
    if converted.get("chapter_id") is not None:
        converted["chapter_id"] = _uuid(converted["chapter_id"], "scope chapter_id")
    try:
        return ScopeContext.model_validate(converted)
    except Exception as exc:
        raise StateStoreError("invalid persisted ScopeContext") from exc


def _uuid(value: Any, label: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise StateStoreError(f"invalid persisted {label}") from exc


class SQLitePreviewStateStore:
    """Persist and restore the Stage 05 development state as one transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            connection = self._connect()
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS preview_state (
                        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                        schema_version INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            raise StateStoreError("preview state database is unreadable") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _snapshot(services: ApiServices) -> dict[str, Any]:
        books = services.books
        terminal_answers = [
            record
            for record in services.answers.records.values()
            if record.ledger.status
            in {AnswerRunStatus.COMPLETED, AnswerRunStatus.FAILED, AnswerRunStatus.CANCELLED}
        ]
        terminal_jobs = [
            job
            for job in getattr(services.jobs.store, "jobs", {}).values()
            if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
        ]
        durable_result_ids = {
            *(record.ledger.run_id for record in terminal_answers),
            *(job.job_id for job in terminal_jobs),
            *books.highlights.keys(),
        }
        return {
            "cursor_secret": base64.b64encode(books.cursor_secret).decode("ascii"),
            "books": [_model_json(value) for value in books.books.values()],
            "versions": [_model_json(value) for value in books.versions.values()],
            "chapters": [_model_json(value) for value in books.chapters.values()],
            "blocks": [_model_json(value) for value in books.blocks.values()],
            "chunks": [_model_json(value) for value in books.chunks.values()],
            "chunk_texts": {str(key): value for key, value in books.chunk_texts.items()},
            "chunk_embeddings": {str(key): value for key, value in books.chunk_embeddings.items()},
            "source_paths": {str(key): value for key, value in books.source_paths.items()},
            "progress": [_model_json(value) for value in books.progress.values()],
            "highlights": [_model_json(value) for value in books.highlights.values()],
            "evidence": [_model_json(value) for value in books.evidence.values()],
            "tombstones": [_model_json(value) for value in books.tombstones.values()],
            "deletion_steps": {str(key): value for key, value in books.deletion_steps.items()},
            "jobs": [_model_json(value) for value in terminal_jobs],
            "answers": [
                {
                    # Session/CSRF data and the verbose SSE execution ledger are
                    # deliberately not durable.  Keep only the ownership and
                    # trace summary required to recover user-visible history.
                    "scope": {
                        "user_id": str(record.scope.user_id),
                        "book_id": str(record.scope.book_id),
                        "book_version_id": str(record.scope.book_version_id),
                        "chapter_id": str(record.scope.chapter_id) if record.scope.chapter_id else None,
                        "furthest_chunk_index": record.scope.furthest_chunk_index,
                        "request_id": str(record.scope.request_id),
                        "trace_id": str(record.scope.trace_id),
                    },
                    "run_id": str(record.ledger.run_id),
                    "status": record.ledger.status.value,
                    "question": record.question,
                    "created_at": record.created_at.isoformat(),
                    "answer_text": record.answer_text,
                    "evidence_ids": [str(value) for value in record.evidence_ids],
                    "conversation_id": str(record.conversation_id),
                    "finished_at": record.finished_at.isoformat() if record.finished_at else None,
                    "model_name": record.model_name,
                    "tool_call_count": record.tool_call_count,
                    "error_code": record.error_code.value if record.error_code else None,
                    "intent": record.intent.model_dump(mode="json") if record.intent is not None else None,
                    "trace_details": record.trace_details,
                }
                for record in terminal_answers
            ],
            "idempotency": [
                {
                    "user_id": str(key[0]),
                    "operation": key[1],
                    "key": key[2],
                    "body_hash": value[0],
                    "book_id": str(value[1]),
                    "job_id": str(value[2]),
                }
                for key, value in services.idempotency.items()
            ],
            "mutation_idempotency": [
                {
                    "user_id": str(key[0]),
                    "operation": key[1],
                    "key": key[2],
                    "body_hash": value[0],
                    "result_id": str(value[1]),
                }
                for key, value in services.mutation_idempotency.items()
                if value[1] in durable_result_ids
            ],
            "companion": services.companion.dump(),
            "memory": services.memory.dump(),
            "book_memory": services.book_memory.dump(),
        }

    def save(self, services: ApiServices) -> None:
        with self._lock:
            # Snapshot while holding the same lock as the transaction.  A
            # delayed older request therefore cannot overwrite a newer state
            # with bytes captured before it entered the write queue.
            snapshot = self._snapshot(services)
            payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            try:
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        INSERT INTO preview_state(singleton_id, schema_version, payload_json, payload_sha256)
                        VALUES(1, ?, ?, ?)
                        ON CONFLICT(singleton_id) DO UPDATE SET
                            schema_version=excluded.schema_version,
                            payload_json=excluded.payload_json,
                            payload_sha256=excluded.payload_sha256,
                            updated_at=CURRENT_TIMESTAMP
                        """,
                        (SCHEMA_VERSION, payload, digest),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
            except sqlite3.DatabaseError as exc:
                raise StateStoreError("failed to persist preview state") from exc

    def load(self, services: ApiServices) -> bool:
        with self._lock:
            try:
                connection = self._connect()
                try:
                    row = connection.execute(
                        "SELECT schema_version, payload_json, payload_sha256 FROM preview_state WHERE singleton_id=1"
                    ).fetchone()
                finally:
                    connection.close()
            except sqlite3.DatabaseError as exc:
                raise StateStoreError("failed to read preview state") from exc
        if row is None:
            return False
        version, payload, expected_digest = row
        if version != SCHEMA_VERSION:
            raise StateStoreError("preview state schema version is incompatible")
        actual_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if actual_digest != expected_digest:
            raise StateStoreError("preview state checksum mismatch")
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise StateStoreError("preview state JSON is invalid") from exc
        if not isinstance(raw, dict):
            raise StateStoreError("preview state root must be an object")

        # Restore into temporary collections first.  The live service is only
        # replaced after all model and relationship checks succeed.
        restored_books = {
            value.book_id: value for value in (_restore_model(Book, item) for item in raw.get("books", []))
        }
        restored_versions = {
            value.book_version_id: value
            for value in (_restore_model(BookVersion, item) for item in raw.get("versions", []))
        }
        restored_chapters = {
            value.chapter_id: value
            for value in (_restore_model(Chapter, item) for item in raw.get("chapters", []))
        }
        restored_blocks = {
            value.block_id: value for value in (_restore_model(Block, item) for item in raw.get("blocks", []))
        }
        restored_chunks = {
            value.chunk_id: value for value in (_restore_model(Chunk, item) for item in raw.get("chunks", []))
        }
        restored_progress = [
            _restore_model(ReadingProgress, item) for item in raw.get("progress", [])
        ]
        restored_highlights = {
            value.highlight_id: value
            for value in (_restore_model(HighlightAnchor, item) for item in raw.get("highlights", []))
        }
        restored_evidence = {
            value.evidence_id: value
            for value in (_restore_model(EvidenceRef, item) for item in raw.get("evidence", []))
        }
        restored_tombstones = {
            value.book_id: value
            for value in (_restore_model(Tombstone, item) for item in raw.get("tombstones", []))
        }
        restored_jobs = {
            value.job_id: value for value in (_restore_model(JobRecord, item) for item in raw.get("jobs", []))
        }
        chunk_texts = {
            _uuid(key, "chunk text id"): value for key, value in raw.get("chunk_texts", {}).items()
        }
        chunk_embeddings: dict[UUID, list[float]] = {}
        for key, value in (raw.get("chunk_embeddings", {}) or {}).items():
            chunk_id = _uuid(key, "chunk embedding id")
            try:
                chunk_embeddings[chunk_id] = validate_embeddings([value], expected_count=1)[0]
            except (TypeError, ValueError) as exc:
                raise StateStoreError("invalid persisted chunk embedding") from exc
        source_paths = {
            _uuid(key, "source path book id"): str(value)
            for key, value in raw.get("source_paths", {}).items()
        }
        if not all(isinstance(value, str) for value in chunk_texts.values()):
            raise StateStoreError("invalid persisted chunk text")

        self._validate_relationships(
            restored_books,
            restored_versions,
            restored_chapters,
            restored_blocks,
            restored_chunks,
            chunk_texts,
            restored_progress,
            restored_highlights,
            restored_evidence,
            restored_jobs,
        )
        if any(key not in restored_chunks for key in chunk_embeddings):
            raise StateStoreError("persisted chunk embedding references missing chunk")

        answers: dict[UUID, _AnswerRecord] = {}
        for item in raw.get("answers", []):
            if not isinstance(item, dict):
                raise StateStoreError("invalid persisted answer")
            scope = _restore_scope(item.get("scope"))
            run_id = _uuid(item.get("run_id"), "answer run id")
            try:
                status = AnswerRunStatus(item.get("status"))
            except (TypeError, ValueError) as exc:
                raise StateStoreError("persisted answer status is invalid") from exc
            if status not in {
                AnswerRunStatus.COMPLETED,
                AnswerRunStatus.FAILED,
                AnswerRunStatus.CANCELLED,
            }:
                raise StateStoreError("persisted answer is not terminal")
            ledger = AnswerEventLedger(
                run_id=run_id,
                trace_id=scope.trace_id,
                request_id=scope.request_id,
                scope=scope,
                evidence_required=(
                    item.get("intent", {}).get("route", DialogueRoute.BOOK_DIALOGUE.value)
                    == DialogueRoute.BOOK_DIALOGUE.value
                    if isinstance(item.get("intent"), dict)
                    else True
                ),
            )
            evidence_ids = [_uuid(value, "answer evidence id") for value in item.get("evidence_ids", [])]
            if any(value not in restored_evidence for value in evidence_ids):
                raise StateStoreError("persisted answer references missing evidence")
            ledger.accepted()
            if status is AnswerRunStatus.COMPLETED:
                intent_value = item.get("intent")
                if isinstance(intent_value, dict):
                    try:
                        intent = IntentFrame.model_validate_json(json.dumps(intent_value, ensure_ascii=False))
                    except Exception as exc:
                        raise StateStoreError("persisted answer intent is invalid") from exc
                else:
                    intent = IntentFrame(
                        route=DialogueRoute.BOOK_DIALOGUE,
                        relation=DialogueRelation.NEW,
                        goals=[DialogueGoal.EXPLAIN],
                        context_sources=[ContextSource.NONE],
                    )
                if not evidence_ids and intent.route is DialogueRoute.BOOK_DIALOGUE:
                    raise StateStoreError("completed persisted answer has no evidence")
                if evidence_ids:
                    call_id = uuid5(run_id, "restored-search-book")
                    ledger.start_tool(call_id, ToolName.SEARCH_BOOK)
                    ledger.finish_tool(call_id, ToolName.SEARCH_BOOK, "succeeded", result_ref="restored-history")
                    bundle = EvidenceBundle(refs=[restored_evidence[value] for value in evidence_ids])
                    proof = issue_verified_evidence_token(
                        run_id=run_id,
                        scope=scope,
                        bundle=bundle,
                        oracle=lambda trusted_scope, evidence_id: _read_restored_evidence(
                            trusted_scope, evidence_id, restored_evidence
                        ),
                    )
                    ledger.evidence(proof)
                answer_text = str(item.get("answer_text", ""))
                if not answer_text:
                    raise StateStoreError("completed persisted answer text is empty")
                ledger.answer_delta(answer_text)
                conversation_id = _uuid(item.get("conversation_id") or uuid5(run_id, "conversation"), "conversation id")
                ledger.complete(
                    answer_id=uuid5(run_id, "restored-answer"),
                    conversation_id=conversation_id,
                    evidence_ids=evidence_ids,
                )
            else:
                intent_value = item.get("intent")
                if isinstance(intent_value, dict):
                    try:
                        intent = IntentFrame.model_validate_json(json.dumps(intent_value, ensure_ascii=False))
                    except Exception as exc:
                        raise StateStoreError("persisted answer intent is invalid") from exc
                else:
                    intent = IntentFrame(
                        route=DialogueRoute.BOOK_DIALOGUE,
                        relation=DialogueRelation.NEW,
                        goals=[DialogueGoal.EXPLAIN],
                        context_sources=[ContextSource.NONE],
                    )
                answer_text = str(item.get("answer_text", ""))
                conversation_id = _uuid(item.get("conversation_id") or uuid5(run_id, "conversation"), "conversation id")
                if status is AnswerRunStatus.CANCELLED:
                    ledger.cancel()
                else:
                    ledger.fail(
                        ErrorCode(item["error_code"]) if item.get("error_code") else ErrorCode.INTERNAL_ERROR,
                        "历史回答在执行时失败",
                    )
            record = _AnswerRecord(
                scope=scope,
                question=str(item.get("question", "")),
                ledger=ledger,
                created_at=_restore_datetime(item.get("created_at"), "answer created_at"),
                answer_text=answer_text,
                evidence_ids=evidence_ids,
                conversation_id=conversation_id,
                finished_at=_restore_datetime(item.get("finished_at"), "answer finished_at")
                if item.get("finished_at")
                else None,
                model_name=str(item["model_name"]) if item.get("model_name") is not None else None,
                tool_call_count=int(item.get("tool_call_count", 0)),
                error_code=ErrorCode(item["error_code"]) if item.get("error_code") else None,
                intent=intent,
                trace_details=dict(item.get("trace_details") or {}),
            )
            if not record.question or scope.book_id not in restored_books:
                raise StateStoreError("persisted answer scope is invalid")
            answers[ledger.run_id] = record

        try:
            cursor_secret = base64.b64decode(raw.get("cursor_secret", ""), validate=True)
        except Exception as exc:
            raise StateStoreError("persisted cursor secret is invalid") from exc
        if len(cursor_secret) < 16:
            raise StateStoreError("persisted cursor secret is too short")

        services.books.books = restored_books
        services.books.versions = restored_versions
        services.books.chapters = restored_chapters
        services.books.blocks = restored_blocks
        services.books.chunks = restored_chunks
        services.books.chunk_texts = chunk_texts
        services.books.chunk_embeddings = chunk_embeddings
        services.books.block_chunk_indexes = {
            block_id: chunk.chunk_index for chunk in restored_chunks.values() for block_id in chunk.block_ids
        }
        services.books.evidence = restored_evidence
        services.books.source_paths = source_paths
        services.books.progress = {
            (value.user_id, value.book_version_id, value.chapter_id): value for value in restored_progress
        }
        services.books.highlights = restored_highlights
        services.books.cursor_secret = cursor_secret
        services.books.tombstones = restored_tombstones
        services.books.deletion_steps = {
            _uuid(key, "deletion book id"): list(value)
            for key, value in raw.get("deletion_steps", {}).items()
        }
        if hasattr(services.jobs.store, "jobs"):
            services.jobs.store.jobs = restored_jobs
        services.answers.records = answers
        services.idempotency = {
            (_uuid(item["user_id"], "idempotency user id"), str(item["operation"]), str(item["key"])): (
                str(item["body_hash"]),
                _uuid(item["book_id"], "idempotency book id"),
                _uuid(item["job_id"], "idempotency job id"),
            )
            for item in raw.get("idempotency", [])
        }
        services.mutation_idempotency = {
            (_uuid(item["user_id"], "mutation user id"), str(item["operation"]), str(item["key"])): (
                str(item["body_hash"]),
                _uuid(item["result_id"], "mutation result id"),
            )
            for item in raw.get("mutation_idempotency", [])
        }
        try:
            services.companion.restore(raw.get("companion"))
        except (TypeError, ValueError) as exc:
            raise StateStoreError("persisted companion state is invalid") from exc
        try:
            services.memory.restore(raw.get("memory"))
        except (TypeError, ValueError) as exc:
            raise StateStoreError("persisted memory state is invalid") from exc
        for value in services.memory.records.values():
            book = restored_books.get(value.book_id)
            if book is None or book.user_id != value.user_id:
                raise StateStoreError("persisted memory is outside its book")
        restored_book_memory = BookMemoryStore()
        try:
            restored_book_memory.restore(raw.get("book_memory"))
        except (TypeError, ValueError) as exc:
            raise StateStoreError("persisted book memory state is invalid") from exc
        book_memory_items = (
            *restored_book_memory.concepts.values(),
            *restored_book_memory.episodes.values(),
            *restored_book_memory.signals.values(),
        )
        for value in book_memory_items:
            book = restored_books.get(value.book_id)
            version = restored_versions.get(value.book_version_id)
            if (
                book is None
                or version is None
                or book.user_id != value.user_id
                or version.user_id != value.user_id
                or version.book_id != value.book_id
            ):
                raise StateStoreError("persisted book memory is outside its book version")
        services.book_memory = restored_book_memory
        return True

    @staticmethod
    def _validate_relationships(
        books: dict[UUID, Book],
        versions: dict[UUID, BookVersion],
        chapters: dict[UUID, Chapter],
        blocks: dict[UUID, Block],
        chunks: dict[UUID, Chunk],
        chunk_texts: dict[UUID, str],
        progress: list[ReadingProgress],
        highlights: dict[UUID, HighlightAnchor],
        evidence: dict[UUID, EvidenceRef],
        jobs: dict[UUID, JobRecord],
    ) -> None:
        for version in versions.values():
            book = books.get(version.book_id)
            if book is None or book.user_id != version.user_id:
                raise StateStoreError("persisted version is outside its book")
        for book in books.values():
            if book.active_version_id is not None and book.active_version_id not in versions:
                raise StateStoreError("persisted book has a missing active version")
        for chapter in chapters.values():
            version = versions.get(chapter.book_version_id)
            if version is None or (chapter.user_id, chapter.book_id) != (version.user_id, version.book_id):
                raise StateStoreError("persisted chapter is outside its version")
        for block in blocks.values():
            chapter = chapters.get(block.chapter_id)
            if chapter is None or (block.user_id, block.book_id, block.book_version_id) != (
                chapter.user_id,
                chapter.book_id,
                chapter.book_version_id,
            ):
                raise StateStoreError("persisted block is outside its chapter")
        if set(chunk_texts) != set(chunks):
            raise StateStoreError("persisted chunk text set is incomplete")
        for chunk in chunks.values():
            chapter = chapters.get(chunk.chapter_id)
            if chapter is None or any(block_id not in blocks for block_id in chunk.block_ids):
                raise StateStoreError("persisted chunk references missing content")
            if (chunk.user_id, chunk.book_id, chunk.book_version_id) != (
                chapter.user_id,
                chapter.book_id,
                chapter.book_version_id,
            ):
                raise StateStoreError("persisted chunk is outside its chapter")
            if sha256_text(chunk_texts[chunk.chunk_id]) != chunk.text_sha256:
                raise StateStoreError("persisted chunk text hash mismatch")
        for value in progress:
            chapter = chapters.get(value.chapter_id)
            if chapter is None or (value.user_id, value.book_id, value.book_version_id) != (
                chapter.user_id,
                chapter.book_id,
                chapter.book_version_id,
            ):
                raise StateStoreError("persisted progress is outside its chapter")
        for value in highlights.values():
            chapter = chapters.get(value.chapter_id)
            if chapter is None or value.start.block_id not in blocks or value.end.block_id not in blocks:
                raise StateStoreError("persisted highlight references missing content")
        for value in evidence.values():
            chunk = chunks.get(value.chunk_id)
            if chunk is None or value.block_ids != chunk.block_ids:
                raise StateStoreError("persisted evidence references missing content")
        for value in jobs.values():
            if value.book_id not in books:
                raise StateStoreError("persisted job references a missing book")


def _restore_datetime(value: Any, label: str):
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise StateStoreError(f"invalid persisted {label}") from exc


def _read_restored_evidence(
    scope: ScopeContext,
    evidence_id: UUID,
    evidence: dict[UUID, EvidenceRef],
) -> EvidenceRef | None:
    value = evidence.get(evidence_id)
    if value is None:
        return None
    if (
        value.user_id != scope.user_id
        or value.book_id != scope.book_id
        or value.book_version_id != scope.book_version_id
        or (scope.chapter_id is not None and value.chapter_id != scope.chapter_id)
    ):
        return None
    return value


class PostgresPreviewStateStore:
    """PostgreSQL-backed restart store for the current Stage 05 runtime.

    The vertical slice still exposes the frozen in-memory contract objects to
    the API.  PostgreSQL is nevertheless the authoritative durable row; the
    existing SQLite decoder is used only in a private temporary file to reuse
    its fail-closed model and relationship validation until the API is moved
    to the normalized repositories.
    """

    def __init__(self, database: Any, *, migration_path: Path | None = None) -> None:
        self.database = database
        self._lock = threading.RLock()
        path = migration_path or Path(__file__).resolve().parents[2] / "db" / "migrations" / "0005_stage05_preview_state.sql"
        self.database.apply_migration(path)

    @staticmethod
    def _serialized(services: ApiServices) -> tuple[str, str]:
        snapshot = SQLitePreviewStateStore._snapshot(services)
        payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def save(self, services: ApiServices) -> None:
        with self._lock:
            payload, digest = self._serialized(services)
            try:
                with self.database.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO stage05_preview_state(singleton_id, schema_version, payload_json, payload_sha256)
                        VALUES (1, %s, %s, %s)
                        ON CONFLICT (singleton_id) DO UPDATE SET
                            schema_version = EXCLUDED.schema_version,
                            payload_json = EXCLUDED.payload_json,
                            payload_sha256 = EXCLUDED.payload_sha256,
                            updated_at = now()
                        """,
                        (SCHEMA_VERSION, payload, digest),
                    )
            except Exception as exc:
                raise StateStoreError("failed to persist PostgreSQL preview state") from exc

    def load(self, services: ApiServices) -> bool:
        with self._lock:
            try:
                with self.database.connection() as connection:
                    row = connection.execute(
                        "SELECT schema_version, payload_json, payload_sha256 FROM stage05_preview_state WHERE singleton_id = 1"
                    ).fetchone()
            except Exception as exc:
                raise StateStoreError("failed to read PostgreSQL preview state") from exc
        if row is None:
            return False
        version = row["schema_version"] if isinstance(row, dict) else row[0]
        payload = row["payload_json"] if isinstance(row, dict) else row[1]
        expected_digest = row["payload_sha256"] if isinstance(row, dict) else row[2]
        # Feed the already-tested strict decoder without creating a persistent
        # SQLite database or changing the PostgreSQL source of truth.
        with tempfile.TemporaryDirectory(prefix="reading-agent-preview-codec-") as temporary:
            path = Path(temporary) / "decoder.sqlite3"
            decoder = SQLitePreviewStateStore(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "INSERT INTO preview_state(singleton_id, schema_version, payload_json, payload_sha256) VALUES(1, ?, ?, ?)",
                    (version, payload, expected_digest),
                )
                connection.commit()
            finally:
                connection.close()
            return decoder.load(services)
