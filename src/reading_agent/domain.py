"""Fail-closed domain rules shared by the HTTP and worker contract layers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from .contracts import (
    AnswerRunStatus,
    Book,
    BookVersion,
    Block,
    Chapter,
    Chunk,
    ErrorBody,
    ErrorCode,
    EvidenceBundle,
    EvidenceRef,
    HighlightAnchor,
    JobRecord,
    JobStage,
    JobStatus,
    JobType,
    ProgressUpdate,
    ReadingProgress,
    ScopeContext,
    SSEAcceptedPayload,
    SSEAnswerDeltaPayload,
    SSECancelledPayload,
    SSECompletedPayload,
    SSEEvidencePayload,
    SSEEnvelope,
    SSEEventType,
    SSEFailedPayload,
    SSEHeartbeatPayload,
    SSEStatusPayload,
    SSEToolFinishedPayload,
    SSEToolStartedPayload,
    ToolArgs,
    ToolCallStatus,
    ToolError,
    ToolName,
    ToolResult,
    TOOL_ARGUMENT_MODELS,
    TOOL_OUTPUT_MODELS,
)
from .ports import AuthorizedScopeRecord, AuthorizationPort, EvidenceReaderPort


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ContractViolation(Exception):
    """A safe, user-facing contract failure with no sensitive details."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        details: dict[str, str | int | bool] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code or error_status(code)
        self.details = details
        super().__init__(message)


def error_status(code: ErrorCode) -> int:
    return {
        ErrorCode.UNAUTHENTICATED: 401,
        ErrorCode.CSRF_FAILED: 403,
        ErrorCode.NOT_FOUND: 404,
        ErrorCode.FORBIDDEN: 403,
        ErrorCode.CONFLICT: 409,
        ErrorCode.VERSION_CONFLICT: 409,
        ErrorCode.IDEMPOTENCY_CONFLICT: 409,
        ErrorCode.INVALID_INPUT: 422,
        ErrorCode.UNSUPPORTED_FORMAT: 415,
        ErrorCode.PAYLOAD_TOO_LARGE: 413,
        ErrorCode.BOOK_NOT_READY: 409,
        ErrorCode.INVALID_JOB_STATE: 409,
        ErrorCode.ANCHOR_INVALID: 422,
        ErrorCode.TOOL_DISABLED: 409,
        ErrorCode.TOOL_TIMEOUT: 504,
        ErrorCode.EVIDENCE_REQUIRED: 422,
        ErrorCode.RATE_LIMITED: 429,
        ErrorCode.INTERNAL_ERROR: 500,
    }[code]


def make_error_body(
    violation: ContractViolation,
    *,
    request_id: UUID | None = None,
) -> ErrorBody:
    return ErrorBody(
        code=violation.code,
        message=violation.message,
        request_id=request_id or uuid4(),
        retryable=violation.retryable,
        details=violation.details,
    )


