from __future__ import annotations

import pytest

from datetime import datetime, timezone
from uuid import uuid4

from reading_agent.contracts import JobRecord, JobStage, JobStatus, JobType, ScopeContext
from reading_agent.domain import AnswerEventLedger
from reading_agent.storage_boundary import PersistenceBoundary
from reading_agent.auth import _password_hash, _password_matches
from reading_agent.worker import JobController, JobWorker


class SpySnapshotStore:
    def __init__(self) -> None:
        self.saved = 0
        self.loaded = 0

    def save(self, services: object) -> None:
        self.saved += 1

    def load(self, services: object) -> bool:
        self.loaded += 1
        return True


class SpyBookRepository:
    def __init__(self) -> None:
        self.progress = object()
        self.calls: list[str] = []

    def get_book(self, scope: object) -> object:
        self.calls.append("get_book")
        return object()

    def create_book(self, book: object) -> object:
        self.calls.append("create_book")
        return book

    def get_progress(self, scope: object) -> object:
        self.calls.append("get_progress")
        return self.progress

    def put_progress(self, scope: object, progress: object, expected_row_version: int) -> object:
        self.calls.append(f"put_progress:{expected_row_version}")
        return progress


def test_preview_boundary_uses_one_snapshot_store_and_reports_no_cutover() -> None:
    snapshot = SpySnapshotStore()
    boundary = PersistenceBoundary(
        snapshot_store=snapshot,
        book_repository=object(),
        job_store=object(),
        answer_store=object(),
    )

    boundary.save(object())
    assert boundary.load(object()) is True
    assert snapshot.saved == 1 and snapshot.loaded == 1
    assert boundary.normalized_adapters_ready is True
    assert boundary.status == {
        "mode": "preview-snapshot-bridge",
        "normalized_adapters_ready": True,
        "runtime_cutover": False,
    }

    with pytest.raises(RuntimeError, match="cutover is not ready"):
        boundary.require_runtime_cutover()


def test_boundary_is_normalized_only_when_explicitly_enabled() -> None:
    boundary = PersistenceBoundary(
        snapshot_store=SpySnapshotStore(),
        book_repository=object(),
        job_store=object(),
        answer_store=object(),
        runtime_cutover_enabled=True,
    )

    assert boundary.status["mode"] == "normalized-repositories"
    assert boundary.status["runtime_cutover"] is True
    boundary.require_runtime_cutover()


def test_normalized_book_progress_path_delegates_only_after_explicit_cutover() -> None:
    repository = SpyBookRepository()
    boundary = PersistenceBoundary(
        snapshot_store=SpySnapshotStore(),
        book_repository=repository,  # type: ignore[arg-type]
        job_store=object(),
        answer_store=object(),
        runtime_cutover_enabled=True,
    )

    assert boundary.get_book(object()) is not None
    assert boundary.get_progress(object()) is repository.progress
    marker = object()
    assert boundary.put_progress(object(), marker, 3) is marker
    assert repository.calls == ["get_book", "get_progress", "put_progress:3"]


def test_book_creation_can_be_delegated_before_job_creation() -> None:
    repository = SpyBookRepository()
    boundary = PersistenceBoundary(
        snapshot_store=SpySnapshotStore(), book_repository=repository,
        job_store=object(), answer_store=object(),
    )
    marker = object()
    assert boundary.create_book(marker) is marker
    assert repository.calls == ["create_book"]


def test_persistent_auth_password_hash_round_trip_and_wrong_password_rejects() -> None:
    encoded = _password_hash("reading-demo")
    assert _password_matches("reading-demo", encoded)
    assert not _password_matches("wrong", encoded)


def _job() -> JobRecord:
    now = datetime.now(timezone.utc)
    return JobRecord(
        job_id=uuid4(), user_id=uuid4(), book_id=uuid4(), type=JobType.IMPORT_BOOK,
        idempotency_key=f"test-{uuid4()}", input_sha256="0" * 64,
        pipeline_version="test", created_at=now, updated_at=now,
    )


def test_worker_claims_one_job_and_handler_owns_terminal_transition() -> None:
    controller = JobController()
    job = controller.add(_job())
    seen: list[str] = []

    def handle(claimed: JobRecord, owner: JobController) -> None:
        seen.append(claimed.lease_owner or "")
        owner.checkpoint(claimed.job_id, "worker-test", stage=JobStage.VERIFY, values={"verified": True})
        owner.checkpoint(claimed.job_id, "worker-test", stage=JobStage.PUBLISH, values={"verified": True})
        owner.succeed(claimed.job_id, "worker-test")

    result = JobWorker(controller, worker_id="worker-test", handler=handle).run_once()
    assert result is not None and result.status is JobStatus.SUCCEEDED
    assert seen == ["worker-test"]
    assert JobWorker(controller, worker_id="worker-test", handler=handle).run_once() is None


def test_answer_ledger_sink_receives_ordered_events_before_replay() -> None:
    stored = []
    ledger = AnswerEventLedger(
        run_id=uuid4(), trace_id=uuid4(), scope=ScopeContext(
            session_id=uuid4(), user_id=uuid4(), book_id=uuid4(),
            book_version_id=uuid4(), request_id=uuid4(), trace_id=uuid4(),
        ), evidence_required=False, event_sink=stored.append,
    )
    ledger.accepted()
    ledger.answer_delta("hello")
    ledger.complete(answer_id=uuid4(), conversation_id=uuid4())
    assert [item.seq for item in stored] == [1, 2, 3]
    assert [item.seq for item in ledger.events] == [1, 2, 3]
