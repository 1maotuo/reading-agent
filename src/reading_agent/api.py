"""Minimal REST/OpenAPI/SSE surface for the Stage 04 contract draft.

The default services are in-memory fakes so the contract can be exercised
without a database or external provider.  They are not a production adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
import base64
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from typing import Any, Callable
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, BackgroundTasks, Body, Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from .contracts import (
    AnswerHistoryItem,
    AnswerHistoryPage,
    AnswerRunStatus,
    AnswerRunView,
    Block,
    BlockList,
    Book,
    BookCreateResponse,
    BookFormat,
    BookPage,
    BookUploadRequest,
    BookVersionStatus,
    BookView,
    Chapter,
    ChapterList,
    ErrorCode,
    ErrorEnvelope,
    EvidenceBundle,
    EvidenceRef,
    HighlightAnchor,
    HighlightCreate,
    HighlightPage,
    JobRecord,
    JobAccepted,
    JobRetryRequest,
    JobStage,
    JobStatus,
    JobType,
    JobView,
    ProgressUpdate,
    ReadingProgress,
    ScopeContext,
    SessionLogin,
    SessionView,
    QuestionCreate,
    DialogueRoute,
    IntentFrame,
    IntentTarget,
    IntentTargetKind,
    SSEEnvelope,
    TraceView,
    Tombstone,
)
from .ports import AuthorizedScopeRecord
from .domain import (
    AnswerEventLedger,
    ContractViolation,
    apply_progress_update,
    assert_scope_identity,
    build_scope,
    canonical_sha256,
    derive_chapter_scope,
    error_status,
    make_error_body,
    sha256_bytes,
    sha256_text,
    utc_now,
    validate_highlight,
)
from .worker import JobController
from .dialogue import HybridIntentRouter
from .companion import CompanionProfiles, CompanionUpdate, CompanionView
from .memory import ReadingMemory, ReadingMemoryStore
from .book_memory import BookLearnerProfileView, BookMemoryStore
from .storage_boundary import PersistenceBoundary


UTC = timezone.utc
SESSION_COOKIE = "ra_session"
CSRF_COOKIE = "ra_csrf"
MAX_UPLOAD_BYTES = 50_000_000
DEFAULT_USER_ID = UUID("00000000-0000-4000-8000-000000000001")


@dataclass
class _SessionState:
    view: SessionView
    csrf: str


@dataclass(frozen=True)
class ParsedUpload:
    """Server-derived upload metadata; no client hash/size fields are trusted."""

    title: str
    format: BookFormat
    file_sha256: str
    byte_size: int
    source_name: str
    file_bytes: bytes


class MemoryAuth:
    """Server-side session/auth fake used only by the contract app."""

    def __init__(self) -> None:
        self.accounts: dict[str, tuple[UUID, str]] = {"reader": (DEFAULT_USER_ID, "contract-password")}
        self.sessions: dict[str, _SessionState] = {}

    def add_account(self, identifier: str, password: str, user_id: UUID | None = None) -> UUID:
        user = user_id or uuid4()
        self.accounts[identifier] = (user, password)
        return user

    def login(self, identifier: str, password: str) -> tuple[str, SessionView, str]:
        account = self.accounts.get(identifier)
        if account is None or not secrets.compare_digest(account[1].encode("utf-8"), password.encode("utf-8")):
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid credentials")
        now = utc_now()
        view = SessionView(session_id=uuid4(), user_id=account[0], expires_at=now + timedelta(hours=8))
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        self.sessions[token] = _SessionState(view=view, csrf=csrf)
        return token, view, csrf

    def authenticate(self, token: str | None) -> SessionView:
        state = self.sessions.get(token or "")
        if state is None or state.view.expires_at <= utc_now():
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "authentication required")
        return state.view

    def csrf_for(self, token: str | None) -> str | None:
        state = self.sessions.get(token or "")
        return state.csrf if state else None

    def validate_csrf(self, token: str | None, csrf: str | None) -> bool:
        expected = self.csrf_for(token)
        return expected is not None and csrf is not None and secrets.compare_digest(expected, csrf)

    def logout(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)


class MemoryBooks:
    """Small repository fake with owner checks and no persistence."""

    def __init__(self) -> None:
        self.books: dict[UUID, Book] = {}
        self.versions: dict[UUID, Any] = {}
        self.chapters: dict[UUID, Chapter] = {}
        self.blocks: dict[UUID, Block] = {}
        self.chunks: dict[UUID, Any] = {}
        self.chunk_texts: dict[UUID, str] = {}
        # Optional retrieval sidecar; absence means BM25-only/legacy content.
        self.chunk_embeddings: dict[UUID, list[float]] = {}
        self.block_chunk_indexes: dict[UUID, int] = {}
        self.evidence: dict[UUID, EvidenceRef] = {}
        self.source_paths: dict[UUID, str] = {}
        self.progress: dict[tuple[UUID, UUID, UUID], ReadingProgress] = {}
        self.highlights: dict[UUID, HighlightAnchor] = {}
        self.cursor_secret = secrets.token_bytes(32)
        self.tombstones: dict[UUID, Tombstone] = {}
        self.deletion_steps: dict[UUID, list[str]] = {}

    def add_book(self, book: Book) -> Book:
        self.books[book.book_id] = book
        return book

    def add_version(self, version: Any) -> None:
        self.versions[version.book_version_id] = version

    def add_chapter(self, chapter: Chapter) -> None:
        self.chapters[chapter.chapter_id] = chapter

    def add_block(self, block: Block) -> None:
        self.blocks[block.block_id] = block

    def add_chunk(self, chunk: Any, text: str, embedding: list[float] | None = None) -> None:
        self.chunks[chunk.chunk_id] = chunk
        self.chunk_texts[chunk.chunk_id] = text
        if embedding is not None:
            self.chunk_embeddings[chunk.chunk_id] = list(embedding)
        for block_id in chunk.block_ids:
            self.block_chunk_indexes[block_id] = chunk.chunk_index

    def read_evidence(self, scope: ScopeContext, evidence_id: UUID) -> EvidenceRef | None:
        evidence = self.evidence.get(evidence_id)
        if evidence is None:
            return None
        if (
            evidence.user_id != scope.user_id
            or evidence.book_id != scope.book_id
            or evidence.book_version_id != scope.book_version_id
            or (scope.chapter_id is not None and evidence.chapter_id != scope.chapter_id)
        ):
            return None
        return evidence

    def get(self, book_id: UUID) -> Book | None:
        return self.books.get(book_id)

    def owned(self, book_id: UUID, user_id: UUID) -> Book | None:
        book = self.books.get(book_id)
        if book is None or book.user_id != user_id or book.status in {"deleting", "deleted"}:
            return None
        return book

    def active_version(self, book: Book) -> UUID:
        if book.active_version_id is None:
            raise ContractViolation(ErrorCode.BOOK_NOT_READY, "book has no ready version")
        version = self.versions.get(book.active_version_id)
        if (
            version is None
            or version.status is not BookVersionStatus.READY
            or version.user_id != book.user_id
            or version.book_id != book.book_id
        ):
            raise ContractViolation(ErrorCode.BOOK_NOT_READY, "book has no ready version")
        return version.book_version_id

    def chapter_for_scope(self, scope: ScopeContext, chapter_id: UUID) -> Chapter:
        chapter = self.chapters.get(chapter_id)
        if chapter is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        try:
            assert_scope_identity(scope, chapter, require_chapter=True)
        except ContractViolation as exc:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found") from exc
        return chapter

    def authorized_chapter(self, *, user_id: UUID, book_id: UUID, book_version_id: UUID, chapter_id: UUID) -> Chapter | None:
        """Read a chapter row and return it only after full composite checks."""

        chapter = self.chapters.get(chapter_id)
        if (
            chapter is None
            or chapter.user_id != user_id
            or chapter.book_id != book_id
            or chapter.book_version_id != book_version_id
        ):
            return None
        return chapter

    def view(self, book: Book) -> BookView:
        return BookView(
            book_id=book.book_id,
            title=book.title,
            format=book.format,
            active_version_id=book.active_version_id,
            status=book.status,
            created_at=book.created_at,
            row_version=book.row_version,
        )

    def _encode_cursor(self, scope: ScopeContext, collection: str, offset: int) -> str:
        payload = json.dumps(
            {
                "v": 1,
                "collection": collection,
                "user_id": str(scope.user_id),
                "book_id": str(scope.book_id),
                "book_version_id": str(scope.book_version_id),
                "chapter_id": str(scope.chapter_id) if scope.chapter_id is not None else None,
                "offset": offset,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
        signature = hmac.new(self.cursor_secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{encoded}.{signature}"

    def _decode_cursor(self, scope: ScopeContext, collection: str, cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            encoded, signature = cursor.split(".", 1)
            expected = hmac.new(self.cursor_secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("bad cursor signature")
            padded = encoded + ("=" * (-len(encoded) % 4))
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
            if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("collection") != collection:
                raise ValueError("bad cursor payload")
            if (
                payload.get("user_id") != str(scope.user_id)
                or payload.get("book_id") != str(scope.book_id)
                or payload.get("book_version_id") != str(scope.book_version_id)
                or payload.get("chapter_id") != (str(scope.chapter_id) if scope.chapter_id is not None else None)
            ):
                raise ValueError("cursor scope mismatch")
            offset = payload.get("offset")
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ValueError("bad cursor offset")
            return offset
        except Exception as exc:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "cursor is invalid") from exc

    def list_chapters(self, scope: ScopeContext, cursor: str | None, limit: int) -> tuple[list[Chapter], str | None]:
        if not 1 <= limit <= 100:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "limit is outside the allowed range")
        values = sorted(
            (
                chapter
                for chapter in self.chapters.values()
                if chapter.user_id == scope.user_id
                and chapter.book_id == scope.book_id
                and chapter.book_version_id == scope.book_version_id
            ),
            key=lambda item: item.ordinal,
        )
        start = self._decode_cursor(scope, "chapters", cursor)
        items = values[start : start + limit]
        next_cursor = self._encode_cursor(scope, "chapters", start + limit) if len(items) == limit else None
        return items, next_cursor

    def list_blocks(self, scope: ScopeContext, chapter_id: UUID, cursor: str | None, limit: int) -> tuple[list[Block], str | None]:
        self.chapter_for_scope(scope, chapter_id)
        if not 1 <= limit <= 100:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "limit is outside the allowed range")
        values = sorted(
            (
                block
                for block in self.blocks.values()
                if block.user_id == scope.user_id
                and block.book_id == scope.book_id
                and block.book_version_id == scope.book_version_id
                and block.chapter_id == chapter_id
            ),
            key=lambda item: item.ordinal,
        )
        start = self._decode_cursor(scope, f"blocks:{chapter_id}", cursor)
        items = values[start : start + limit]
        next_cursor = self._encode_cursor(scope, f"blocks:{chapter_id}", start + limit) if len(items) == limit else None
        return items, next_cursor

    def page_highlights(self, scope: ScopeContext, cursor: str | None, limit: int) -> tuple[list[HighlightAnchor], str | None]:
        if not 1 <= limit <= 100:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "limit is outside the allowed range")
        values = sorted(
            (
                anchor
                for anchor in self.highlights.values()
                if anchor.user_id == scope.user_id
                and anchor.book_id == scope.book_id
                and anchor.book_version_id == scope.book_version_id
            ),
            key=lambda item: (item.created_at, str(item.highlight_id)),
        )
        start = self._decode_cursor(scope, "highlights", cursor)
        items = values[start : start + limit]
        next_cursor = self._encode_cursor(scope, "highlights", start + limit) if len(items) == limit else None
        return items, next_cursor

    def put_progress(self, scope: ScopeContext, update: ProgressUpdate, expected_row_version: int) -> ReadingProgress:
        key = (scope.user_id, scope.book_version_id, update.chapter_id)
        current = self.progress.get(key)
        if current is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "progress not found")
        result = apply_progress_update(
            scope=scope,
            current=current,
            update=update,
            expected_row_version=expected_row_version,
        )
        self.progress[key] = result
        return result


@dataclass
class _AnswerRecord:
    scope: ScopeContext
    question: str
    ledger: AnswerEventLedger
    created_at: datetime
    answer_text: str = ""
    evidence_ids: list[UUID] = field(default_factory=list)
    conversation_id: UUID = field(default_factory=uuid4)
    finished_at: datetime | None = None
    model_name: str | None = None
    tool_call_count: int = 0
    error_code: ErrorCode | None = None
    intent: IntentFrame | None = None
    cancel_requested: bool = False
    trace_details: dict[str, Any] = field(default_factory=dict)


class MemoryAnswers:
    def __init__(self) -> None:
        self.records: dict[UUID, _AnswerRecord] = {}
        # Optional normalized sink selected by the Stage 05 runtime.  The
        # default contract app leaves this unset and remains dependency-free.
        self.durable_store: Any | None = None

    def create(
        self,
        scope: ScopeContext,
        question: str,
        *,
        conversation_id: UUID | None = None,
        intent: IntentFrame | None = None,
    ) -> _AnswerRecord:
        run_id = uuid4()
        conversation = conversation_id or uuid4()
        durable = self.durable_store
        holder: dict[str, _AnswerRecord] = {}

        def persist_event(event: SSEEnvelope) -> None:
            if durable is None:
                return
            durable.append_event(scope, event)
            record = holder.get("record")
            if record is None or event.type.value not in {"completed", "failed", "cancelled"}:
                return
            status = {
                "completed": AnswerRunStatus.COMPLETED,
                "failed": AnswerRunStatus.FAILED,
                "cancelled": AnswerRunStatus.CANCELLED,
            }[event.type.value]
            durable.save_terminal(
                scope=scope,
                run_id=record.ledger.run_id,
                status=status,
                answer_text=record.answer_text,
                evidence_ids=record.evidence_ids,
                conversation_id=record.conversation_id,
                finished_at=record.finished_at or event.emitted_at,
                model_name=record.model_name,
                tool_call_count=record.tool_call_count,
                error_code=record.error_code,
            )

        if durable is not None:
            durable.create_run(
                scope=scope,
                run_id=run_id,
                conversation_id=conversation,
                question=question,
                created_at=utc_now(),
            )
        ledger = AnswerEventLedger(
            run_id=run_id,
            trace_id=scope.trace_id,
            request_id=scope.request_id,
            scope=scope,
            evidence_required=intent is None or intent.route is DialogueRoute.BOOK_DIALOGUE,
            event_sink=persist_event,
        )
        ledger.accepted()
        record = _AnswerRecord(
            scope=scope,
            question=question,
            ledger=ledger,
            created_at=utc_now(),
            conversation_id=conversation,
            intent=intent,
        )
        holder["record"] = record
        self.records[ledger.run_id] = record
        return record


@dataclass
class ApiServices:
    auth: MemoryAuth = field(default_factory=MemoryAuth)
    books: MemoryBooks = field(default_factory=MemoryBooks)
    jobs: JobController = field(default_factory=JobController)
    answers: MemoryAnswers = field(default_factory=MemoryAnswers)
    idempotency: dict[tuple[UUID, str, str], tuple[str, UUID, UUID]] = field(default_factory=dict)
    mutation_idempotency: dict[tuple[UUID, str, str], tuple[str, UUID]] = field(default_factory=dict)
    upload_handler: Any | None = None
    answer_handler: Any | None = None
    dev_mode: bool = False
    dynamic_answer_events: bool = False
    companion: CompanionProfiles = field(default_factory=CompanionProfiles)
    memory: ReadingMemoryStore = field(default_factory=ReadingMemoryStore)
    book_memory: BookMemoryStore = field(default_factory=BookMemoryStore)
    storage_boundary: PersistenceBoundary | None = None


def _services(request: Request) -> ApiServices:
    return request.app.state.services


def get_current_session(request: Request) -> SessionView:
    return _services(request).auth.authenticate(request.cookies.get(SESSION_COOKIE))


def _csrf(request: Request, services: ApiServices) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    validator = getattr(services.auth, "validate_csrf", None)
    if callable(validator):
        if not validator(token, request.headers.get("X-CSRF-Token")):
            raise ContractViolation(ErrorCode.CSRF_FAILED, "CSRF validation failed")
        return
    expected = services.auth.csrf_for(token)
    provided = request.headers.get("X-CSRF-Token")
    if expected is None or provided is None or not secrets.compare_digest(expected, provided):
        raise ContractViolation(ErrorCode.CSRF_FAILED, "CSRF validation failed")


def get_book_scope(book_id: UUID, request: Request, session: SessionView = Depends(get_current_session)) -> ScopeContext:
    services = _services(request)
    book = services.books.owned(book_id, session.user_id)
    if book is None:
        raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
    version_id = services.books.active_version(book)
    try:
        return build_scope(
            session_id=session.session_id,
            user_id=session.user_id,
            book_id=book.book_id,
            book_version_id=version_id,
            request_id=uuid4(),
            trace_id=uuid4(),
            authorization=_BookAuthorization(services.books),
            expires_at=session.expires_at,
        )
    except ContractViolation as exc:
        if exc.code is ErrorCode.FORBIDDEN:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found") from exc
        raise


def get_chapter_scope(
    book_id: UUID,
    chapter_id: UUID,
    request: Request,
    session: SessionView = Depends(get_current_session),
) -> ScopeContext:
    services = _services(request)
    base = get_book_scope(book_id, request, session)
    return derive_chapter_scope(
        parent_scope=base,
        chapter_id=chapter_id,
        authorization=_BookAuthorization(services.books),
    )


def _validate_question_context(services: ApiServices, scope: ScopeContext, payload: QuestionCreate) -> None:
    """Validate all client-supplied question context before any handler runs."""

    if payload.highlight_id is not None and payload.selection_context is not None:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "一次只能附加一个原文上下文")
    if payload.current_chapter_id is not None:
        chapter = services.books.authorized_chapter(
            user_id=scope.user_id,
            book_id=scope.book_id,
            book_version_id=scope.book_version_id,
            chapter_id=payload.current_chapter_id,
        )
        if chapter is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
    if payload.highlight_id is not None:
        anchor = services.books.highlights.get(payload.highlight_id)
        if (
            anchor is None
            or anchor.user_id != scope.user_id
            or anchor.book_id != scope.book_id
            or anchor.book_version_id != scope.book_version_id
            or services.books.authorized_chapter(
                user_id=scope.user_id,
                book_id=scope.book_id,
                book_version_id=scope.book_version_id,
                chapter_id=anchor.chapter_id,
            )
            is None
        ):
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
    if payload.selection_context is not None:
        selection = payload.selection_context
        chapter = services.books.authorized_chapter(
            user_id=scope.user_id,
            book_id=scope.book_id,
            book_version_id=scope.book_version_id,
            chapter_id=selection.chapter_id,
        )
        block = services.books.blocks.get(selection.block_id)
        if (
            chapter is None
            or block is None
            or block.user_id != scope.user_id
            or block.book_id != scope.book_id
            or block.book_version_id != scope.book_version_id
            or block.chapter_id != selection.chapter_id
            or selection.end_offset > len(block.text)
            or block.text[selection.start_offset : selection.end_offset] != selection.exact_quote
            or sha256_text(selection.exact_quote) != selection.text_sha256
        ):
            raise ContractViolation(ErrorCode.ANCHOR_INVALID, "所选原文已经变化，请重新选择")


class _BookAuthorization:
    def __init__(self, books: MemoryBooks) -> None:
        self.books = books

    def read_authorized_scope(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
        book_id: UUID,
        book_version_id: UUID,
        chapter_id: UUID | None,
    ) -> AuthorizedScopeRecord | None:
        """Perform the trusted server readback used to construct ScopeContext."""

        book = self.books.owned(book_id, user_id)
        if book is None:
            return None
        try:
            active_version = self.books.active_version(book)
        except ContractViolation:
            return None
        if active_version != book_version_id:
            return None
        furthest: int | None = None
        trusted_chapter: UUID | None = None
        if chapter_id is not None:
            chapter = self.books.authorized_chapter(
                user_id=user_id,
                book_id=book_id,
                book_version_id=book_version_id,
                chapter_id=chapter_id,
            )
            if chapter is None:
                return None
            trusted_chapter = chapter.chapter_id
            progress = self.books.progress.get((user_id, book_version_id, trusted_chapter))
            if progress is not None:
                furthest = progress.furthest_chunk_index
        return AuthorizedScopeRecord(
            session_id=session_id,
            user_id=book.user_id,
            book_id=book.book_id,
            book_version_id=active_version,
            chapter_id=trusted_chapter,
            furthest_chunk_index=furthest,
        )

    def authorize(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
        book_id: UUID,
        book_version_id: UUID,
        chapter_id: UUID | None,
    ) -> bool:
        return self.read_authorized_scope(
            session_id=session_id,
            user_id=user_id,
            book_id=book_id,
            book_version_id=book_version_id,
            chapter_id=chapter_id,
        ) is not None


def _job_view(job: JobRecord) -> JobView:
    return JobView(
        job_id=job.job_id,
        book_id=job.book_id,
        type=job.type,
        status=job.status,
        stage=job.stage,
        attempts=job.attempts,
        created_at=job.created_at,
        updated_at=job.updated_at,
        row_version=job.row_version,
        error_code=job.error_code,
    )


def _sse_frame(event: SSEEnvelope) -> str:
    payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.type.value}\ndata: {payload}\n\n"


def _http_error(violation: ContractViolation, request: Request) -> JSONResponse:
    body = ErrorEnvelope(error=make_error_body(violation))
    return JSONResponse(status_code=violation.status_code, content=body.model_dump(mode="json"))


async def _decode_json_body(request: Request, model: type[Any]) -> Any:
    """Use Pydantic's JSON path so strict Python models accept valid JSON scalars."""

    try:
        return model.model_validate_json(await request.body())
    except (ValidationError, ValueError) as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "request validation failed") from exc