def _get(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def assert_scope_identity(
    scope: ScopeContext,
    value: Any,
    *,
    require_chapter: bool = False,
    require_block: bool = False,
) -> None:
    """Require every available composite identity component to match scope."""

    required = ("user_id", "book_id", "book_version_id")
    if require_chapter:
        required += ("chapter_id",)
    if require_block:
        required += ("block_id",)
    for field in required:
        expected = getattr(scope, field, None)
        actual = _get(value, field)
        if expected is None or actual is None or actual != expected:
            raise ContractViolation(ErrorCode.FORBIDDEN, "resource is outside the authorized scope")
    if getattr(scope, "chapter_id", None) is not None and _get(value, "chapter_id") not in {
        None,
        scope.chapter_id,
    }:
        raise ContractViolation(ErrorCode.FORBIDDEN, "resource is outside the authorized scope")


def build_scope(
    *,
    session_id: UUID,
    user_id: UUID,
    book_id: UUID,
    book_version_id: UUID,
    request_id: UUID,
    trace_id: UUID,
    authorization: AuthorizationPort,
    chapter_id: UUID | None = None,
    furthest_chunk_index: int | None = None,
    expires_at: datetime | None = None,
) -> ScopeContext:
    """Read authorization first, then derive the frozen scope from that row."""

    now = utc_now()
    if expires_at is not None and (expires_at.tzinfo is None or expires_at <= now):
        raise ContractViolation(ErrorCode.UNAUTHENTICATED, "session is expired")
    try:
        record = authorization.read_authorized_scope(
            session_id=session_id,
            user_id=user_id,
            book_id=book_id,
            book_version_id=book_version_id,
            chapter_id=chapter_id,
        )
    except Exception as exc:  # pragma: no cover - adapter boundary
        raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found") from exc
    if record is None:
        raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
    if record.session_id != session_id or record.user_id != user_id:
        raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
    if chapter_id is None:
        if record.chapter_id is not None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
    elif record.chapter_id is None:
        raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
    return ScopeContext(
        session_id=record.session_id,
        user_id=record.user_id,
        book_id=record.book_id,
        book_version_id=record.book_version_id,
        chapter_id=record.chapter_id,
        furthest_chunk_index=record.furthest_chunk_index,
        request_id=request_id,
        trace_id=trace_id,
    )


def derive_chapter_scope(
    *,
    parent_scope: ScopeContext,
    chapter_id: UUID,
    authorization: AuthorizationPort,
) -> ScopeContext:
    """Authorize/read a chapter before placing its server value in ScopeContext."""

    record = authorization.read_authorized_scope(
        session_id=parent_scope.session_id,
        user_id=parent_scope.user_id,
        book_id=parent_scope.book_id,
        book_version_id=parent_scope.book_version_id,
        chapter_id=chapter_id,
    )
    if record is None or record.chapter_id is None:
        raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
    if (
        record.session_id != parent_scope.session_id
        or record.user_id != parent_scope.user_id
        or record.book_id != parent_scope.book_id
        or record.book_version_id != parent_scope.book_version_id
    ):
        raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
    return ScopeContext(
        session_id=record.session_id,
        user_id=record.user_id,
        book_id=record.book_id,
        book_version_id=record.book_version_id,
        chapter_id=record.chapter_id,
        furthest_chunk_index=record.furthest_chunk_index,
        request_id=parent_scope.request_id,
        trace_id=parent_scope.trace_id,
    )


def assert_book_version_consistency(book: Book, version: BookVersion) -> None:
    if book.user_id != version.user_id or book.book_id != version.book_id:
        raise ContractViolation(ErrorCode.FORBIDDEN, "book version is outside the book scope")
    if version.status.value != "ready" and book.active_version_id == version.book_version_id:
        raise ContractViolation(ErrorCode.BOOK_NOT_READY, "book version is not ready")


def assert_chapter_consistency(chapter: Chapter, version: BookVersion) -> None:
    if (
        chapter.user_id != version.user_id
        or chapter.book_id != version.book_id
        or chapter.book_version_id != version.book_version_id
    ):
        raise ContractViolation(ErrorCode.FORBIDDEN, "chapter is outside the book version")


def assert_block_consistency(block: Block, chapter: Chapter) -> None:
    if (
        block.user_id != chapter.user_id
        or block.book_id != chapter.book_id
        or block.book_version_id != chapter.book_version_id
        or block.chapter_id != chapter.chapter_id
    ):
        raise ContractViolation(ErrorCode.FORBIDDEN, "block is outside the chapter")


def assert_chunk_consistency(chunk: Chunk, chapter: Chapter) -> None:
    if (
        chunk.user_id != chapter.user_id
        or chunk.book_id != chapter.book_id
        or chunk.book_version_id != chapter.book_version_id
        or chunk.chapter_id != chapter.chapter_id
    ):
        raise ContractViolation(ErrorCode.FORBIDDEN, "chunk is outside the chapter")


def chunk_body_sha256(blocks: Iterable[Block]) -> str:
    """Canonical hash for the ordered trusted Block body represented by a Chunk."""

    return sha256_text("\n".join(block.text for block in blocks))


def validate_chunk_block_readback(
    *,
    scope: ScopeContext,
    chunk: Chunk,
    trusted_blocks: Iterable[Block],
) -> tuple[Block, ...]:
    """Validate a DB Chunk only against independently reread canonical Blocks."""

    assert_scope_identity(scope, chunk, require_chapter=True)
    blocks = tuple(trusted_blocks)
    if len(blocks) != len(chunk.block_ids) or not blocks:
        raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "chunk block readback is incomplete")
    if [block.block_id for block in blocks] != chunk.block_ids:
        raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "chunk block order does not match trusted readback")
    ordinals = [block.ordinal for block in blocks]
    if ordinals != sorted(ordinals) or len(set(ordinals)) != len(ordinals):
        raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "chunk block order is not canonical")
    for block in blocks:
        if (
            block.user_id != chunk.user_id
            or block.book_id != chunk.book_id
            or block.book_version_id != chunk.book_version_id
            or block.chapter_id != chunk.chapter_id
        ):
            raise ContractViolation(ErrorCode.FORBIDDEN, "chunk references a block outside its composite scope")
        if sha256_text(block.text) != block.text_sha256:
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "trusted block body hash mismatch")
    if chunk.text_sha256 != chunk_body_sha256(blocks):
        raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "chunk body hash does not match trusted blocks")
    return blocks


