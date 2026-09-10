"""Stable, strict public and internal contract models for Stage 04.

The models in this module are intentionally boring.  They are the boundary
between HTTP, jobs, tools, and the future persistence implementation.  A
``ScopeContext`` is an internal authorization value and is never used as a
request, response, or tool-argument model.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, ClassVar, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


class StrictModel(BaseModel):
    """The common v2 contract configuration.

    ``strict`` is complemented by explicit UUID/time validators below.  This
    keeps Python-side tests honest while JSON HTTP input still receives the
    normal Pydantic JSON decoding rules.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_assignment=True,
        validate_default=True,
    )

    @field_validator("*", mode="after")
    @classmethod
    def _aware_datetimes_only(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("datetime must be timezone-aware")
        return value


class FrozenStrictModel(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        validate_assignment=True,
        validate_default=True,
    )


class BookFormat(str, Enum):
    PDF = "pdf"
    EPUB = "epub"
    TXT = "txt"
    MARKDOWN = "markdown"


class BookVersionStatus(str, Enum):
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class JobType(str, Enum):
    IMPORT_BOOK = "import_book"
    DELETE_BOOK = "delete_book"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class JobStage(str, Enum):
    VALIDATE = "validate"
    STAGE_OBJECT = "stage_object"
    PARSE = "parse"
    BUILD_BLOCKS = "build_blocks"
    BUILD_CHUNKS = "build_chunks"
    EMBED = "embed"
    VERIFY = "verify"
    PUBLISH = "publish"
    PURGE = "purge"


class AnswerRunStatus(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DialogueRoute(str, Enum):
    READER_ACTION = "reader_action"
    BOOK_DIALOGUE = "book_dialogue"
    OPEN_DIALOGUE = "open_dialogue"
    CLARIFICATION = "clarification"


class DialogueRelation(str, Enum):
    NEW = "new"
    FOLLOWUP = "followup"
    CONFUSED = "confused"
    CORRECTION = "correction"


class DialogueGoal(str, Enum):
    EXPLAIN = "explain"
    EXAMPLE = "example"
    SUMMARIZE = "summarize"
    ANALYZE_ARGUMENT = "analyze_argument"
    COMPARE = "compare"
    CRITIQUE = "critique"


class ContextSource(str, Enum):
    SELECTION = "selection"
    CURRENT_VIEW = "current_view"
    PREVIOUS_TURN = "previous_turn"
    NONE = "none"


class ToolName(str, Enum):
    SEARCH_BOOK = "search_book"
    READ_BOOK_BLOCKS = "read_book_blocks"
    GET_BOOK_STRUCTURE = "get_book_structure"
    SEARCH_READING_MEMORY = "search_reading_memory"
    WEB_SEARCH = "web_search"
    READ_WEB_SOURCE = "read_web_source"


class ToolCallStatus(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DISABLED = "disabled"


class SSEEventType(str, Enum):
    ACCEPTED = "accepted"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    EVIDENCE = "evidence"
    ANSWER_DELTA = "answer_delta"
    COMPLETED = "completed"
    FAILED = "failed"
    HEARTBEAT = "heartbeat"


class ErrorCode(str, Enum):
    UNAUTHENTICATED = "unauthenticated"
    CSRF_FAILED = "csrf_failed"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"
    VERSION_CONFLICT = "version_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_FORMAT = "unsupported_format"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    BOOK_NOT_READY = "book_not_ready"
    INVALID_JOB_STATE = "invalid_job_state"
    ANCHOR_INVALID = "anchor_invalid"
    TOOL_DISABLED = "tool_disabled"
    TOOL_TIMEOUT = "tool_timeout"
    EVIDENCE_REQUIRED = "evidence_required"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"


UTCDateTime: TypeAlias = datetime
NonEmptyText = Annotated[str, StringConstraints(min_length=1)]
Hash256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
ShortToken = Annotated[str, StringConstraints(min_length=1, max_length=256)]
OpaqueSourceId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]+$", min_length=1, max_length=256)]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _strict_uuid(value: UUID) -> UUID:
    if not isinstance(value, UUID):
        raise ValueError("UUID value required")
    return value


class ScopeContext(FrozenStrictModel):
    """Server-derived authorization scope; never accept this from clients."""

    session_id: UUID
    user_id: UUID
    book_id: UUID
    book_version_id: UUID
    chapter_id: UUID | None = None
    furthest_chunk_index: int | None = Field(default=None, ge=0)
    request_id: UUID
    trace_id: UUID

    _scope_uuids: ClassVar[tuple[str, ...]] = (
        "session_id",
        "user_id",
        "book_id",
        "book_version_id",
        "chapter_id",
        "request_id",
        "trace_id",
    )

    @field_validator(*_scope_uuids, mode="before")
    @classmethod
    def _require_uuid_objects(cls, value: Any) -> Any:
        if value is None:
            return None
        return _strict_uuid(value)


class SessionView(StrictModel):
    session_id: UUID
    user_id: UUID
    expires_at: UTCDateTime


class SessionLogin(StrictModel):
    identifier: NonEmptyText
    password: NonEmptyText


class SourceLocator(StrictModel):
    kind: Literal["page", "href", "line", "offset", "synthetic"]
    value: NonEmptyText


class Book(StrictModel):
    book_id: UUID
    user_id: UUID
    title: NonEmptyText
    format: BookFormat
    active_version_id: UUID | None = None
    status: Literal["active", "deleting", "deleted"] = "active"
    created_at: UTCDateTime
    row_version: int = Field(ge=1)


class BookVersion(StrictModel):
    book_version_id: UUID
    book_id: UUID
    user_id: UUID
    file_sha256: Hash256
    pipeline_version: NonEmptyText
    status: BookVersionStatus
    created_at: UTCDateTime
    published_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def _published_state(self) -> "BookVersion":
        if self.status is BookVersionStatus.READY and self.published_at is None:
            raise ValueError("ready version requires published_at")
        if self.status is not BookVersionStatus.READY and self.published_at is not None:
            raise ValueError("only ready version may have published_at")
        return self


class Chapter(StrictModel):
    chapter_id: UUID
    book_id: UUID
    user_id: UUID
    book_version_id: UUID
    ordinal: int = Field(ge=0)
    title: NonEmptyText
    source_locator: SourceLocator


class Block(StrictModel):
    block_id: UUID
    chapter_id: UUID
    book_id: UUID
    user_id: UUID
    book_version_id: UUID
    ordinal: int = Field(ge=0)
    text: NonEmptyText
    text_sha256: Hash256
    source_locator: SourceLocator

    @model_validator(mode="after")
    def _hash_matches_text(self) -> "Block":
        if self.text_sha256 != _sha256_text(self.text):
            raise ValueError("block text hash mismatch")
        return self


class Chunk(StrictModel):
    chunk_id: UUID
    chapter_id: UUID
    book_id: UUID
    user_id: UUID
    book_version_id: UUID
    chunk_index: int = Field(ge=0)
    block_ids: list[UUID] = Field(min_length=1, max_length=100)
    text_sha256: Hash256
    token_count: int = Field(ge=0)
    embedding_model: NonEmptyText
    chunker_version: NonEmptyText

    @field_validator("block_ids")
    @classmethod
    def _unique_ordered_blocks(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("chunk block_ids must be unique and ordered")
        return value


class ReadingPosition(StrictModel):
    chapter_id: UUID
    block_id: UUID
    block_offset: int = Field(ge=0)
    updated_at: UTCDateTime
    device_id: NonEmptyText
    row_version: int = Field(ge=1)


class ReadingProgress(StrictModel):
    book_id: UUID
    user_id: UUID
    book_version_id: UUID
    chapter_id: UUID
    last_chunk_index: int = Field(ge=0)
    furthest_chunk_index: int = Field(ge=0)
    position: ReadingPosition
    updated_at: UTCDateTime
    row_version: int = Field(ge=1)

    @model_validator(mode="after")
    def _furthest_not_before_last(self) -> "ReadingProgress":
        if self.last_chunk_index > self.furthest_chunk_index:
            raise ValueError("last progress cannot exceed furthest progress")
        return self


class ProgressUpdate(StrictModel):
    chapter_id: UUID
    last_chunk_index: int = Field(ge=0)
    furthest_chunk_index: int = Field(ge=0)
    position: ReadingPosition
    device_id: NonEmptyText

    @model_validator(mode="after")
    def _update_consistency(self) -> "ProgressUpdate":
        if self.last_chunk_index > self.furthest_chunk_index:
            raise ValueError("last progress cannot exceed furthest progress")
        if self.position.chapter_id != self.chapter_id:
            raise ValueError("position chapter mismatch")
        return self


class HighlightEndpoint(StrictModel):
    block_id: UUID
    offset: int = Field(ge=0)


class HighlightAnchor(StrictModel):
    highlight_id: UUID
    user_id: UUID
    book_id: UUID
    book_version_id: UUID
    chapter_id: UUID
    start: HighlightEndpoint
    end: HighlightEndpoint
    exact_quote: NonEmptyText
    prefix: str = Field(max_length=512)
    suffix: str = Field(max_length=512)
    text_sha256: Hash256
    orphaned: bool = False
    created_at: UTCDateTime

    @model_validator(mode="after")
    def _same_block_order_if_local(self) -> "HighlightAnchor":
        if self.start.block_id == self.end.block_id and self.end.offset < self.start.offset:
            raise ValueError("highlight end precedes start")
        return self


class HighlightCreate(StrictModel):
    """Client highlight payload; server supplies the composite identity."""

    chapter_id: UUID
    start: HighlightEndpoint
    end: HighlightEndpoint
    exact_quote: NonEmptyText
    prefix: str = Field(max_length=512)
    suffix: str = Field(max_length=512)
    text_sha256: Hash256


class EvidenceRef(StrictModel):
    evidence_id: UUID
    user_id: UUID
    book_id: UUID
    book_version_id: UUID
    chapter_id: UUID
    chunk_id: UUID
    chunk_index: int = Field(ge=0)
    block_ids: list[UUID] = Field(min_length=1, max_length=100)
    quote: NonEmptyText
    content_sha256: Hash256
    source_locator: SourceLocator

    @field_validator("block_ids")
    @classmethod
    def _evidence_blocks_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("evidence block_ids must be unique")
        return value


class EvidenceBundle(StrictModel):
    refs: list[EvidenceRef] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _deduplicate(self) -> "EvidenceBundle":
        ids = [ref.evidence_id for ref in self.refs]
        if len(set(ids)) != len(ids):
            raise ValueError("evidence refs must be deduplicated")
        return self


class QuestionCreate(StrictModel):
    question: NonEmptyText = Field(max_length=8000)
    highlight_id: UUID | None = None
    current_chapter_id: UUID | None = None
    conversation_id: UUID | None = None
    client_request_id: UUID


class IntentFrame(StrictModel):
    route: DialogueRoute
    relation: DialogueRelation
    goals: list[DialogueGoal] = Field(default_factory=list, max_length=4)
    context_sources: list[ContextSource] = Field(min_length=1, max_length=4)


class BookUploadRequest(StrictModel):
    """The only public upload fields; bytes/hash/size are server-derived."""

    title: NonEmptyText = Field(max_length=300)
    format: BookFormat


class BookCreateResponse(StrictModel):
    book_id: UUID
    job_id: UUID


class JobAccepted(StrictModel):
    job_id: UUID


class BookView(StrictModel):
    book_id: UUID
    title: NonEmptyText
    format: BookFormat
    active_version_id: UUID | None = None
    status: Literal["active", "deleting", "deleted"]
    created_at: UTCDateTime
    row_version: int = Field(ge=1)


class BookPage(StrictModel):
    items: list[BookView]


class JobView(StrictModel):
    job_id: UUID
    book_id: UUID
    type: JobType
    status: JobStatus
    stage: JobStage
    attempts: int = Field(ge=0, le=3)
    created_at: UTCDateTime
    updated_at: UTCDateTime
    row_version: int = Field(ge=1)
    error_code: ErrorCode | None = None


class JobRetryRequest(StrictModel):
    reason: str = Field(default="manual_retry", max_length=120)


class ChapterList(StrictModel):
    items: list[Chapter]
    next_cursor: str | None = None


class BlockList(StrictModel):
    items: list[Block]
    next_cursor: str | None = None


class HighlightPage(StrictModel):
    items: list[HighlightAnchor]
    next_cursor: str | None = None


class AnswerHistoryItem(StrictModel):
    run_id: UUID
    trace_id: UUID
    question: NonEmptyText
    answer: str
    status: AnswerRunStatus
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=20)
    created_at: UTCDateTime
    finished_at: UTCDateTime | None = None
    conversation_id: UUID | None = None
    intent: IntentFrame | None = None


class AnswerHistoryPage(StrictModel):
    items: list[AnswerHistoryItem]


class TraceView(StrictModel):
    trace_id: UUID
    run_id: UUID
    request_id: UUID
    started_at: UTCDateTime
    finished_at: UTCDateTime | None = None
    model_name: str | None = None
    tool_call_count: int = Field(ge=0)
    error_code: ErrorCode | None = None


class AnswerRunView(StrictModel):
    run_id: UUID
    book_id: UUID
    status: AnswerRunStatus
    trace_id: UUID
    created_at: UTCDateTime
    conversation_id: UUID
    intent: IntentFrame


class ErrorBody(StrictModel):
    code: ErrorCode
    message: NonEmptyText
    request_id: UUID
    retryable: bool = False
    details: dict[str, str | int | bool] | None = None


class ErrorEnvelope(StrictModel):
    error: ErrorBody


class JobRecord(StrictModel):
    job_id: UUID
    user_id: UUID
    book_id: UUID
    type: JobType
    status: JobStatus = JobStatus.QUEUED
    stage: JobStage = JobStage.VALIDATE
    attempts: int = Field(default=0, ge=0, le=3)
    lease_owner: str | None = None
    lease_expires_at: UTCDateTime | None = None
    heartbeat_at: UTCDateTime | None = None
    idempotency_key: NonEmptyText
    input_sha256: Hash256
    pipeline_version: NonEmptyText
    checkpoint: dict[str, str | int | bool] = Field(default_factory=dict)
    cancel_requested: bool = False
    retryable: bool = False
    error_code: ErrorCode | None = None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    row_version: int = Field(default=1, ge=1)


class ToolError(StrictModel):
    code: ErrorCode
    message: NonEmptyText


class SearchBookArgs(StrictModel):
    query: NonEmptyText = Field(max_length=2000)
    top_k: int = Field(default=5, ge=1, le=10)
    chapter_ids: list[UUID] | None = Field(default=None, max_length=100)


class ReadBookBlocksArgs(StrictModel):
    block_ids: list[UUID] = Field(min_length=1, max_length=20)
    context_before: int = Field(default=0, ge=0, le=2)
    context_after: int = Field(default=0, ge=0, le=2)


class GetBookStructureArgs(StrictModel):
    chapter_cursor: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=50, ge=1, le=100)


class SearchReadingMemoryArgs(StrictModel):
    query: NonEmptyText = Field(max_length=2000)
    kinds: list[Literal["highlight", "note", "answer"]] | None = Field(default=None, max_length=3)
    limit: int = Field(default=5, ge=1, le=10)


class WebSearchArgs(StrictModel):
    query: NonEmptyText = Field(max_length=2000)
    allowed_domains: list[NonEmptyText] | None = Field(default=None, max_length=20)
    limit: int = Field(default=5, ge=1, le=5)


class ReadWebSourceArgs(StrictModel):
    source_id: OpaqueSourceId


class EvidenceCandidate(StrictModel):
    evidence_id: UUID
    chapter_id: UUID
    chunk_index: int = Field(ge=0)
    quote: NonEmptyText
    score: float = Field(ge=0)
    source_locator: SourceLocator


class SearchBookOutput(StrictModel):
    items: list[EvidenceCandidate] = Field(max_length=20)


class BlockExcerpt(StrictModel):
    block_id: UUID
    text: NonEmptyText
    source_locator: SourceLocator


class ReadBookBlocksOutput(StrictModel):
    items: list[BlockExcerpt] = Field(min_length=1, max_length=20)


class ChapterSummary(StrictModel):
    chapter_id: UUID
    ordinal: int = Field(ge=0)
    title: NonEmptyText


class GetBookStructureOutput(StrictModel):
    items: list[ChapterSummary]
    next_cursor: str | None = None


class MemoryHit(StrictModel):
    memory_id: UUID
    kind: Literal["highlight", "note", "answer"]
    excerpt: NonEmptyText
    score: float = Field(ge=0)


class SearchReadingMemoryOutput(StrictModel):
    items: list[MemoryHit] = Field(max_length=20)


class WebSearchHit(StrictModel):
    source_id: OpaqueSourceId
    title: NonEmptyText
    snippet: NonEmptyText


class WebSearchOutput(StrictModel):
    items: list[WebSearchHit] = Field(max_length=5)


class WebSourceExcerpt(StrictModel):
    source_id: OpaqueSourceId
    title: NonEmptyText
    excerpt: NonEmptyText


class ReadWebSourceOutput(StrictModel):
    source: WebSourceExcerpt


ToolOutputData: TypeAlias = (
    SearchBookOutput
    | ReadBookBlocksOutput
    | GetBookStructureOutput
    | SearchReadingMemoryOutput
    | WebSearchOutput
    | ReadWebSourceOutput
)


ToolArgs: TypeAlias = (
    SearchBookArgs
    | ReadBookBlocksArgs
    | GetBookStructureArgs
    | SearchReadingMemoryArgs
    | WebSearchArgs
    | ReadWebSourceArgs
)


class ToolResult(StrictModel):
    call_id: UUID
    name: ToolName
    status: ToolCallStatus
    data: ToolOutputData | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=20)
    error: ToolError | None = None

    @model_validator(mode="after")
    def _result_shape(self) -> "ToolResult":
        if self.status is ToolCallStatus.SUCCEEDED and self.error is not None:
            raise ValueError("successful tool result cannot contain error")
        if self.status in {ToolCallStatus.FAILED, ToolCallStatus.DISABLED} and self.error is None:
            raise ValueError("failed or disabled tool result requires error")
        return self


