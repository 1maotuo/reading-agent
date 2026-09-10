from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from reading_agent.api import create_app, export_openapi
from reading_agent.contracts import (
    ErrorCode,
    EvidenceBundle,
    EvidenceCandidate,
    EvidenceRef,
    JobRecord,
    JobStage,
    JobStatus,
    JobType,
    ScopeContext,
    SearchBookOutput,
    SourceLocator,
    ToolCallStatus,
    ToolName,
    ToolResult,
    export_tool_schemas,
)
from reading_agent.domain import (
    AnswerEventLedger,
    ContractViolation,
    canonical_sha256,
    delete_book_fail_closed,
    dispatch_tool,
    issue_verified_evidence_token,
    require_evidence,
    sha256_text,
    validate_evidence_bundle,
)
from reading_agent.worker import JobController


UTC = timezone.utc


def uid() -> UUID:
    return uuid4()


def make_scope(*, user_id: UUID | None = None, furthest: int | None = 5) -> ScopeContext:
    return ScopeContext(
        session_id=uid(),
        user_id=user_id or uid(),
        book_id=uid(),
        book_version_id=uid(),
        chapter_id=uid(),
        furthest_chunk_index=furthest,
        request_id=uid(),
        trace_id=uid(),
    )


def make_job(*, job_type: JobType = JobType.IMPORT_BOOK) -> JobRecord:
    now = datetime.now(UTC)
    return JobRecord(
        job_id=uid(),
        user_id=uid(),
        book_id=uid(),
        type=job_type,
        idempotency_key=f"job-key-{uuid4()}",
        input_sha256=sha256_text("input"),
        pipeline_version="stage04-test",
        created_at=now,
        updated_at=now,
    )


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime.now(UTC)

    def now(self) -> datetime:
        return self.value


def make_publishable(controller: JobController, worker: str = "worker") -> JobRecord:
    job = controller.add(make_job())
    controller.claim(job.job_id, worker, lease_seconds=30)
    controller.checkpoint(job.job_id, worker, stage=JobStage.VERIFY, values={"verified": True})
    return controller.checkpoint(job.job_id, worker, stage=JobStage.PUBLISH, values={"verified": True})


def committed_job(job: JobRecord) -> JobRecord:
    return job.model_copy(
        update={
            "status": JobStatus.SUCCEEDED,
            "lease_owner": None,
            "lease_expires_at": None,
            "row_version": job.row_version + 1,
        }
    )


def test_c06_job_state_machine_lease_takeover_attempt_limit_and_cancel() -> None:
    clock = MutableClock()
    controller = JobController(clock=clock.now)
    job = controller.add(make_job())
    with pytest.raises(ContractViolation):
        controller.transition(job.job_id, JobStatus.RUNNING)
    controller.claim(job.job_id, "worker-a", lease_seconds=10)
    with pytest.raises(ContractViolation):
        controller.claim(job.job_id, "worker-b", lease_seconds=10)
    clock.value += timedelta(seconds=11)
    takeover = controller.claim(job.job_id, "worker-b", lease_seconds=10)
    assert takeover.attempts == 2
    failed = controller.fail(job.job_id, "worker-b", retryable=True)
    assert failed.status is JobStatus.FAILED
    assert failed.retryable is True
    queued = controller.retry(job.job_id)
    assert queued.status is JobStatus.QUEUED
    third = controller.claim(job.job_id, "worker-c", lease_seconds=10)
    assert third.attempts == 3
    exhausted = controller.fail(job.job_id, "worker-c", retryable=True)
    assert exhausted.status is JobStatus.FAILED
    assert exhausted.retryable is False
    with pytest.raises(ContractViolation):
        controller.retry(job.job_id)
    expired = controller.add(make_job())
    controller.claim(expired.job_id, "worker-a", lease_seconds=1)
    clock.value += timedelta(seconds=2)
    with pytest.raises(ContractViolation):
        controller.succeed(expired.job_id, "worker-a")
    queued = controller.add(make_job())
    cancelled = controller.request_cancel(queued.job_id)
    assert cancelled.status is JobStatus.CANCELLED
    assert controller.request_cancel(queued.job_id).status is JobStatus.CANCELLED
    running = controller.add(make_job())
    controller.claim(running.job_id, "worker-a")
    requested = controller.request_cancel(running.job_id)
    assert requested.status is JobStatus.CANCEL_REQUESTED
    done = controller.checkpoint(running.job_id, "worker-a", stage=JobStage.PARSE, values={"offset": 1})
    assert done.status is JobStatus.CANCELLED