def validate_evidence_bundle(
    scope: ScopeContext,
    bundle: EvidenceBundle,
    oracle: EvidenceReaderPort | Callable[[ScopeContext, UUID], EvidenceRef | None],
) -> EvidenceBundle:
    """Validate model/provider evidence against an independent server readback."""

    if not bundle.refs:
        raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "at least one evidence reference is required")
    for requested in bundle.refs:
        assert_scope_identity(scope, requested, require_chapter=True)
        if scope.furthest_chunk_index is not None and requested.chunk_index > scope.furthest_chunk_index:
            raise ContractViolation(ErrorCode.FORBIDDEN, "evidence is beyond reading progress")
        reread = oracle.reread(scope, requested.evidence_id) if hasattr(oracle, "reread") else oracle(scope, requested.evidence_id)
        if reread is None:
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "evidence could not be verified")
        assert_scope_identity(scope, reread, require_chapter=True)
        if (
            reread.evidence_id != requested.evidence_id
            or reread.chunk_id != requested.chunk_id
            or reread.chunk_index != requested.chunk_index
            or reread.block_ids != requested.block_ids
            or reread.quote != requested.quote
            or reread.content_sha256 != requested.content_sha256
            or reread.source_locator != requested.source_locator
        ):
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "evidence readback did not match")
    return bundle


def scope_identity_key(scope: ScopeContext) -> tuple[UUID, UUID, UUID, UUID, UUID | None]:
    return (scope.user_id, scope.book_id, scope.book_version_id, scope.session_id, scope.chapter_id)


_VERIFIED_TOKEN_PROOF = object()


@dataclass(frozen=True, init=False)
class VerifiedEvidenceToken:
    """Opaque run-bound proof issued only after independent evidence readback."""

    run_id: UUID
    scope_key: tuple[UUID, UUID, UUID, UUID, UUID | None]
    evidence_ids: tuple[UUID, ...]
    _proof: object

    def __init__(
        self,
        *,
        run_id: UUID,
        scope_key: tuple[UUID, UUID, UUID, UUID, UUID | None],
        evidence_ids: tuple[UUID, ...],
        _proof: object,
    ) -> None:
        if _proof is not _VERIFIED_TOKEN_PROOF:
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "evidence proof is invalid")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "scope_key", scope_key)
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "_proof", _proof)