class SSEAcceptedPayload(StrictModel):
    pass


class SSEToolStartedPayload(StrictModel):
    call_id: UUID
    name: ToolName
    status: Literal["started"] = "started"


class SSEToolFinishedPayload(StrictModel):
    call_id: UUID
    name: ToolName
    status: Literal["succeeded", "failed", "disabled"]
    result_ref: str | None = None
    error_code: ErrorCode | None = None


class SSEEvidencePayload(StrictModel):
    evidence_ids: list[UUID] = Field(min_length=1, max_length=20)


class SSEAnswerDeltaPayload(StrictModel):
    text_delta: NonEmptyText


class SSECompletedPayload(StrictModel):
    answer_id: UUID
    conversation_id: UUID
    evidence_ids: list[UUID] = Field(max_length=20)
    usage: dict[str, int] = Field(default_factory=dict)


class SSEFailedPayload(StrictModel):
    error: ErrorBody


class SSEHeartbeatPayload(StrictModel):
    pass


class SSEEnvelope(StrictModel):
    run_id: UUID
    trace_id: UUID
    seq: int = Field(ge=1)
    emitted_at: UTCDateTime
    type: SSEEventType
    payload: dict[str, Any]


class IdempotencyRecord(StrictModel):
    user_id: UUID
    operation: NonEmptyText
    key: NonEmptyText
    request_hash: Hash256
    book_id: UUID
    job_id: UUID
    created_at: UTCDateTime