def test_c07_publish_requires_verify_and_returns_atomic_terminal_commit() -> None:
    controller = JobController()
    failed_job = controller.add(make_job())
    controller.claim(failed_job.job_id, "worker")
    with pytest.raises(ContractViolation):
        controller.publish_verified(
            failed_job.job_id,
            "worker",
            verify=lambda _: True,
            publish_transaction=lambda job: committed_job(job),
        )

    ready_job = make_publishable(controller)
    active = ["old-ready"]
    calls: list[str] = []
    result = controller.publish_verified(
        ready_job.job_id,
        "worker",
        verify=lambda _: False,
        publish_transaction=lambda job: (calls.append("must-not-run"), committed_job(job))[1],
    )
    assert not result.published
    assert active == ["old-ready"]
    assert calls == []
    assert controller.get(ready_job.job_id).status is JobStatus.FAILED

    successful_job = make_publishable(controller)

    def publish_transaction(job: JobRecord) -> JobRecord:
        calls.append("one-transaction")
        active[0] = "new-ready"
        return committed_job(job)

    result = controller.publish_verified(
        successful_job.job_id,
        "worker",
        verify=lambda _: True,
        publish_transaction=publish_transaction,
    )
    assert result.published
    assert calls == ["one-transaction"]
    assert active == ["new-ready"]
    assert controller.get(successful_job.job_id).status is JobStatus.SUCCEEDED

    broken_job = make_publishable(controller)
    result = controller.publish_verified(
        broken_job.job_id,
        "worker",
        verify=lambda _: True,
        publish_transaction=lambda _: (_ for _ in ()).throw(RuntimeError("transaction failed")),
    )
    assert not result.published
    assert active == ["new-ready"]
    assert controller.get(broken_job.job_id).status is JobStatus.FAILED


def evidence_for(scope: ScopeContext, *, chunk_index: int = 2, quote: str = "canonical evidence") -> EvidenceRef:
    return EvidenceRef(
        evidence_id=uid(),
        user_id=scope.user_id,
        book_id=scope.book_id,
        book_version_id=scope.book_version_id,
        chapter_id=scope.chapter_id,
        chunk_id=uid(),
        chunk_index=chunk_index,
        block_ids=[uid()],
        quote=quote,
        content_sha256=sha256_text(quote),
        source_locator=SourceLocator(kind="synthetic", value="independent-oracle"),
    )


class ProviderSpy:
    def __init__(self, result_factory):
        self.calls: list[tuple[ToolName, object, ScopeContext, UUID]] = []
        self.result_factory = result_factory

    def call(self, name, args, scope, call_id):
        self.calls.append((name, args, scope, call_id))
        return self.result_factory(name, args, scope, call_id)


