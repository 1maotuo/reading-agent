"""Dependency-inversion ports for the Stage 04 contract draft.

Concrete database, model, object-store, and MCP adapters are intentionally not
part of this package.  Every port receives a server-created scope where data
could be tenant-sensitive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol
from uuid import UUID

from .contracts import (
    Book,
    Block,
    Chapter,
    Chunk,
    EvidenceRef,
    JobRecord,
    ReadingPosition,
    ReadingProgress,
    ScopeContext,
    ToolName,
    ToolResult,
)


@dataclass(frozen=True)
class AuthorizedScopeRecord:
    """A server-read authorization row; callers must not fill it from HTTP."""

    session_id: UUID
    user_id: UUID
    book_id: UUID
    book_version_id: UUID
    chapter_id: UUID | None
    furthest_chunk_index: int | None


class SessionPort(Protocol):
    def authenticate(self, session_token: str) -> Any: ...

    def login(self, identifier: str, password: str) -> Any: ...

    def logout(self, session_token: str) -> None: ...


class AuthorizationPort(Protocol):
    def read_authorized_scope(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
        book_id: UUID,
        book_version_id: UUID,
        chapter_id: UUID | None,
    ) -> AuthorizedScopeRecord | None: ...

    def authorize(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
        book_id: UUID,
        book_version_id: UUID,
        chapter_id: UUID | None,
    ) -> bool: ...


class BookRepositoryPort(Protocol):
    def create_book(self, book: Book) -> Book: ...

    def get_book(self, scope: ScopeContext) -> Book | None: ...

    def list_chapters(self, scope: ScopeContext, cursor: str | None, limit: int) -> tuple[list[Chapter], str | None]: ...

    def list_blocks(self, scope: ScopeContext, chapter_id: UUID, cursor: str | None, limit: int) -> tuple[list[Block], str | None]: ...

    def read_chunks(self, scope: ScopeContext, chunk_ids: list[UUID]) -> list[Chunk]: ...

    def read_blocks_by_ids(self, scope: ScopeContext, block_ids: list[UUID]) -> list[Block]: ...

    def read_evidence(self, scope: ScopeContext, evidence_id: UUID) -> EvidenceRef | None: ...

    def get_progress(self, scope: ScopeContext) -> ReadingProgress | None: ...

    def put_progress(
        self,
        scope: ScopeContext,
        progress: ReadingProgress,
        expected_row_version: int,
    ) -> ReadingProgress: ...


class EvidenceReaderPort(Protocol):
    """Independent server readback used to verify provider evidence."""

    def reread(self, scope: ScopeContext, evidence_id: UUID) -> EvidenceRef | None: ...


class ToolProviderPort(Protocol):
    def call(self, name: ToolName, args: Any, scope: ScopeContext, call_id: UUID) -> ToolResult: ...


class ModelPort(Protocol):
    def generate(self, *, scope: ScopeContext, prompt: str, evidence_ids: list[UUID]) -> Any: ...


class ObjectStorePort(Protocol):
    def delete_book_objects(self, *, scope: ScopeContext) -> None: ...


class JobStorePort(Protocol):
    def get(self, job_id: UUID) -> JobRecord | None: ...

    def save(self, job: JobRecord) -> JobRecord: ...

    def compare_and_swap(self, old: JobRecord, new: JobRecord) -> JobRecord: ...

    def claim_next(self, worker_id: str, *, lease_seconds: int = 30) -> JobRecord | None: ...


class PublishTransactionPort(Protocol):
    def publish_verified_version(self, *, scope: ScopeContext, job: JobRecord) -> JobRecord: ...


class ClockPort(Protocol):
    def now(self) -> datetime: ...


ToolCallable: type = Callable[[Any, ScopeContext, UUID], ToolResult]