class Tombstone(StrictModel):
    user_id: UUID
    book_id: UUID
    deleted_at: UTCDateTime
    revoke_at: UTCDateTime
    purge_after: UTCDateTime

    @model_validator(mode="after")
    def _deletion_order(self) -> "Tombstone":
        if self.revoke_at < self.deleted_at or self.purge_after < self.revoke_at:
            raise ValueError("tombstone timestamps must be ordered")
        return self


TOOL_ARGUMENT_MODELS: dict[ToolName, type[StrictModel]] = {
    ToolName.SEARCH_BOOK: SearchBookArgs,
    ToolName.READ_BOOK_BLOCKS: ReadBookBlocksArgs,
    ToolName.GET_BOOK_STRUCTURE: GetBookStructureArgs,
    ToolName.SEARCH_READING_MEMORY: SearchReadingMemoryArgs,
    ToolName.WEB_SEARCH: WebSearchArgs,
    ToolName.READ_WEB_SOURCE: ReadWebSourceArgs,
}


TOOL_OUTPUT_MODELS: dict[ToolName, type[StrictModel]] = {
    ToolName.SEARCH_BOOK: SearchBookOutput,
    ToolName.READ_BOOK_BLOCKS: ReadBookBlocksOutput,
    ToolName.GET_BOOK_STRUCTURE: GetBookStructureOutput,
    ToolName.SEARCH_READING_MEMORY: SearchReadingMemoryOutput,
    ToolName.WEB_SEARCH: WebSearchOutput,
    ToolName.READ_WEB_SOURCE: ReadWebSourceOutput,
}


def export_tool_schemas() -> dict[str, dict[str, Any]]:
    """Export the six deterministic argument schemas for evidence hashing."""

    return {
        name.value: TOOL_ARGUMENT_MODELS[name].model_json_schema()
        for name in ToolName
    }


PUBLIC_REQUEST_MODELS: tuple[type[BaseModel], ...] = (
    SessionLogin,
    BookUploadRequest,
    QuestionCreate,
    ProgressUpdate,
    JobRetryRequest,
)