def test_c11_local_tools_have_output_models_and_mandatory_independent_readback() -> None:
    current = make_scope()
    canonical = evidence_for(current)
    output = SearchBookOutput(
        items=[
            EvidenceCandidate(
                evidence_id=canonical.evidence_id,
                chapter_id=current.chapter_id,
                chunk_index=canonical.chunk_index,
                quote=canonical.quote,
                score=1.0,
                source_locator=canonical.source_locator,
            )
        ]
    )
    provider = ProviderSpy(
        lambda name, args, scope, call_id: ToolResult(
            call_id=call_id,
            name=name,
            status=ToolCallStatus.SUCCEEDED,
            data=output,
            evidence_refs=[canonical],
        )
    )
    with pytest.raises(ContractViolation):
        dispatch_tool(
            scope=current,
            name=ToolName.SEARCH_BOOK,
            args={"query": "question", "top_k": 3},
            call_id=uid(),
            provider=provider,
        )
    assert provider.calls == []
    call_id = uid()
    result = dispatch_tool(
        scope=current,
        name=ToolName.SEARCH_BOOK,
        args={"query": "question", "top_k": 3},
        call_id=call_id,
        provider=provider,
        evidence_reader=lambda _scope, evidence_id: canonical if evidence_id == canonical.evidence_id else None,
    )
    assert result.status is ToolCallStatus.SUCCEEDED
    assert isinstance(result.data, SearchBookOutput)
    assert provider.calls[0][2] == current
    with pytest.raises(ContractViolation):
        dispatch_tool(
            scope=current,
            name=ToolName.SEARCH_BOOK,
            args={"query": "question", "scope": "forged"},
            call_id=uid(),
            provider=provider,
            evidence_reader=lambda _scope, evidence_id: canonical if evidence_id == canonical.evidence_id else None,
        )
    out_of_scope = evidence_for(make_scope(user_id=uid()))
    bad_provider = ProviderSpy(
        lambda name, args, scope, call_id: ToolResult(
            call_id=call_id,
            name=name,
            status=ToolCallStatus.SUCCEEDED,
            data=output,
            evidence_refs=[out_of_scope],
        )
    )
    with pytest.raises(ContractViolation):
        dispatch_tool(
            scope=current,
            name=ToolName.READ_BOOK_BLOCKS,
            args={"block_ids": [uid()]},
            call_id=uid(),
            provider=bad_provider,
            evidence_reader=lambda _scope, evidence_id: out_of_scope if evidence_id == out_of_scope.evidence_id else None,
        )
    assert len(bad_provider.calls) == 1
    assert set(export_tool_schemas()) == {name.value for name in ToolName}


def test_c12_web_tools_are_disabled_without_network_or_provider_call() -> None:
    current = make_scope()
    provider = ProviderSpy(lambda *_: pytest.fail("disabled web tool must not call provider"))
    for name, args in (
        (ToolName.WEB_SEARCH, {"query": "internet"}),
        (ToolName.READ_WEB_SOURCE, {"source_id": "opaque-source"}),
    ):
        result = dispatch_tool(scope=current, name=name, args=args, call_id=uid(), provider=provider)
        assert result.status is ToolCallStatus.DISABLED
        assert result.error is not None and result.error.code is ErrorCode.TOOL_DISABLED
    assert provider.calls == []


def test_c13_evidence_token_readback_scope_and_progress_are_required() -> None:
    current = make_scope(furthest=5)
    canonical = evidence_for(current, chunk_index=4)
    bundle = EvidenceBundle(refs=[canonical])
    oracle = lambda _scope, _id: canonical
    assert validate_evidence_bundle(current, bundle, oracle) == bundle
    run_id = uid()
    ledger = AnswerEventLedger(run_id=run_id, trace_id=uid(), scope=current)
    ledger.accepted()
    call_id = uid()
    ledger.start_tool(call_id, ToolName.SEARCH_BOOK)
    ledger.finish_tool(call_id, ToolName.SEARCH_BOOK, "succeeded")
    token = issue_verified_evidence_token(run_id=run_id, scope=current, bundle=bundle, oracle=oracle)
    ledger.add_verified_evidence(token)
    assert ledger.events[-1].type.value == "evidence"
    with pytest.raises(ContractViolation):
        ledger.evidence([canonical.evidence_id])  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        ledger.complete(answer_id=uid(), conversation_id=uid(), evidence_ids=[uid()])
    with pytest.raises(ContractViolation):
        validate_evidence_bundle(
            current,
            EvidenceBundle(refs=[canonical.model_copy(update={"quote": "forged", "content_sha256": sha256_text("forged")})]),
            oracle,
        )
    with pytest.raises(ContractViolation):
        validate_evidence_bundle(current, EvidenceBundle(refs=[canonical.model_copy(update={"chunk_index": 6})]), oracle)
    with pytest.raises(ContractViolation):
        require_evidence(None)
    with pytest.raises(ContractViolation):
        require_evidence(EvidenceBundle.model_construct(refs=[]))