def issue_verified_evidence_token(
    *,
    run_id: UUID,
    scope: ScopeContext,
    bundle: EvidenceBundle,
    oracle: EvidenceReaderPort | Callable[[ScopeContext, UUID], EvidenceRef | None],
) -> VerifiedEvidenceToken:
    verified = validate_evidence_bundle(scope, bundle, oracle)
    return VerifiedEvidenceToken(
        run_id=run_id,
        scope_key=scope_identity_key(scope),
        evidence_ids=tuple(ref.evidence_id for ref in verified.refs),
        _proof=_VERIFIED_TOKEN_PROOF,
    )


def require_evidence(bundle: EvidenceBundle | None) -> EvidenceBundle:
    if bundle is None or not bundle.refs:
        raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "answer requires verified evidence")
    return bundle


def _contains_internal_scope(value: Any) -> bool:
    if isinstance(value, ScopeContext):
        return True
    if hasattr(value, "model_dump"):
        return _contains_internal_scope(value.model_dump())
    if isinstance(value, Mapping):
        return any(
            key in {"scope", "scope_context", "session_id", "user_id", "book_id", "book_version_id", "furthest_chunk_index"}
            or _contains_internal_scope(v)
            for key, v in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_contains_internal_scope(item) for item in value)
    return False


def validate_tool_result(
    scope: ScopeContext,
    result: ToolResult,
    *,
    oracle: EvidenceReaderPort | Callable[[ScopeContext, UUID], EvidenceRef | None] | None = None,
) -> ToolResult:
    if _contains_internal_scope(result.data):
        raise ContractViolation(ErrorCode.INTERNAL_ERROR, "tool result contains internal scope")
    local_tools = {
        ToolName.SEARCH_BOOK,
        ToolName.READ_BOOK_BLOCKS,
        ToolName.GET_BOOK_STRUCTURE,
        ToolName.SEARCH_READING_MEMORY,
    }
    if result.status is ToolCallStatus.SUCCEEDED and result.name in local_tools and not result.evidence_refs:
        raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "local tool result requires evidence")
    if result.status is ToolCallStatus.SUCCEEDED and result.name in local_tools and oracle is None:
        raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "local tool result requires server readback")
    for evidence in result.evidence_refs:
        assert_scope_identity(scope, evidence, require_chapter=True)
        if scope.furthest_chunk_index is not None and evidence.chunk_index > scope.furthest_chunk_index:
            raise ContractViolation(ErrorCode.FORBIDDEN, "tool result is beyond reading progress")
    if result.evidence_refs and oracle is None:
        raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "evidence requires server readback")
    if result.evidence_refs:
        validate_evidence_bundle(scope, EvidenceBundle(refs=result.evidence_refs), oracle)  # type: ignore[arg-type]
    return result


