"""Bounded in-memory job state machine used by the Stage 04 contract tests.

The real worker will persist the same fields in Postgres.  This draft keeps the
state transitions and safety boundaries executable without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from uuid import UUID

from .contracts import ErrorCode, JobRecord, JobStage, JobStatus, JobType
from .domain import ContractViolation, utc_now


_ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    # RUNNING is entered only by claim(), which allocates an attempt and a
    # lease.  It is deliberately absent from ordinary transition().
    JobStatus.QUEUED: frozenset({JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(),
    JobStatus.RETRY_WAIT: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.CANCEL_REQUESTED: frozenset(),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}

_STAGE_ORDER: dict[JobStage, int] = {
    JobStage.VALIDATE: 0,
    JobStage.STAGE_OBJECT: 1,
    JobStage.PARSE: 2,
    JobStage.BUILD_BLOCKS: 3,
    JobStage.BUILD_CHUNKS: 4,
    JobStage.EMBED: 5,
    JobStage.VERIFY: 6,
    JobStage.PUBLISH: 7,
    JobStage.PURGE: 8,
}


@dataclass(frozen=True)
class PublishResult:
    published: bool
    reason: str


class InMemoryJobStore:
    """A deterministic fake port; no persistence or external side effects."""

    def __init__(self) -> None:
        self.jobs: dict[UUID, JobRecord] = {}

    def get(self, job_id: UUID) -> JobRecord | None:
        return self.jobs.get(job_id)

    def save(self, job: JobRecord) -> JobRecord:
        self.jobs[job.job_id] = job
        return job

    def list_for_book(self, user_id: UUID, book_id: UUID) -> list[JobRecord]:
        return [
            job for job in self.jobs.values()
            if job.user_id == user_id and job.book_id == book_id
        ]

    def claim_next(self, worker_id: str, *, lease_seconds: int = 30) -> JobRecord | None:
        """Claim the oldest available job for the dependency-free worker test."""

        candidates = sorted(self.jobs.values(), key=lambda item: (item.created_at, str(item.job_id)))
        for job in candidates:
            if job.status in {JobStatus.QUEUED, JobStatus.RETRY_WAIT}:
                return JobController(self, clock=lambda: utc_now()).claim(
                    job.job_id, worker_id, lease_seconds=lease_seconds
                )
        return None


class JobController:
    def __init__(self, store: InMemoryJobStore | None = None, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.store = store or InMemoryJobStore()
        self.clock = clock

    def add(self, job: JobRecord) -> JobRecord:
        if self.store.get(job.job_id) is not None:
            raise ContractViolation(ErrorCode.CONFLICT, "job already exists")
        return self.store.save(job)

    def list_for_book(self, user_id: UUID, book_id: UUID) -> list[JobRecord]:
        list_for_book = getattr(self.store, "list_for_book", None)
        if callable(list_for_book):
            return list(list_for_book(user_id, book_id))
        return []

    def claim_next(self, worker_id: str, *, lease_seconds: int = 30) -> JobRecord | None:
        """Atomically claim one queued job through the selected store."""

        persistent_claim = getattr(self.store, "claim_next", None)
        if callable(persistent_claim) and not hasattr(self.store, "jobs"):
            return persistent_claim(worker_id, lease_seconds=lease_seconds)
        for job in sorted(
            getattr(self.store, "jobs", {}).values(),
            key=lambda item: (item.created_at, str(item.job_id)),
        ):
            if job.status in {JobStatus.QUEUED, JobStatus.RETRY_WAIT}:
                return self.claim(job.job_id, worker_id, lease_seconds=lease_seconds)
        return None

    def get(self, job_id: UUID) -> JobRecord:
        job = self.store.get(job_id)
        if job is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "job not found")
        return job

    def _replace(self, job: JobRecord, **changes: object) -> JobRecord:
        updated = job.model_copy(update={**changes, "updated_at": self.clock(), "row_version": job.row_version + 1})
        # model_copy(update=...) does not revalidate in Pydantic; validate the
        # full replacement before it enters the store.
        updated = JobRecord.model_validate(updated.model_dump())
        compare_and_swap = getattr(self.store, "compare_and_swap", None)
        if callable(compare_and_swap) and not hasattr(self.store, "jobs"):
            return compare_and_swap(job, updated)
        return self.store.save(updated)

    def transition(self, job_id: UUID, target: JobStatus) -> JobRecord:
        job = self.get(job_id)
        if target is JobStatus.RUNNING:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "running requires claim and lease")
        if target not in _ALLOWED[job.status]:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "illegal job state transition")
        return self._replace(job, status=target)

    def _require_active_lease(self, job: JobRecord, worker_id: str) -> datetime:
        now = self.clock()
        if (
            job.status not in {JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}
            or not worker_id
            or job.lease_owner != worker_id
            or job.lease_expires_at is None
            or job.lease_expires_at <= now
        ):
            raise ContractViolation(ErrorCode.CONFLICT, "worker does not hold an active job lease")
        return now

    def claim(self, job_id: UUID, worker_id: str, *, lease_seconds: int = 30) -> JobRecord:
        if not worker_id or lease_seconds <= 0:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid worker lease")
        job = self.get(job_id)
        now = self.clock()
        active_lease = job.lease_owner is not None and job.lease_expires_at is not None and job.lease_expires_at > now
        if active_lease:
            raise ContractViolation(ErrorCode.CONFLICT, "job lease is held")
        if job.status is JobStatus.RUNNING and not active_lease:
            # An expired lease may be taken over; a live lease may not be
            # duplicated.  This is the only running-state takeover path.
            pass
        elif job.status not in {JobStatus.QUEUED, JobStatus.RETRY_WAIT}:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "job is not claimable")
        if job.attempts >= 3:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "job attempt limit reached")
        persistent_claim = getattr(self.store, "claim", None)
        if callable(persistent_claim) and not hasattr(self.store, "jobs"):
            return persistent_claim(job_id, worker_id, lease_seconds=lease_seconds)
        return self._replace(
            job,
            status=JobStatus.RUNNING,
            attempts=job.attempts + 1,
            lease_owner=worker_id,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            heartbeat_at=now,
        )

    def heartbeat(self, job_id: UUID, worker_id: str, *, lease_seconds: int = 30) -> JobRecord:
        job = self.get(job_id)
        if lease_seconds <= 0:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid worker lease")
        now = self._require_active_lease(job, worker_id)
        return self._replace(job, heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease_seconds))

    def request_cancel(self, job_id: UUID) -> JobRecord:
        job = self.get(job_id)
        if job.status in {JobStatus.CANCELLED, JobStatus.SUCCEEDED, JobStatus.FAILED}:
            return job
        if job.status in {JobStatus.QUEUED, JobStatus.RETRY_WAIT}:
            return self._replace(job, status=JobStatus.CANCELLED, cancel_requested=True)
        if job.status is JobStatus.RUNNING:
            return self._replace(job, status=JobStatus.CANCEL_REQUESTED, cancel_requested=True)
        if job.status is JobStatus.CANCEL_REQUESTED:
            return job
        raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "job cannot be cancelled")

    def checkpoint(self, job_id: UUID, worker_id: str, *, stage: JobStage, values: dict[str, str | int | bool]) -> JobRecord:
        job = self.get(job_id)
        self._require_active_lease(job, worker_id)
        if job.status is JobStatus.CANCEL_REQUESTED:
            return self._replace(job, status=JobStatus.CANCELLED, stage=stage, lease_owner=None, lease_expires_at=None)
        if _STAGE_ORDER[stage] < _STAGE_ORDER[job.stage]:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "job stage cannot move backwards")
        if stage is JobStage.PUBLISH and values.get("verified") is not True:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "publish checkpoint requires verified=true")
        if stage is JobStage.PURGE and values.get("purged") is not True:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "purge checkpoint requires purged=true")
        return self._replace(job, stage=stage, checkpoint=dict(values))

    def succeed(self, job_id: UUID, worker_id: str) -> JobRecord:
        job = self.get(job_id)
        self._require_active_lease(job, worker_id)
        if job.status is JobStatus.CANCEL_REQUESTED:
            return self._replace(job, status=JobStatus.CANCELLED, lease_owner=None, lease_expires_at=None)
        if job.type is JobType.IMPORT_BOOK and not (
            job.stage is JobStage.PUBLISH and job.checkpoint.get("verified") is True
        ):
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "import job must verify before success")
        if job.type is JobType.DELETE_BOOK and not (
            job.stage is JobStage.PURGE and job.checkpoint.get("purged") is True
        ):
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "delete job must purge before success")
        return self._replace(job, status=JobStatus.SUCCEEDED, lease_owner=None, lease_expires_at=None)

    def fail(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        retryable: bool,
        error_code: ErrorCode = ErrorCode.INTERNAL_ERROR,
    ) -> JobRecord:
        job = self.get(job_id)
        self._require_active_lease(job, worker_id)
        if job.status is JobStatus.CANCEL_REQUESTED:
            return self._replace(job, status=JobStatus.CANCELLED, lease_owner=None, lease_expires_at=None)
        return self._replace(
            job,
            status=JobStatus.FAILED,
            retryable=retryable and job.attempts < 3,
            error_code=error_code,
            lease_owner=None,
            lease_expires_at=None,
        )

    def retry(self, job_id: UUID) -> JobRecord:
        job = self.get(job_id)
        if job.status is not JobStatus.FAILED or not job.retryable or job.attempts >= 3:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "job is not retryable")
        job = self._replace(job, status=JobStatus.RETRY_WAIT, error_code=None, retryable=False)
        return self._replace(job, status=JobStatus.QUEUED)

    def publish_verified(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        verify: Callable[[JobRecord], bool],
        publish_transaction: Callable[[JobRecord], JobRecord],
    ) -> PublishResult:
        """Verify completely, then invoke one callback for the atomic publish.

        The callback is the transaction boundary: it must return the already
        committed terminal JobRecord together with the active-version/ready
        update it performed.  This controller never calls succeed separately.
        """

        job = self.get(job_id)
        self._require_active_lease(job, worker_id)
        if job.type is not JobType.IMPORT_BOOK or job.stage is not JobStage.PUBLISH:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "publish requires the publish stage")
        if job.checkpoint.get("verified") is not True:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "publish requires a verified checkpoint")
        try:
            verified = verify(job)
        except Exception:
            self.fail(job_id, worker_id, retryable=False)
            return PublishResult(False, "verification_failed")
        if not verified:
            self.fail(job_id, worker_id, retryable=False)
            return PublishResult(False, "verification_failed")
        try:
            committed = publish_transaction(job)
        except Exception:
            # A transaction failure leaves the old active version untouched;
            # only the job's own lease is converted to a terminal failure.
            self.fail(job_id, worker_id, retryable=False)
            return PublishResult(False, "publish_failed")
        if not isinstance(committed, JobRecord):
            self.fail(job_id, worker_id, retryable=False)
            return PublishResult(False, "publish_result_invalid")
        if (
            committed.job_id != job.job_id
            or committed.user_id != job.user_id
            or committed.book_id != job.book_id
            or committed.type is not job.type
            or committed.status is not JobStatus.SUCCEEDED
            or committed.stage is not JobStage.PUBLISH
            or committed.attempts != job.attempts
            or committed.lease_owner is not None
            or committed.lease_expires_at is not None
        ):
            self.fail(job_id, worker_id, retryable=False)
            return PublishResult(False, "publish_result_invalid")
        persisted = self.store.get(job_id)
        if persisted is None or persisted.row_version != committed.row_version:
            self.store.save(JobRecord.model_validate(committed.model_dump()))
        return PublishResult(True, "published")


class JobWorker:
    """Small bounded worker loop shared by local and PostgreSQL runtimes.

    The handler owns the domain pipeline and must use the controller's lease,
    checkpoint, and terminal methods.  This class only performs one atomic
    claim at a time, so a second process cannot run the same queued job.
    """

    def __init__(
        self,
        controller: JobController,
        *,
        worker_id: str,
        handler: Callable[[JobRecord, JobController], None],
    ) -> None:
        if not worker_id:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "worker_id is required")
        self.controller = controller
        self.worker_id = worker_id
        self.handler = handler

    def run_once(self, *, lease_seconds: int = 120) -> JobRecord | None:
        job = self.controller.claim_next(self.worker_id, lease_seconds=lease_seconds)
        if job is None:
            return None
        try:
            self.handler(job, self.controller)
        except ContractViolation as exc:
            current = self.controller.get(job.job_id)
            if current.status in {JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}:
                self.controller.fail(job.job_id, self.worker_id, retryable=False, error_code=exc.code)
            raise
        except Exception:
            current = self.controller.get(job.job_id)
            if current.status in {JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}:
                self.controller.fail(job.job_id, self.worker_id, retryable=False)
            raise
        return self.controller.get(job.job_id)
