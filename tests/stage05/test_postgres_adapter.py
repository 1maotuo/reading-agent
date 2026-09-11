"""Integration checks for the narrow Stage 05 PostgreSQL adapter boundary."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from reading_agent.contracts import (
    Block,
    Book,
    BookFormat,
    Chapter,
    Chunk,
    ErrorCode,
    EvidenceRef,
    AnswerRunStatus,
    JobRecord,
    JobStage,
    JobStatus,
    JobType,
    ReadingPosition,
    ReadingProgress,
    ScopeContext,
    SourceLocator,
    SSEEnvelope,
    SSEEventType,
)
from reading_agent.domain import ContractViolation, sha256_text
from reading_agent.postgres_adapter import (
    PostgresBookRepository,
    PostgresAnswerStore,
    PostgresDatabase,
    PostgresJobStore,
    PostgresPublishTransaction,
)


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db" / "migrations" / "0002_stage05_postgres_bootstrap.sql"
EVIDENCE_MIGRATION = ROOT / "db" / "migrations" / "0003_stage05_evidence_refs.sql"
ANSWER_MIGRATION = ROOT / "db" / "migrations" / "0004_stage05_answer_runs.sql"
UTC = timezone.utc


@pytest.fixture(scope="module")
def database() -> PostgresDatabase:
    dsn = os.getenv("READING_AGENT_STAGE05_DSN")
    if not dsn:
        pytest.skip("set READING_AGENT_STAGE05_DSN for the PostgreSQL integration boundary")
    database = PostgresDatabase(dsn)
    database.apply_migration(MIGRATION)
    database.apply_migration(MIGRATION)
    database.apply_migration(EVIDENCE_MIGRATION)
    database.apply_migration(EVIDENCE_MIGRATION)
    database.apply_migration(ANSWER_MIGRATION)
    database.apply_migration(ANSWER_MIGRATION)
    return database


def _fixture(database: PostgresDatabase) -> tuple[PostgresBookRepository, ScopeContext, JobRecord, Book, BookVersion]:
    from reading_agent.contracts import BookVersion, BookVersionStatus

    now = datetime.now(UTC)
    user_id, book_id, version_id, chapter_id = uuid4(), uuid4(), uuid4(), uuid4()
    block_id, chunk_id, job_id = uuid4(), uuid4(), uuid4()
    title = "Stage 05 PostgreSQL adapter fixture"
    block_text = "真实 PostgreSQL 事务与范围隔离探测"
    book = Book(
        book_id=book_id, user_id=user_id, title=title, format=BookFormat.TXT,
        created_at=now, row_version=1,
    )
    version = BookVersion(
        book_version_id=version_id, book_id=book_id, user_id=user_id,
        file_sha256=sha256_text("fixture-file"), pipeline_version="stage05-test",
        status=BookVersionStatus.BUILDING, created_at=now,
    )
    chapter = Chapter(
        chapter_id=chapter_id, book_id=book_id, user_id=user_id, book_version_id=version_id,
        ordinal=0, title="事务", source_locator=SourceLocator(kind="synthetic", value="stage05"),
    )
    block = Block(
        block_id=block_id, chapter_id=chapter_id, book_id=book_id, user_id=user_id,
        book_version_id=version_id, ordinal=0, text=block_text,
        text_sha256=sha256_text(block_text), source_locator=chapter.source_locator,
    )
    chunk = Chunk(
        chunk_id=chunk_id, chapter_id=chapter_id, book_id=book_id, user_id=user_id,
        book_version_id=version_id, chunk_index=0, block_ids=[block_id],
        text_sha256=sha256_text(block_text), token_count=8, embedding_model="test",
        chunker_version="test",
    )
    job = JobRecord(
        job_id=job_id, user_id=user_id, book_id=book_id, type=JobType.IMPORT_BOOK,
        status=JobStatus.RUNNING, stage=JobStage.PUBLISH, attempts=1,
        lease_owner="stage05-test", lease_expires_at=now + timedelta(minutes=5),
        heartbeat_at=now, idempotency_key=f"stage05-{job_id}", input_sha256=sha256_text("input"),
        pipeline_version="stage05-test", checkpoint={"book_version_id": str(version_id)},
        created_at=now, updated_at=now,
    )
    with database.transaction() as connection:
        connection.execute("INSERT INTO users(user_id, created_at) VALUES (%s, %s)", (user_id, now))
        connection.execute(
            "INSERT INTO books(book_id, user_id, title, format, status, created_at, row_version) VALUES (%s, %s, %s, %s, 'active', %s, 1)",
            (book_id, user_id, title, book.format.value, now),
        )
        connection.execute(
            "INSERT INTO book_versions(book_version_id, user_id, book_id, file_sha256, pipeline_version, status, created_at) VALUES (%s, %s, %s, %s, %s, 'building', %s)",
            (version_id, user_id, book_id, version.file_sha256, version.pipeline_version, now),
        )
        connection.execute(
            "INSERT INTO chapters(chapter_id, user_id, book_id, book_version_id, ordinal, title, source_locator) VALUES (%s, %s, %s, %s, 0, %s, %s::jsonb)",
            (chapter_id, user_id, book_id, version_id, chapter.title, '{"kind":"synthetic","value":"stage05"}'),
        )
        connection.execute(
            "INSERT INTO blocks(block_id, user_id, book_id, book_version_id, chapter_id, ordinal, body, text_sha256, source_locator) VALUES (%s, %s, %s, %s, %s, 0, %s, %s, %s::jsonb)",
            (block_id, user_id, book_id, version_id, chapter_id, block_text, block.text_sha256, '{"kind":"synthetic","value":"stage05"}'),
        )
        connection.execute(
            "INSERT INTO chunks(chunk_id, user_id, book_id, book_version_id, chapter_id, chunk_index, block_ids, text_sha256, token_count, embedding_model, chunker_version, search_text) VALUES (%s, %s, %s, %s, %s, 0, %s::jsonb, %s, 8, 'test', 'test', %s)",
            (chunk_id, user_id, book_id, version_id, chapter_id, f'["{block_id}"]', chunk.text_sha256, block_text),
        )
        connection.execute(
            "INSERT INTO jobs(job_id, user_id, book_id, type, status, stage, attempts, lease_owner, lease_expires_at, heartbeat_at, idempotency_key, input_sha256, pipeline_version, checkpoint, created_at, updated_at, row_version) VALUES (%s, %s, %s, 'import_book', 'running', 'publish', 1, 'stage05-test', %s, %s, %s, %s, 'stage05-test', %s::jsonb, %s, %s, 1)",
            (job_id, user_id, book_id, now + timedelta(minutes=5), now, job.idempotency_key, job.input_sha256, '{"book_version_id":"' + str(version_id) + '"}', now, now),
        )
    scope = ScopeContext(
        session_id=uuid4(), user_id=user_id, book_id=book_id, book_version_id=version_id,
        chapter_id=chapter_id, furthest_chunk_index=0, request_id=uuid4(), trace_id=uuid4(),
    )
    return PostgresBookRepository(database, cursor_secret=b"stage05-test-cursor-secret"), scope, job, book, version


def test_migration_real_pgvector_and_scoped_reads(database: PostgresDatabase) -> None:
    repository, scope, job, _, _ = _fixture(database)
    PostgresPublishTransaction(database).publish_verified_version(scope=scope, job=job)
    with database.connection() as connection:
        assert connection.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
        assert connection.execute("SELECT 1 FROM schema_migrations WHERE migration_id = '0002_stage05_postgres_bootstrap'").fetchone()
        assert connection.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'stage05_chunks_vector_idx'").fetchone()
    book = repository.get_book(scope)
    assert book is not None and book.user_id == scope.user_id
    chapters, cursor = repository.list_chapters(scope, None, 10)
    assert len(chapters) == 1 and cursor is None
    blocks, _ = repository.list_blocks(scope, scope.chapter_id, None, 10)  # type: ignore[arg-type]
    assert len(blocks) == 1 and blocks[0].text == "真实 PostgreSQL 事务与范围隔离探测"
    chunks = repository.read_chunks(scope, [scope.book_version_id])
    assert chunks == []  # wrong chunk ID must not become a scope bypass
    with database.connection() as connection:
        chunk_id = connection.execute(
            "SELECT chunk_id FROM chunks WHERE user_id = %s AND book_id = %s AND book_version_id = %s",
            (scope.user_id, scope.book_id, scope.book_version_id),
        ).fetchone()["chunk_id"]
    assert len(repository.read_chunks(scope, [chunk_id])) == 1
    other_scope = scope.model_copy(update={"user_id": uuid4()})
    assert repository.get_book(other_scope) is None
    assert repository.read_chunks(other_scope, [chunk_id]) == []
    evidence = EvidenceRef(
        evidence_id=uuid4(), user_id=scope.user_id, book_id=scope.book_id,
        book_version_id=scope.book_version_id, chapter_id=scope.chapter_id,
        chunk_id=chunk_id, chunk_index=0, block_ids=[blocks[0].block_id],
        quote=blocks[0].text, content_sha256=sha256_text(blocks[0].text),
        source_locator=blocks[0].source_locator,
    )
    assert repository.put_evidence(scope, evidence).evidence_id == evidence.evidence_id
    assert repository.read_evidence(scope, evidence.evidence_id) == evidence
    assert repository.read_evidence(other_scope, evidence.evidence_id) is None


def test_progress_optimistic_lock_and_publish_transaction(database: PostgresDatabase) -> None:
    repository, scope, job, _, version = _fixture(database)
    now = datetime.now(UTC)
    position = ReadingPosition(chapter_id=scope.chapter_id, block_id=uuid4(), block_offset=0, updated_at=now, device_id="desktop", row_version=1)  # type: ignore[arg-type]
    progress = ReadingProgress(
        book_id=scope.book_id, user_id=scope.user_id, book_version_id=scope.book_version_id,
        chapter_id=scope.chapter_id, last_chunk_index=0, furthest_chunk_index=0,
        position=position, updated_at=now, row_version=1,  # type: ignore[arg-type]
    )
    saved = repository.put_progress(scope, progress, 0)
    assert saved.row_version == 1
    advanced = saved.model_copy(update={"furthest_chunk_index": 1})
    advanced = ReadingProgress.model_validate(advanced.model_dump())
    updated = repository.put_progress(scope, advanced, saved.row_version)
    assert updated.row_version == 2 and updated.furthest_chunk_index == 1
    with pytest.raises(ContractViolation) as conflict:
        repository.put_progress(scope, advanced, saved.row_version)
    assert conflict.value.code.value == "version_conflict"
    committed = PostgresPublishTransaction(database).publish_verified_version(scope=scope, job=job)
    assert committed.status is JobStatus.SUCCEEDED
    book = repository.get_book(scope)
    assert book is not None and book.active_version_id == version.book_version_id


def test_transaction_rolls_back_on_exception(database: PostgresDatabase) -> None:
    marker = uuid4()
    with pytest.raises(RuntimeError):
        with database.transaction() as connection:
            connection.execute("INSERT INTO users(user_id, created_at) VALUES (%s, CURRENT_TIMESTAMP)", (marker,))
            raise RuntimeError("intentional rollback probe")
    with database.connection() as connection:
        assert connection.execute("SELECT 1 FROM users WHERE user_id = %s", (marker,)).fetchone() is None


def test_postgres_job_store_claim_and_compare_and_swap(database: PostgresDatabase) -> None:
    _, scope, template, _, _ = _fixture(database)
    job = JobRecord.model_validate(
        template.model_copy(
            update={
                "job_id": uuid4(),
                "status": JobStatus.QUEUED,
                "stage": JobStage.VALIDATE,
                "attempts": 0,
                "lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
                "idempotency_key": f"stage05-job-store-{uuid4()}",
                "row_version": 1,
            }
        ).model_dump()
    )
    store = PostgresJobStore(database)
    assert store.create(job).job_id == job.job_id
    claimed = store.claim(job.job_id, "worker-a", lease_seconds=60)
    assert claimed.status is JobStatus.RUNNING and claimed.attempts == 1
    advanced = JobRecord.model_validate(
        claimed.model_copy(update={"stage": JobStage.PARSE, "row_version": claimed.row_version + 1}).model_dump()
    )
    assert store.compare_and_swap(claimed, advanced).stage is JobStage.PARSE
    with pytest.raises(ContractViolation) as conflict:
        store.compare_and_swap(claimed, advanced)
    assert conflict.value.code is ErrorCode.VERSION_CONFLICT
    assert store.get(job.job_id).user_id == scope.user_id  # type: ignore[union-attr]
    queued_next = JobRecord.model_validate(
        template.model_copy(
            update={
                "job_id": uuid4(),
                "status": JobStatus.QUEUED,
                "stage": JobStage.VALIDATE,
                "attempts": 0,
                "lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
                "idempotency_key": f"stage05-job-store-next-{uuid4()}",
                "checkpoint": {},
                "row_version": 1,
            }
        ).model_dump()
    )
    store.create(queued_next)
    claimed_next = store.claim_next("worker-next", lease_seconds=60)
    assert claimed_next is not None
    assert claimed_next.status is JobStatus.RUNNING and 1 <= claimed_next.attempts <= 3


def test_postgres_answer_store_history_and_event_replay(database: PostgresDatabase) -> None:
    repository, scope, job, _, _ = _fixture(database)
    PostgresPublishTransaction(database).publish_verified_version(scope=scope, job=job)
    with database.connection() as connection:
        row = connection.execute(
            """
            SELECT c.chunk_id, c.block_ids, c.search_text
            FROM chunks AS c
            WHERE c.user_id = %s AND c.book_id = %s AND c.book_version_id = %s
            LIMIT 1
            """,
            (scope.user_id, scope.book_id, scope.book_version_id),
        ).fetchone()
    assert row is not None
    import json

    block_ids = row["block_ids"] if isinstance(row["block_ids"], list) else json.loads(row["block_ids"])
    block_id = UUID(block_ids[0])
    evidence = EvidenceRef(
        evidence_id=uuid4(), user_id=scope.user_id, book_id=scope.book_id,
        book_version_id=scope.book_version_id, chapter_id=scope.chapter_id,
        chunk_id=row["chunk_id"], chunk_index=0, block_ids=[block_id],
        quote=row["search_text"], content_sha256=sha256_text(row["search_text"]),
        source_locator=SourceLocator(kind="synthetic", value="answer-store-test"),
    )
    repository.put_evidence(scope, evidence)
    store = PostgresAnswerStore(database, repository=repository)
    run_id, conversation_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    created = store.create_run(
        scope=scope, run_id=run_id, conversation_id=conversation_id,
        question="持久化问答测试", created_at=now,
    )
    assert created.status is AnswerRunStatus.ACCEPTED
    accepted = SSEEnvelope(
        run_id=run_id, trace_id=scope.trace_id, seq=1, emitted_at=now,
        type=SSEEventType.ACCEPTED, payload={},
    )
    delta = SSEEnvelope(
        run_id=run_id, trace_id=scope.trace_id, seq=2, emitted_at=now,
        type=SSEEventType.ANSWER_DELTA, payload={"text_delta": "基于原文"},
    )
    store.append_event(scope, accepted)
    store.append_event(scope, delta)
    assert [event.seq for event in store.read_events(scope, run_id)] == [1, 2]
    saved = store.save_terminal(
        scope=scope, run_id=run_id, status=AnswerRunStatus.COMPLETED,
        answer_text="基于原文 [E1]", evidence_ids=[evidence.evidence_id],
        conversation_id=conversation_id, finished_at=now,
        model_name="deterministic-test", tool_call_count=1,
    )
    assert saved.status is AnswerRunStatus.COMPLETED and saved.evidence == [evidence]
    assert store.read_history(scope, run_id) == saved
    assert store.list_history(scope, limit=10) == [saved]
    other_scope = scope.model_copy(update={"user_id": uuid4()})
    assert store.read_history(other_scope, run_id) is None
    assert store.read_events(other_scope, run_id) == []