def dispatch_tool(
    *,
    scope: ScopeContext,
    name: ToolName,
    args: Mapping[str, Any],
    call_id: UUID,
    provider: Any,
    web_enabled: bool = False,
    evidence_reader: EvidenceReaderPort | Callable[[ScopeContext, UUID], EvidenceRef | None] | None = None,
) -> ToolResult:
    """Parse server-controlled tool args and call only the selected local port."""

    if name in {ToolName.WEB_SEARCH, ToolName.READ_WEB_SOURCE} and not web_enabled:
        return ToolResult(
            call_id=call_id,
            name=name,
            status=ToolCallStatus.DISABLED,
            error=ToolError(code=ErrorCode.TOOL_DISABLED, message="web tools are disabled in this contract"),
        )
    if name in {
        ToolName.SEARCH_BOOK,
        ToolName.READ_BOOK_BLOCKS,
        ToolName.GET_BOOK_STRUCTURE,
        ToolName.SEARCH_READING_MEMORY,
    } and evidence_reader is None:
        raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "evidence-producing tools require server readback")
    if not isinstance(args, Mapping):
        raise ContractViolation(ErrorCode.INVALID_INPUT, "tool arguments must be an object")
    try:
        parsed = TOOL_ARGUMENT_MODELS[name].model_validate(dict(args))
    except Exception as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid tool arguments") from exc
    if _contains_internal_scope(args):
        raise ContractViolation(ErrorCode.INVALID_INPUT, "tool arguments cannot contain scope")
    if provider is None or not hasattr(provider, "call"):
        raise ContractViolation(ErrorCode.INTERNAL_ERROR, "tool provider is unavailable", retryable=True)
    result = provider.call(name, parsed, scope, call_id)
    if not isinstance(result, ToolResult):
        raise ContractViolation(ErrorCode.INTERNAL_ERROR, "tool provider returned an invalid result")
    if result.call_id != call_id or result.name is not name:
        raise ContractViolation(ErrorCode.INTERNAL_ERROR, "tool result identity mismatch")
    if result.status is ToolCallStatus.SUCCEEDED:
        expected_output = TOOL_OUTPUT_MODELS[name]
        if not isinstance(result.data, expected_output):
            raise ContractViolation(ErrorCode.INTERNAL_ERROR, "tool result output schema mismatch")
        if name in {
            ToolName.SEARCH_BOOK,
            ToolName.READ_BOOK_BLOCKS,
            ToolName.GET_BOOK_STRUCTURE,
            ToolName.SEARCH_READING_MEMORY,
        } and not result.evidence_refs:
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "local tool result requires evidence")
    return validate_tool_result(scope, result, oracle=evidence_reader)