def test_c14_answer_ledger_rejects_duplicate_inflight_calls_late_events_and_connection_close() -> None:
    current = make_scope()
    canonical = evidence_for(current)
    bundle = EvidenceBundle(refs=[canonical])
    run_id = uid()
    ledger = AnswerEventLedger(run_id=run_id, trace_id=uid(), scope=current)
    ledger.accepted()
    call_id = uid()
    ledger.start_tool(call_id, ToolName.SEARCH_BOOK)
    with pytest.raises(ContractViolation):
        ledger.start_tool(call_id, ToolName.SEARCH_BOOK)
    with pytest.raises(ContractViolation):
        ledger.finish_tool(call_id, ToolName.READ_BOOK_BLOCKS, "succeeded")
    ledger.finish_tool(call_id, ToolName.SEARCH_BOOK, "succeeded")
    token = issue_verified_evidence_token(
        run_id=run_id,
        scope=current,
        bundle=bundle,
        oracle=lambda _scope, _id: canonical,
    )
    ledger.evidence(token)
    ledger.close()
    assert ledger.closed is True
    ledger.answer_delta("answer")
    ledger.complete(answer_id=uid(), conversation_id=uid(), evidence_ids=[canonical.evidence_id])
    before = ledger.events
    with pytest.raises(ContractViolation):
        ledger.complete(answer_id=uid(), conversation_id=uid(), evidence_ids=[canonical.evidence_id])
    assert ledger.events == before
    ordinary = AnswerEventLedger(run_id=uid(), trace_id=uid())
    ordinary.accepted()
    ordinary.fail_unhandled(RuntimeError("private stack"))
    assert ordinary.status.value == "failed"
    assert ordinary.terminal.value == "failed"


def test_c16_delete_tombstone_order_revoke_cancel_and_material_clear() -> None:
    scope = make_scope()
    actions: list[str] = []

    class DeleteFake:
        def tombstone(self, _scope):
            actions.append("tombstone")

        def revoke_access(self, _scope):
            actions.append("revoke")

        def clear_read_material(self, _scope):
            actions.append("clear")

    delete_book_fail_closed(scope=scope, repository=DeleteFake(), request_cancel=lambda: actions.append("cancel"))
    assert actions == ["tombstone", "revoke", "cancel", "clear"]


def test_c18_static_ddl_constraints_indexes_and_schema_exports_are_deterministic_db_blocked() -> None:
    root = Path(__file__).resolve().parents[2]
    ddl = (root / "db" / "migrations" / "0001_stage04_contract_draft.sql").read_text(encoding="utf-8")
    assert "BLOCKED_BY_F03_02" in ddl
    for table in (
        "users",
        "sessions",
        "books",
        "book_versions",
        "chapters",
        "blocks",
        "chunks",
        "jobs",
        "reading_progress",
        "highlights",
        "answer_runs",
        "answer_events",
        "traces",
        "idempotency_keys",
    ):
        assert f"CREATE TABLE {table}" in ddl
    assert "status text NOT NULL CHECK (status IN ('queued'" in ddl
    assert "stage text NOT NULL CHECK (stage IN ('validate'" in ddl
    assert "CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))" in ddl
    assert "FOREIGN KEY (user_id, book_id, book_version_id, chapter_id)" in ddl
    assert "CREATE INDEX stage04_blocks_scope_idx" in ddl
    assert "CREATE INDEX stage04_chunks_scope_idx" in ddl
    assert "CREATE INDEX stage04_jobs_claim_idx" in ddl
    assert not any(line.strip().startswith("CREATE EXTENSION") for line in ddl.splitlines())
    tool_first = export_tool_schemas()
    tool_second = export_tool_schemas()
    assert canonical_sha256(tool_first) == canonical_sha256(tool_second)
    app = create_app()
    openapi_first = export_openapi(app)
    openapi_second = export_openapi(app)
    assert canonical_sha256(openapi_first) == canonical_sha256(openapi_second)
    assert "ScopeContext" not in str(openapi_first)