async def _decode_upload_body(request: Request) -> ParsedUpload:
    """Accept only multipart bytes and derive hash/size on the server."""

    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("multipart/form-data"):
        raise ContractViolation(ErrorCode.INVALID_INPUT, "multipart file upload is required")
    raw = await request.body()
    if len(raw) > MAX_UPLOAD_BYTES + 1_000_000:
        raise ContractViolation(ErrorCode.PAYLOAD_TOO_LARGE, "upload exceeds the contract limit")
    envelope = (
        f"Content-Type: {request.headers.get('content-type')}\r\n"
        "MIME-Version: 1.0\r\n\r\n"
    ).encode("ascii") + raw
    message = BytesParser(policy=policy.default).parsebytes(envelope)
    if not message.is_multipart():
        raise ContractViolation(ErrorCode.INVALID_INPUT, "multipart body is invalid")
    fields: dict[str, str] = {}
    file_bytes: bytes | None = None
    source_name: str | None = None
    file_count = 0
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        value = part.get_payload(decode=True) or b""
        filename = part.get_param("filename", header="content-disposition")
        if name == "file" and filename is not None:
            file_count += 1
            if file_count > 1:
                raise ContractViolation(ErrorCode.INVALID_INPUT, "multipart upload contains multiple files")
            file_bytes = value
            source_name = str(filename)
        elif filename is not None or name not in {"title", "format"}:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "multipart upload contains an unsupported field")
        else:
            if name in fields:
                raise ContractViolation(ErrorCode.INVALID_INPUT, "multipart upload repeats a field")
            try:
                fields[name] = value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ContractViolation(ErrorCode.INVALID_INPUT, "multipart upload metadata is invalid") from exc
    if file_bytes is None or source_name is None or file_count != 1 or not fields.get("title") or not fields.get("format"):
        raise ContractViolation(ErrorCode.INVALID_INPUT, "multipart upload requires title, format, and file")
    if fields["format"] not in {item.value for item in BookFormat}:
        raise ContractViolation(ErrorCode.UNSUPPORTED_FORMAT, "book format is not supported")
    if len(file_bytes) == 0:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "file must not be empty")
    try:
        payload = BookUploadRequest.model_validate_json(
            json.dumps({"title": fields["title"], "format": fields["format"]})
        )
    except (ValidationError, ValueError) as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "multipart upload metadata is invalid") from exc
    return ParsedUpload(
        title=payload.title,
        format=payload.format,
        file_sha256=sha256_bytes(file_bytes),
        byte_size=len(file_bytes),
        source_name=source_name,
        file_bytes=file_bytes,
    )