class AnswerEventLedger:
    """One-run SSE ledger with ordering, evidence, and terminal-state guards."""

    def __init__(
        self,
        *,
        run_id: UUID,
        trace_id: UUID,
        request_id: UUID | None = None,
        scope: ScopeContext | None = None,
        evidence_required: bool = True,
        clock: Callable[[], datetime] = utc_now,
        event_sink: Callable[[SSEEnvelope], None] | None = None,
    ) -> None:
        self.run_id = run_id
        self.trace_id = trace_id
        self.request_id = request_id or uuid4()
        self._scope_key = scope_identity_key(scope) if scope is not None else None
        self.evidence_required = evidence_required
        self._clock = clock
        self._event_sink = event_sink
        self._events: list[SSEEnvelope] = []
        self._started_tools: dict[UUID, ToolName] = {}
        self._finished_tools: set[UUID] = set()
        self._verified_evidence_ids: set[UUID] = set()
        self._terminal: SSEEventType | None = None
        self._connection_closed = False
        self.status = AnswerRunStatus.ACCEPTED

    @property
    def events(self) -> tuple[SSEEnvelope, ...]:
        return tuple(self._events)

    @property
    def terminal(self) -> SSEEventType | None:
        return self._terminal

    @property
    def closed(self) -> bool:
        return self._connection_closed

    def _ensure_open(self) -> None:
        if self._terminal is not None:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "answer run is already terminal")

    def _append(self, event_type: SSEEventType, payload: Any) -> SSEEnvelope:
        self._ensure_open()
        if hasattr(payload, "model_dump"):
            payload_dict = payload.model_dump(mode="json")
        elif isinstance(payload, Mapping):
            payload_dict = dict(payload)
        else:
            raise ContractViolation(ErrorCode.INTERNAL_ERROR, "invalid SSE payload")
        if _contains_internal_scope(payload_dict):
            raise ContractViolation(ErrorCode.INTERNAL_ERROR, "SSE payload contains internal scope")
        envelope = SSEEnvelope(
            run_id=self.run_id,
            trace_id=self.trace_id,
            seq=len(self._events) + 1,
            emitted_at=self._clock(),
            type=event_type,
            payload=payload_dict,
        )
        # Persist before publishing the event to the in-process replay list.
        # A durable sink failure therefore cannot make the API claim that an
        # event was accepted when the normalized answer ledger rejected it.
        if self._event_sink is not None:
            self._event_sink(envelope)
        self._events.append(envelope)
        return envelope

    def accepted(self) -> SSEEnvelope:
        if self._events:
            raise ContractViolation(ErrorCode.CONFLICT, "accepted event already emitted")
        return self._append(SSEEventType.ACCEPTED, SSEAcceptedPayload())

    def phase(
        self,
        phase: Literal["understanding", "searching", "generating", "saving"],
        label: str,
    ) -> SSEEnvelope:
        self.status = AnswerRunStatus.RUNNING
        return self._append(SSEEventType.STATUS, SSEStatusPayload(phase=phase, label=label))

    def start_tool(self, call_id: UUID, name: ToolName) -> SSEEnvelope:
        self._ensure_open()
        if not self._events or self._events[0].type is not SSEEventType.ACCEPTED:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "accepted must be emitted first")
        if self._verified_evidence_ids:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "tool calls must precede evidence")
        if call_id in self._started_tools or call_id in self._finished_tools:
            raise ContractViolation(ErrorCode.CONFLICT, "tool call may not be retried in flight")
        self._started_tools[call_id] = name
        self.status = AnswerRunStatus.RUNNING
        return self._append(SSEEventType.TOOL_STARTED, SSEToolStartedPayload(call_id=call_id, name=name))

    def finish_tool(
        self,
        call_id: UUID,
        name: ToolName,
        status: LiteralStatus,
        *,
        result_ref: str | None = None,
        error_code: ErrorCode | None = None,
    ) -> SSEEnvelope:
        self._ensure_open()
        if call_id not in self._started_tools or call_id in self._finished_tools:
            raise ContractViolation(ErrorCode.CONFLICT, "tool call is not in flight")
        if self._started_tools[call_id] is not name:
            raise ContractViolation(ErrorCode.CONFLICT, "tool call name does not match started call")
        self._finished_tools.add(call_id)
        return self._append(
            SSEEventType.TOOL_FINISHED,
            SSEToolFinishedPayload(
                call_id=call_id,
                name=name,
                status=status,
                result_ref=result_ref,
                error_code=error_code,
            ),
        )

    def add_verified_evidence(self, token: VerifiedEvidenceToken) -> SSEEnvelope:
        self._ensure_open()
        if not self._events or self._events[0].type is not SSEEventType.ACCEPTED:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "accepted must be emitted first")
        if not isinstance(token, VerifiedEvidenceToken) or token._proof is not _VERIFIED_TOKEN_PROOF:
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "evidence proof is invalid")
        if token.run_id != self.run_id:
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "evidence belongs to another answer run")
        if self._scope_key is not None and token.scope_key != self._scope_key:
            raise ContractViolation(ErrorCode.FORBIDDEN, "evidence belongs to another scope")
        values = list(dict.fromkeys(token.evidence_ids))
        if not values:
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "evidence event cannot be empty")
        if not self._started_tools:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "evidence must follow a tool call")
        if set(self._started_tools) != self._finished_tools:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "all started tools must finish before evidence")
        self._verified_evidence_ids.update(values)
        self.status = AnswerRunStatus.RUNNING
        return self._append(SSEEventType.EVIDENCE, SSEEvidencePayload(evidence_ids=values))

    def evidence(self, token: VerifiedEvidenceToken) -> SSEEnvelope:
        """Compatibility name that still accepts only an issued proof token."""

        return self.add_verified_evidence(token)

    def answer_delta(self, text_delta: str) -> SSEEnvelope:
        if self.evidence_required and not self._verified_evidence_ids:
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "answer delta requires evidence first")
        if set(self._started_tools) != self._finished_tools:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "all started tools must finish first")
        self.status = AnswerRunStatus.RUNNING
        return self._append(SSEEventType.ANSWER_DELTA, SSEAnswerDeltaPayload(text_delta=text_delta))

    def complete(
        self,
        *,
        answer_id: UUID,
        conversation_id: UUID,
        evidence_ids: Iterable[UUID] | None = None,
        usage: dict[str, int] | None = None,
    ) -> SSEEnvelope:
        ids = list(dict.fromkeys(self._verified_evidence_ids if evidence_ids is None else evidence_ids))
        if self.evidence_required and not ids:
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "completed answer requires evidence")
        if not set(ids).issubset(self._verified_evidence_ids):
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "completed answer cites unverified evidence")
        if set(self._started_tools) != self._finished_tools:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "all started tools must finish first")
        envelope = self._append(
            SSEEventType.COMPLETED,
            SSECompletedPayload(
                answer_id=answer_id,
                conversation_id=conversation_id,
                evidence_ids=ids,
                usage=usage or {},
            ),
        )
        self._terminal = SSEEventType.COMPLETED
        self.status = AnswerRunStatus.COMPLETED
        return envelope

    def fail(self, violation: ContractViolation | ErrorCode, message: str | None = None) -> SSEEnvelope:
        if self._terminal is not None:
            return self._events[-1]
        if isinstance(violation, ContractViolation):
            error = make_error_body(violation, request_id=self.request_id)
        else:
            error = ErrorBody(
                code=violation,
                message=message or "answer run failed",
                request_id=self.request_id,
                retryable=False,
            )
        envelope = self._append(SSEEventType.FAILED, SSEFailedPayload(error=error))
        self._terminal = SSEEventType.FAILED
        self.status = AnswerRunStatus.FAILED
        return envelope

    def fail_unhandled(self, exc: Exception) -> SSEEnvelope:
        """Convert ordinary handler exceptions to failed, never cancelled."""

        return self.fail(ErrorCode.INTERNAL_ERROR, "answer run failed")

    def cancel(self, reason: str = "已停止生成") -> SSEEnvelope:
        if self._terminal is not None:
            return self._events[-1]
        envelope = self._append(SSEEventType.CANCELLED, SSECancelledPayload(reason=reason))
        self._terminal = SSEEventType.CANCELLED
        self.status = AnswerRunStatus.CANCELLED
        return envelope

    def heartbeat(self) -> SSEEnvelope:
        return self._append(SSEEventType.HEARTBEAT, SSEHeartbeatPayload())

    def replay_after(self, last_event_id: int | None) -> tuple[SSEEnvelope, ...]:
        if last_event_id is None:
            return self.events
        if last_event_id < 0:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "Last-Event-ID must be non-negative")
        return tuple(event for event in self._events if event.seq > last_event_id)

    def close(self) -> None:
        """Record only a connection close; never close the business run ledger."""

        self._connection_closed = True


LiteralStatus = Literal["succeeded", "failed", "disabled"]


def apply_progress_update(
    *,
    scope: ScopeContext,
    current: ReadingProgress,
    update: ProgressUpdate,
    expected_row_version: int,
) -> ReadingProgress:
    assert_scope_identity(scope, current, require_chapter=True)
    if current.row_version != expected_row_version:
        raise ContractViolation(ErrorCode.VERSION_CONFLICT, "reading progress version conflict")
    if update.chapter_id != scope.chapter_id or update.position.chapter_id != scope.chapter_id:
        raise ContractViolation(ErrorCode.FORBIDDEN, "progress is outside the authorized chapter")
    if update.position.block_offset < 0:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "progress offset must be non-negative")
    return ReadingProgress(
        book_id=current.book_id,
        user_id=current.user_id,
        book_version_id=current.book_version_id,
        chapter_id=current.chapter_id,
        last_chunk_index=update.last_chunk_index,
        furthest_chunk_index=max(current.furthest_chunk_index, update.furthest_chunk_index),
        position=update.position,
        updated_at=utc_now(),
        row_version=current.row_version + 1,
    )


def validate_highlight(
    *,
    scope: ScopeContext,
    anchor: HighlightAnchor,
    blocks: Iterable[Block],
) -> HighlightAnchor:
    """Rebuild a quote from independently read trusted Block bodies.

    Cross-block coordinates use a single newline separator, which is the
    stable server representation for a block boundary.  The client quote and
    hash are assertions about this reconstructed value, never its source.
    """

    try:
        assert_scope_identity(scope, anchor, require_chapter=True)
        trusted = tuple(blocks)
        by_id = {block.block_id: block for block in trusted}
        if len(by_id) != len(trusted):
            raise ContractViolation(ErrorCode.ANCHOR_INVALID, "trusted block readback is ambiguous")
        for block in trusted:
            if (
                block.user_id != scope.user_id
                or block.book_id != scope.book_id
                or block.book_version_id != scope.book_version_id
                or block.chapter_id != scope.chapter_id
                or sha256_text(block.text) != block.text_sha256
            ):
                raise ContractViolation(ErrorCode.ANCHOR_INVALID, "trusted block is outside the authorized scope")
        ordinals = [block.ordinal for block in trusted]
        if len(ordinals) != len(set(ordinals)):
            raise ContractViolation(ErrorCode.ANCHOR_INVALID, "trusted block ordinals are ambiguous")
        start_block = by_id.get(anchor.start.block_id)
        end_block = by_id.get(anchor.end.block_id)
        if start_block is None or end_block is None:
            raise ContractViolation(ErrorCode.ANCHOR_INVALID, "highlight endpoint block is unavailable")
        if anchor.start.offset > len(start_block.text) or anchor.end.offset > len(end_block.text):
            raise ContractViolation(ErrorCode.ANCHOR_INVALID, "highlight endpoint is out of bounds")
        if start_block.ordinal > end_block.ordinal:
            raise ContractViolation(ErrorCode.ANCHOR_INVALID, "highlight end precedes start")
        if start_block.block_id == end_block.block_id and anchor.end.offset < anchor.start.offset:
            raise ContractViolation(ErrorCode.ANCHOR_INVALID, "highlight end precedes start")
        if start_block.block_id == end_block.block_id:
            reconstructed = start_block.text[anchor.start.offset : anchor.end.offset]
        else:
            middle = sorted(
                (
                    block
                    for block in trusted
                    if start_block.ordinal < block.ordinal < end_block.ordinal
                ),
                key=lambda block: block.ordinal,
            )
            if [block.ordinal for block in middle] != list(range(start_block.ordinal + 1, end_block.ordinal)):
                raise ContractViolation(ErrorCode.ANCHOR_INVALID, "trusted block range is incomplete")
            reconstructed = "\n".join(
                [start_block.text[anchor.start.offset :], *(block.text for block in middle), end_block.text[: anchor.end.offset]]
            )
        if reconstructed != anchor.exact_quote:
            raise ContractViolation(ErrorCode.ANCHOR_INVALID, "highlight quote does not match trusted blocks")
        if sha256_text(reconstructed) != anchor.text_sha256:
            raise ContractViolation(ErrorCode.ANCHOR_INVALID, "highlight quote hash mismatch")
    except ContractViolation:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(ErrorCode.ANCHOR_INVALID, "highlight anchor is invalid") from exc
    return anchor


def redact_trace_fields(values: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist trace/log fields; never serialize prompts, tokens, or bodies."""

    allowed = {
        "trace_id",
        "run_id",
        "request_id",
        "started_at",
        "finished_at",
        "model_name",
        "tool_call_count",
        "error_code",
        "status",
        "duration_ms",
        "evidence_count",
        "pipeline_version",
    }
    return {key: value for key, value in values.items() if key in allowed}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class DeletionPort(Protocol):
    def tombstone(self, scope: ScopeContext) -> None: ...

    def revoke_access(self, scope: ScopeContext) -> None: ...

    def clear_read_material(self, scope: ScopeContext) -> None: ...


def delete_book_fail_closed(*, scope: ScopeContext, repository: DeletionPort, request_cancel: Callable[[], None]) -> None:
    """Tombstone/revoke synchronously, then schedule cancellation and purge."""

    repository.tombstone(scope)
    repository.revoke_access(scope)
    request_cancel()
    repository.clear_read_material(scope)