def create_app(services: ApiServices | None = None) -> FastAPI:
    app = FastAPI(title="Reading Agent Stage 04 Contract", version="0.1.0-contract", docs_url=None, redoc_url=None)
    app.state.services = services or ApiServices()

    @app.exception_handler(ContractViolation)
    async def _contract_error(request: Request, exc: ContractViolation) -> JSONResponse:
        return _http_error(exc, request)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        violation = ContractViolation(ErrorCode.INVALID_INPUT, "request validation failed")
        return _http_error(violation, request)

    @app.exception_handler(Exception)
    async def _ordinary_error(request: Request, exc: Exception) -> JSONResponse:
        # No stack or exception text crosses the API boundary.  A worker/event
        # ledger is responsible for recording an answer-run failed terminal.
        return _http_error(ContractViolation(ErrorCode.INTERNAL_ERROR, "internal error"), request)

    router = APIRouter(prefix="/api/v1")

    @router.post("/sessions", response_model=SessionView, status_code=200)
    async def create_session(payload: SessionLogin, response: Response, request: Request) -> SessionView:
        try:
            token, view, csrf = app.state.services.auth.login(payload.identifier, payload.password)
        except ContractViolation:
            # Same status/body shape for unknown identifier and wrong password.
            raise
        secure_cookie = not app.state.services.dev_mode
        response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=secure_cookie, samesite="lax")
        response.set_cookie(CSRF_COOKIE, csrf, httponly=False, secure=secure_cookie, samesite="lax")
        return view

    @router.get("/session", response_model=SessionView)
    async def read_session(session: SessionView = Depends(get_current_session)) -> SessionView:
        return session

    @router.delete("/session", status_code=204)
    async def delete_session(request: Request, session: SessionView = Depends(get_current_session)) -> Response:
        _csrf(request, app.state.services)
        app.state.services.auth.logout(request.cookies.get(SESSION_COOKIE))
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(CSRF_COOKIE)
        return response

    @router.get("/books/{book_id}/companion", response_model=CompanionView)
    async def read_companion(
        book_id: UUID,
        session: SessionView = Depends(get_current_session),
    ) -> CompanionView:
        book = app.state.services.books.owned(book_id, session.user_id)
        if book is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        return app.state.services.companion.view(user_id=session.user_id, book_id=book_id)

    @router.put("/books/{book_id}/companion", response_model=CompanionView)
    async def update_companion(
        book_id: UUID,
        request: Request,
        payload: dict[str, Any] = Body(...),
        session: SessionView = Depends(get_current_session),
    ) -> CompanionView:
        _csrf(request, app.state.services)
        book = app.state.services.books.owned(book_id, session.user_id)
        if book is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        update = await _decode_json_body(request, CompanionUpdate)
        return app.state.services.companion.update(user_id=session.user_id, book_id=book_id, payload=update)

    @router.get("/books/{book_id}/memories", response_model=list[ReadingMemory])
    async def list_reading_memories(
        book_id: UUID,
        session: SessionView = Depends(get_current_session),
    ) -> list[ReadingMemory]:
        book = app.state.services.books.owned(book_id, session.user_id)
        if book is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        return app.state.services.memory.list_for_book(user_id=session.user_id, book_id=book_id)

    @router.get("/books/{book_id}/book-memory", response_model=BookLearnerProfileView)
    async def read_book_memory(
        book_id: UUID,
        session: SessionView = Depends(get_current_session),
    ) -> BookLearnerProfileView:
        book = app.state.services.books.owned(book_id, session.user_id)
        if book is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        version_id = app.state.services.books.active_version(book)
        return app.state.services.book_memory.build_profile_view(
            user_id=session.user_id,
            book_id=book_id,
            book_version_id=version_id,
        )

    @router.delete("/books/{book_id}/book-memory")
    async def clear_book_memory(
        book_id: UUID,
        request: Request,
        session: SessionView = Depends(get_current_session),
    ) -> dict[str, int]:
        _csrf(request, app.state.services)
        book = app.state.services.books.owned(book_id, session.user_id)
        if book is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        return {
            "deleted": app.state.services.book_memory.clear_scope(
                user_id=session.user_id, book_id=book_id
            )
        }

    @router.delete("/books/{book_id}/memories")
    async def clear_reading_memories(
        book_id: UUID,
        request: Request,
        session: SessionView = Depends(get_current_session),
    ) -> dict[str, int]:
        _csrf(request, app.state.services)
        book = app.state.services.books.owned(book_id, session.user_id)
        if book is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        deleted = app.state.services.memory.clear_book(user_id=session.user_id, book_id=book_id)
        deleted += app.state.services.book_memory.clear_scope(
            user_id=session.user_id, book_id=book_id
        )
        return {"deleted": deleted}

    @router.post(
        "/books",
        response_model=BookCreateResponse,
        status_code=202,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["title", "format", "file"],
                            "properties": {
                                "title": {"type": "string"},
                                "format": {"type": "string", "enum": [item.value for item in BookFormat]},
                                "file": {"type": "string", "format": "binary"},
                            },
                        }
                    },
                },
            }
        },
    )
    async def create_book(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        session: SessionView = Depends(get_current_session),
    ) -> BookCreateResponse:
        _csrf(request, app.state.services)
        payload = await _decode_upload_body(request)
        if not idempotency_key:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "Idempotency-Key is required")
        if payload.byte_size > MAX_UPLOAD_BYTES:
            raise ContractViolation(ErrorCode.PAYLOAD_TOO_LARGE, "upload exceeds the contract limit")
        body_hash = canonical_sha256(
            {
                "title": payload.title,
                "format": payload.format.value,
                "file_sha256": payload.file_sha256,
                "byte_size": payload.byte_size,
            }
        )
        key = (session.user_id, "import_book", idempotency_key)
        previous = app.state.services.idempotency.get(key)
        if previous is not None:
            old_hash, old_book_id, old_job_id = previous
            if old_hash != body_hash:
                raise ContractViolation(ErrorCode.IDEMPOTENCY_CONFLICT, "idempotency key was reused with different input")
            return BookCreateResponse(book_id=old_book_id, job_id=old_job_id)
        now = utc_now()
        book = Book(
            book_id=uuid4(),
            user_id=session.user_id,
            title=payload.title,
            format=payload.format,
            created_at=now,
            row_version=1,
        )
        job = JobRecord(
            job_id=uuid4(),
            user_id=session.user_id,
            book_id=book.book_id,
            type=JobType.IMPORT_BOOK,
            idempotency_key=idempotency_key,
            input_sha256=payload.file_sha256,
            pipeline_version="stage04-contract-1",
            created_at=now,
            updated_at=now,
        )
        boundary = app.state.services.storage_boundary
        if boundary is not None and boundary.normalized_adapters_ready:
            boundary.create_book(book)
        app.state.services.books.add_book(book)
        app.state.services.jobs.add(job)
        app.state.services.idempotency[key] = (body_hash, book.book_id, job.job_id)
        if app.state.services.upload_handler is not None:
            app.state.services.upload_handler.import_book(
                services=app.state.services,
                payload=payload,
                book=book,
                job=job,
            )
        return BookCreateResponse(book_id=book.book_id, job_id=job.job_id)

    @router.get("/books", response_model=BookPage)
    async def list_books(session: SessionView = Depends(get_current_session)) -> BookPage:
        items = [
            app.state.services.books.view(book)
            for book in app.state.services.books.books.values()
            if book.user_id == session.user_id and book.status != "deleted"
        ]
        items.sort(key=lambda item: item.created_at, reverse=True)
        return BookPage(items=items)

    @router.get("/books/{book_id}", response_model=BookView)
    async def read_book(book_id: UUID, session: SessionView = Depends(get_current_session)) -> BookView:
        book = app.state.services.books.owned(book_id, session.user_id)
        if book is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        return app.state.services.books.view(book)

    @router.delete("/books/{book_id}", response_model=JobAccepted, status_code=202)
    async def delete_book(
        book_id: UUID,
        request: Request,
        if_match: int | None = Header(default=None, alias="If-Match"),
        session: SessionView = Depends(get_current_session),
    ) -> JobAccepted:
        _csrf(request, app.state.services)
        book = app.state.services.books.owned(book_id, session.user_id)
        if book is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        if if_match is None or if_match != book.row_version:
            raise ContractViolation(ErrorCode.VERSION_CONFLICT, "If-Match does not match book version")
        services = app.state.services
        now = utc_now()
        tombstone = Tombstone(
            user_id=session.user_id,
            book_id=book_id,
            deleted_at=now,
            revoke_at=now,
            purge_after=now + timedelta(days=1),
        )
        services.books.tombstones[book_id] = tombstone
        services.books.deletion_steps[book_id] = ["tombstone", "revoke"]
        services.books.deletion_steps[book_id].append("cancel")
        for existing in services.jobs.list_for_book(session.user_id, book_id):
            if (
                existing.user_id == session.user_id
                and existing.book_id == book_id
                and existing.status not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
            ):
                services.jobs.request_cancel(existing.job_id)
        services.books.deletion_steps[book_id].append("purge")
        services.books.books[book_id] = book.model_copy(update={"status": "deleting", "row_version": book.row_version + 1})
        job = JobRecord(
            job_id=uuid4(),
            user_id=session.user_id,
            book_id=book_id,
            type=JobType.DELETE_BOOK,
            stage=JobStage.PURGE,
            idempotency_key=f"delete:{book_id}:{now.timestamp()}",
            input_sha256=sha256_text(str(book_id)),
            pipeline_version="stage04-contract-1",
            checkpoint={"tombstone": True, "revoke": True, "cancel": True, "purge": False},
            created_at=now,
            updated_at=now,
        )
        services.jobs.add(job)
        return JobAccepted(job_id=job.job_id)

    @router.get("/jobs/{job_id}", response_model=JobView)
    async def read_job(job_id: UUID, session: SessionView = Depends(get_current_session)) -> JobView:
        job = app.state.services.jobs.get(job_id)
        if job.user_id != session.user_id:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        return _job_view(job)

    @router.post("/jobs/{job_id}/retry", response_model=JobView, status_code=202)
    async def retry_job(
        job_id: UUID,
        payload: JobRetryRequest,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        session: SessionView = Depends(get_current_session),
    ) -> JobView:
        _csrf(request, app.state.services)
        job = app.state.services.jobs.get(job_id)
        if job.user_id != session.user_id:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        if not idempotency_key:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "Idempotency-Key is required")
        body_hash = canonical_sha256({"job_id": str(job_id), "reason": payload.reason})
        key = (session.user_id, "retry_job", idempotency_key)
        previous = app.state.services.mutation_idempotency.get(key)
        if previous is not None:
            old_hash, old_id = previous
            if old_hash != body_hash or old_id != job_id:
                raise ContractViolation(ErrorCode.IDEMPOTENCY_CONFLICT, "idempotency key was reused with different input")
            return _job_view(app.state.services.jobs.get(job_id))
        if job.status is not JobStatus.FAILED or not job.retryable:
            raise ContractViolation(ErrorCode.INVALID_JOB_STATE, "job is not retryable")
        job = app.state.services.jobs.retry(job_id)
        app.state.services.mutation_idempotency[key] = (body_hash, job_id)
        return _job_view(job)

    @router.post("/jobs/{job_id}/cancel", response_model=JobView, status_code=202)
    async def cancel_job(
        job_id: UUID,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        session: SessionView = Depends(get_current_session),
    ) -> JobView:
        _csrf(request, app.state.services)
        job = app.state.services.jobs.get(job_id)
        if job.user_id != session.user_id:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        if not idempotency_key:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "Idempotency-Key is required")
        body_hash = canonical_sha256({"job_id": str(job_id), "operation": "cancel"})
        key = (session.user_id, "cancel_job", idempotency_key)
        previous = app.state.services.mutation_idempotency.get(key)
        if previous is not None:
            old_hash, old_id = previous
            if old_hash != body_hash or old_id != job_id:
                raise ContractViolation(ErrorCode.IDEMPOTENCY_CONFLICT, "idempotency key was reused with different input")
            return _job_view(app.state.services.jobs.get(job_id))
        result = app.state.services.jobs.request_cancel(job_id)
        app.state.services.mutation_idempotency[key] = (body_hash, job_id)
        return _job_view(result)

    @router.get("/books/{book_id}/chapters", response_model=ChapterList)
    async def list_chapters(
        book_id: UUID,
        request: Request,
        cursor: str | None = None,
        limit: int = 50,
        scope: ScopeContext = Depends(get_book_scope),
    ) -> ChapterList:
        items, next_cursor = app.state.services.books.list_chapters(scope, cursor, limit)
        return ChapterList(items=items, next_cursor=next_cursor)

    @router.get("/books/{book_id}/chapters/{chapter_id}/blocks", response_model=BlockList)
    async def list_blocks(
        book_id: UUID,
        chapter_id: UUID,
        request: Request,
        cursor: str | None = None,
        limit: int = 100,
        scope: ScopeContext = Depends(get_chapter_scope),
    ) -> BlockList:
        items, next_cursor = app.state.services.books.list_blocks(scope, chapter_id, cursor, limit)
        return BlockList(items=items, next_cursor=next_cursor)

    @router.get("/books/{book_id}/progress", response_model=ReadingProgress)
    async def get_progress(
        book_id: UUID,
        chapter_id: UUID,
        request: Request,
        scope: ScopeContext = Depends(get_chapter_scope),
    ) -> ReadingProgress:
        boundary = app.state.services.storage_boundary
        if boundary is not None and boundary.progress_storage_ready:
            result = boundary.get_progress(scope)
        else:
            key = (scope.user_id, scope.book_version_id, chapter_id)
            result = app.state.services.books.progress.get(key)
        if result is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "progress not found")
        return result

    @router.put("/books/{book_id}/progress", response_model=ReadingProgress)
    async def put_progress(
        book_id: UUID,
        request: Request,
        payload: dict[str, Any] = Body(...),
        if_match: int | None = Header(default=None, alias="If-Match"),
        session: SessionView = Depends(get_current_session),
    ) -> ReadingProgress:
        _csrf(request, app.state.services)
        payload = await _decode_json_body(request, ProgressUpdate)
        if if_match is None:
            raise ContractViolation(ErrorCode.VERSION_CONFLICT, "If-Match is required")
        # The body chapter is an untrusted target; it is authorized against
        # the server-owned book/version before a scope is constructed.
        book = app.state.services.books.owned(book_id, session.user_id)
        if book is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        version_id = app.state.services.books.active_version(book)
        root_scope = build_scope(
            session_id=session.session_id,
            user_id=session.user_id,
            book_id=book_id,
            book_version_id=version_id,
            request_id=uuid4(),
            trace_id=uuid4(),
            authorization=_BookAuthorization(app.state.services.books),
            expires_at=session.expires_at,
        )
        scope = derive_chapter_scope(
            parent_scope=root_scope,
            chapter_id=payload.chapter_id,
            authorization=_BookAuthorization(app.state.services.books),
        )
        boundary = app.state.services.storage_boundary
        if boundary is not None and boundary.progress_storage_ready:
            progress = ReadingProgress(
                book_id=scope.book_id,
                user_id=scope.user_id,
                book_version_id=scope.book_version_id,
                chapter_id=payload.chapter_id,
                last_chunk_index=payload.last_chunk_index,
                furthest_chunk_index=payload.furthest_chunk_index,
                position=payload.position,
                updated_at=utc_now(),
                row_version=max(1, if_match),
            )
            result = boundary.put_progress(scope, progress, if_match)
            # Keep scope construction and same-process reads aligned with the
            # durable optimistic-lock result while PostgreSQL remains the
            # source of truth for progress.
            app.state.services.books.progress[(scope.user_id, scope.book_version_id, payload.chapter_id)] = result
            return result
        return app.state.services.books.put_progress(scope, payload, if_match)

    @router.get("/books/{book_id}/highlights", response_model=HighlightPage)
    async def list_highlights(
        book_id: UUID,
        cursor: str | None = None,
        limit: int = 50,
        scope: ScopeContext = Depends(get_book_scope),
    ) -> HighlightPage:
        items, next_cursor = app.state.services.books.page_highlights(scope, cursor, limit)
        return HighlightPage(items=items, next_cursor=next_cursor)

    @router.post(
        "/books/{book_id}/highlights",
        response_model=HighlightAnchor,
        status_code=201,
        openapi_extra={"requestBody": {"content": {"application/json": {"schema": HighlightCreate.model_json_schema()}}}},
    )
    async def create_highlight(
        book_id: UUID,
        request: Request,
        payload: dict[str, Any] = Body(...),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        scope: ScopeContext = Depends(get_book_scope),
    ) -> HighlightAnchor:
        _csrf(request, app.state.services)
        payload = await _decode_json_body(request, HighlightCreate)
        if not idempotency_key:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "Idempotency-Key is required")
        chapter_scope = derive_chapter_scope(
            parent_scope=scope,
            chapter_id=payload.chapter_id,
            authorization=_BookAuthorization(app.state.services.books),
        )
        blocks, _ = app.state.services.books.list_blocks(chapter_scope, payload.chapter_id, None, 100)
        anchor = HighlightAnchor(
            highlight_id=uuid4(),
            user_id=scope.user_id,
            book_id=scope.book_id,
            book_version_id=scope.book_version_id,
            chapter_id=payload.chapter_id,
            start=payload.start,
            end=payload.end,
            exact_quote=payload.exact_quote,
            prefix=payload.prefix,
            suffix=payload.suffix,
            text_sha256=payload.text_sha256,
            created_at=utc_now(),
        )
        validate_highlight(scope=chapter_scope, anchor=anchor, blocks=blocks)
        body_hash = canonical_sha256(payload.model_dump(mode="json"))
        key = (scope.user_id, "create_highlight", idempotency_key)
        previous = app.state.services.mutation_idempotency.get(key)
        if previous is not None:
            old_hash, old_id = previous
            if old_hash != body_hash or old_id not in app.state.services.books.highlights:
                raise ContractViolation(ErrorCode.IDEMPOTENCY_CONFLICT, "idempotency key was reused with different input")
            return app.state.services.books.highlights[old_id]
        app.state.services.books.highlights[anchor.highlight_id] = anchor
        app.state.services.mutation_idempotency[key] = (body_hash, anchor.highlight_id)
        return anchor

    @router.delete("/books/{book_id}/highlights/{highlight_id}", status_code=204)
    async def delete_highlight(
        book_id: UUID,
        highlight_id: UUID,
        request: Request,
        session: SessionView = Depends(get_current_session),
    ) -> Response:
        _csrf(request, app.state.services)
        book = app.state.services.books.owned(book_id, session.user_id)
        anchor = app.state.services.books.highlights.get(highlight_id)
        if book is None or anchor is None or anchor.user_id != session.user_id or anchor.book_id != book_id:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        del app.state.services.books.highlights[highlight_id]
        return Response(status_code=204)

    @router.post(
        "/books/{book_id}/questions",
        response_model=AnswerRunView,
        status_code=202,
        openapi_extra={"requestBody": {"content": {"application/json": {"schema": QuestionCreate.model_json_schema()}}}},
    )
    async def create_question(
        book_id: UUID,
        request: Request,
        background_tasks: BackgroundTasks,
        payload: dict[str, Any] = Body(...),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        scope: ScopeContext = Depends(get_book_scope),
    ) -> AnswerRunView:
        _csrf(request, app.state.services)
        payload = await _decode_json_body(request, QuestionCreate)
        if not idempotency_key:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "Idempotency-Key is required")
        answer_services = app.state.services
        _validate_question_context(answer_services, scope, payload)
        candidate_records = [
            record
            for record in answer_services.answers.records.values()
            if record.scope.user_id == scope.user_id
            and record.scope.book_id == scope.book_id
            and record.scope.book_version_id == scope.book_version_id
            and record.ledger.status is AnswerRunStatus.COMPLETED
        ]
        conversation_records: list[_AnswerRecord] = []
        if payload.conversation_id is not None:
            conversation_records = [
                record for record in candidate_records if record.conversation_id == payload.conversation_id
            ]
            if not conversation_records:
                # Deliberately do not reveal whether the identifier belongs to
                # another user, book, or old document version.
                raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        else:
            conversation_records = []
        previous_route = None
        previous_intent = None
        previous_question = None
        previous_answer = None
        if conversation_records:
            previous = max(conversation_records, key=lambda item: item.created_at)
            previous_route = previous.intent.route if previous.intent is not None else DialogueRoute.BOOK_DIALOGUE
            previous_intent = previous.intent
            previous_question = previous.question
            previous_answer = previous.answer_text
        explicit_target = None
        if payload.selection_context is not None:
            explicit_target = IntentTarget(
                kind=IntentTargetKind.SELECTION,
                identifier=str(payload.selection_context.block_id),
                explicit=True,
            )
        elif payload.highlight_id is not None:
            explicit_target = IntentTarget(
                kind=IntentTargetKind.HIGHLIGHT,
                identifier=str(payload.highlight_id),
                explicit=True,
            )
        semantic_model = getattr(answer_services.answer_handler, "model", None)
        intent = await asyncio.to_thread(
            HybridIntentRouter(semantic_model).classify,
            payload.question,
            has_selection=payload.highlight_id is not None or payload.selection_context is not None,
            has_current_view=payload.current_chapter_id is not None,
            previous_route=previous_route,
            explicit_target=explicit_target,
            current_chapter_id=str(payload.current_chapter_id) if payload.current_chapter_id else None,
            previous_intent=previous_intent,
            previous_question=previous_question,
            previous_answer=previous_answer,
        )
        body_hash = canonical_sha256(payload.model_dump(mode="json"))
        key = (scope.user_id, "create_question", idempotency_key)
        previous = app.state.services.mutation_idempotency.get(key)
        if previous is not None:
            old_hash, old_run_id = previous
            if old_hash != body_hash or old_run_id not in app.state.services.answers.records:
                raise ContractViolation(ErrorCode.IDEMPOTENCY_CONFLICT, "idempotency key was reused with different input")
            old_record = app.state.services.answers.records[old_run_id]
            return AnswerRunView(
                run_id=old_record.ledger.run_id,
                book_id=old_record.scope.book_id,
                status=old_record.ledger.status,
                trace_id=old_record.scope.trace_id,
                created_at=old_record.created_at,
                conversation_id=old_record.conversation_id,
                intent=old_record.intent or intent,
            )
        if app.state.services.answer_handler is not None:
            scope = app.state.services.answer_handler.resolve_scope(
                services=app.state.services,
                root_scope=scope,
                payload=payload,
                intent=intent,
            )
        record = app.state.services.answers.create(
            scope,
            payload.question,
            conversation_id=payload.conversation_id,
            intent=intent,
        )
        app.state.services.mutation_idempotency[key] = (body_hash, record.ledger.run_id)
        if app.state.services.answer_handler is not None:
            background_tasks.add_task(
                app.state.services.answer_handler.run,
                services=app.state.services,
                record=record,
                payload=payload,
            )
        return AnswerRunView(
            run_id=record.ledger.run_id,
            book_id=scope.book_id,
            status=record.ledger.status,
            trace_id=scope.trace_id,
            created_at=record.created_at,
            conversation_id=record.conversation_id,
            intent=intent,
        )

    @router.get("/answer-runs/{run_id}/events")
    async def answer_events(
        run_id: UUID,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        session: SessionView = Depends(get_current_session),
    ) -> StreamingResponse:
        record = app.state.services.answers.records.get(run_id)
        if record is None or record.scope.user_id != session.user_id:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        try:
            last = int(last_event_id) if last_event_id is not None else None
        except ValueError as exc:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "Last-Event-ID is invalid") from exc
        async def stream() -> Any:
            cursor = last or 0
            idle_ticks = 0
            while True:
                events = record.ledger.replay_after(cursor)
                for event in events:
                    cursor = event.seq
                    idle_ticks = 0
                    yield _sse_frame(event)
                if record.ledger.terminal is not None or not app.state.services.dynamic_answer_events:
                    break
                idle_ticks += 1
                if idle_ticks >= 600:
                    break
                await asyncio.sleep(0.1)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @router.post("/answer-runs/{run_id}/cancel", response_model=AnswerRunView, status_code=202)
    async def cancel_answer_run(
        run_id: UUID,
        request: Request,
        session: SessionView = Depends(get_current_session),
    ) -> AnswerRunView:
        _csrf(request, app.state.services)
        record = app.state.services.answers.records.get(run_id)
        if record is None or record.scope.user_id != session.user_id:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        if record.ledger.terminal is None:
            handler = app.state.services.answer_handler
            if handler is not None and hasattr(handler, "cancel"):
                handler.cancel(record)
            else:
                record.cancel_requested = True
            record.ledger.cancel()
            record.finished_at = datetime.now(timezone.utc)
        return AnswerRunView(
            run_id=record.ledger.run_id,
            book_id=record.scope.book_id,
            status=record.ledger.status,
            trace_id=record.scope.trace_id,
            created_at=record.created_at,
            conversation_id=record.conversation_id,
            intent=record.intent or HybridIntentRouter().classify(record.question),
        )

    @router.get("/answer-runs/{run_id}/evidence", response_model=EvidenceBundle)
    async def read_answer_evidence(
        run_id: UUID,
        session: SessionView = Depends(get_current_session),
    ) -> EvidenceBundle:
        record = app.state.services.answers.records.get(run_id)
        if record is None or record.scope.user_id != session.user_id:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        refs = [
            evidence
            for evidence_id in record.evidence_ids
            if (evidence := app.state.services.books.read_evidence(record.scope, evidence_id)) is not None
        ]
        if not refs:
            raise ContractViolation(ErrorCode.EVIDENCE_REQUIRED, "answer has no verified evidence")
        return EvidenceBundle(refs=refs)

    @router.get("/books/{book_id}/answers", response_model=AnswerHistoryPage)
    async def list_answer_history(
        book_id: UUID,
        session: SessionView = Depends(get_current_session),
    ) -> AnswerHistoryPage:
        book = app.state.services.books.owned(book_id, session.user_id)
        if book is None:
            raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")
        records = [
            record
            for record in app.state.services.answers.records.values()
            if record.scope.user_id == session.user_id and record.scope.book_id == book_id
        ]
        records.sort(key=lambda item: item.created_at)
        return AnswerHistoryPage(
            items=[
                AnswerHistoryItem(
                    run_id=record.ledger.run_id,
                    trace_id=record.scope.trace_id,
                    question=record.question,
                    answer=record.answer_text,
                    status=record.ledger.status,
                    evidence=[
                        evidence
                        for evidence_id in record.evidence_ids
                        if (evidence := app.state.services.books.read_evidence(record.scope, evidence_id)) is not None
                    ],
                    created_at=record.created_at,
                    finished_at=record.finished_at,
                    conversation_id=record.conversation_id,
                    intent=record.intent,
                )
                for record in records
            ]
        )

    @router.get("/traces/{trace_id}", response_model=TraceView)
    async def read_trace(trace_id: UUID, session: SessionView = Depends(get_current_session)) -> TraceView:
        for record in app.state.services.answers.records.values():
            if record.scope.trace_id == trace_id and record.scope.user_id == session.user_id:
                return TraceView(
                    trace_id=trace_id,
                    run_id=record.ledger.run_id,
                    request_id=record.scope.request_id,
                    started_at=record.created_at,
                    finished_at=record.finished_at,
                    model_name=record.model_name,
                    tool_call_count=record.tool_call_count,
                    error_code=record.error_code,
                    intent=record.intent,
                    context=dict(record.trace_details.get("context", {})),
                    stream_mode=record.trace_details.get("stream_mode"),
                    memory_hits=int(record.trace_details.get("memory_hits", 0)),
                    skill_version=record.trace_details.get("skill_version"),
                )
        raise ContractViolation(ErrorCode.NOT_FOUND, "resource not found")

    app.include_router(router)
    original_openapi = app.openapi

    def contract_openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = original_openapi()
            components = schema.setdefault("components", {}).setdefault("schemas", {})
            # Body adapters use the JSON validation path so strict Python
            # models retain strictness while valid JSON UUID/enums work.  Keep
            # named public models in OpenAPI for client generation.
            for model in (
                SessionLogin,
                BookUploadRequest,
                QuestionCreate,
                ProgressUpdate,
                HighlightCreate,
                JobRetryRequest,
                JobAccepted,
                HighlightPage,
            ):
                components.setdefault(model.__name__, model.model_json_schema())
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = contract_openapi  # type: ignore[method-assign]
    return app


app = create_app()


def export_openapi(app_instance: FastAPI = app) -> dict[str, Any]:
    """Return a fresh OpenAPI object; callers hash canonical JSON."""

    app_instance.openapi_schema = None
    return app_instance.openapi()


def sse_frame_for_test(event: SSEEnvelope) -> str:
    """Public test helper without exposing a ScopeContext in OpenAPI."""

    return _sse_frame(event)
