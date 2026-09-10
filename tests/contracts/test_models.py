from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from reading_agent.contracts import (
    Block,
    Book,
    BookFormat,
    BookVersion,
    BookVersionStatus,
    Chapter,
    Chunk,
    ErrorCode,
    EvidenceBundle,
    EvidenceRef,
    HighlightAnchor,
    HighlightEndpoint,
    ProgressUpdate,
    ReadingPosition,
    ReadingProgress,
    ScopeContext,
    SourceLocator,
)
from reading_agent.domain import (
    ContractViolation,
    apply_progress_update,
    assert_block_consistency,
    assert_book_version_consistency,
    assert_chapter_consistency,
    assert_chunk_consistency,
    assert_scope_identity,
    chunk_body_sha256,
    redact_trace_fields,
    sha256_text,
    validate_highlight,
    validate_chunk_block_readback,
)


UTC = timezone.utc


def uid() -> UUID:
    return uuid4()


def scope(*, chapter_id: UUID | None = None, furthest: int | None = 4) -> ScopeContext:
    return ScopeContext(
        session_id=uid(),
        user_id=uid(),
        book_id=uid(),
        book_version_id=uid(),
        chapter_id=chapter_id,
        furthest_chunk_index=furthest,
        request_id=uid(),
        trace_id=uid(),
    )


def locator() -> SourceLocator:
    return SourceLocator(kind="synthetic", value="contract")


def test_c01_strict_extra_uuid_time_and_frozen_scope() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        Book(
            book_id=uid(),
            user_id=uid(),
            title="book",
            format="pdf",  # strict enum does not accept a Python string
            created_at=now,
            row_version=1,
        )
    with pytest.raises(ValidationError):
        Book.model_validate(
            {
                "book_id": str(uid()),
                "user_id": uid(),
                "title": "book",
                "format": "pdf",
                "created_at": now,
                "row_version": 1,
                "unexpected": True,
            }
        )
    with pytest.raises(ValidationError):
        ReadingPosition(
            chapter_id=uid(),
            block_id=uid(),
            block_offset="0",
            updated_at=now,
            device_id="device",
            row_version=1,
        )
    with pytest.raises(ValidationError):
        SessionViewForTest = ReadingPosition(
            chapter_id=uid(),
            block_id=uid(),
            block_offset=0,
            updated_at=datetime.now(),
            device_id="device",
            row_version=1,
        )
        del SessionViewForTest
    current = scope()
    with pytest.raises((ValidationError, TypeError)):
        current.user_id = uid()


def test_c03_composite_identity_is_checked_across_entities() -> None:
    chapter_id = uid()
    good = scope(chapter_id=chapter_id)
    version = BookVersion(
        book_version_id=good.book_version_id,
        book_id=good.book_id,
        user_id=good.user_id,
        file_sha256=sha256_text("input"),
        pipeline_version="p1",
        status=BookVersionStatus.READY,
        created_at=datetime.now(UTC),
        published_at=datetime.now(UTC),
    )
    book = Book(
        book_id=good.book_id,
        user_id=good.user_id,
        title="book",
        format=BookFormat.TXT,
        active_version_id=good.book_version_id,
        created_at=datetime.now(UTC),
        row_version=1,
    )
    assert_book_version_consistency(book, version)
    wrong_version = version.model_copy(update={"book_id": uid()})
    with pytest.raises(ContractViolation):
        assert_book_version_consistency(book, wrong_version)
    chapter = Chapter(
        chapter_id=chapter_id,
        book_id=good.book_id,
        user_id=good.user_id,
        book_version_id=good.book_version_id,
        ordinal=0,
        title="Chapter",
        source_locator=locator(),
    )
    assert_chapter_consistency(chapter, version)
    wrong_chapter = chapter.model_copy(update={"book_version_id": uid()})
    with pytest.raises(ContractViolation):
        assert_chapter_consistency(wrong_chapter, version)
    block = Block(
        block_id=uid(),
        chapter_id=chapter_id,
        book_id=good.book_id,
        user_id=good.user_id,
        book_version_id=good.book_version_id,
        ordinal=0,
        text="canonical text",
        text_sha256=sha256_text("canonical text"),
        source_locator=locator(),
    )
    assert_block_consistency(block, chapter)
    with pytest.raises(ContractViolation):
        assert_scope_identity(good, block.model_copy(update={"user_id": uid()}), require_chapter=True)
    # Chunk -> Block is an independent trusted readback, not a global block-id
    # lookup.  Order, composite identity, body hash, and completeness all bind.
    second_block = Block(
        block_id=uid(),
        chapter_id=chapter_id,
        book_id=good.book_id,
        user_id=good.user_id,
        book_version_id=good.book_version_id,
        ordinal=1,
        text="second canonical text",
        text_sha256=sha256_text("second canonical text"),
        source_locator=locator(),
    )
    chunk = Chunk(
        chunk_id=uid(),
        chapter_id=chapter_id,
        book_id=good.book_id,
        user_id=good.user_id,
        book_version_id=good.book_version_id,
        chunk_index=0,
        block_ids=[block.block_id, second_block.block_id],
        text_sha256=chunk_body_sha256([block, second_block]),
        token_count=4,
        embedding_model="none",
        chunker_version="c1",
    )
    assert_chunk_consistency(chunk, chapter)
    with pytest.raises(ContractViolation):
        assert_chunk_consistency(chunk.model_copy(update={"chapter_id": uid()}), chapter)
    assert validate_chunk_block_readback(scope=good, chunk=chunk, trusted_blocks=[block, second_block]) == (block, second_block)
    with pytest.raises(ContractViolation):
        validate_chunk_block_readback(scope=good, chunk=chunk, trusted_blocks=[second_block, block])
    with pytest.raises(ContractViolation):
        validate_chunk_block_readback(scope=good, chunk=chunk, trusted_blocks=[block])
    with pytest.raises(ContractViolation):
        validate_chunk_block_readback(
            scope=good,
            chunk=chunk,
            trusted_blocks=[block, second_block.model_copy(update={"book_version_id": uid()})],
        )
    with pytest.raises(ContractViolation):
        validate_chunk_block_readback(
            scope=good,
            chunk=chunk,
            trusted_blocks=[block, second_block.model_copy(update={"text": "tampered"})],
        )


def test_c08_ready_version_and_nonnegative_stable_ordinals() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        BookVersion(
            book_version_id=uid(),
            book_id=uid(),
            user_id=uid(),
            file_sha256=sha256_text("input"),
            pipeline_version="p1",
            status=BookVersionStatus.READY,
            created_at=now,
        )
    with pytest.raises(ValidationError):
        Chapter(
            chapter_id=uid(),
            book_id=uid(),
            user_id=uid(),
            book_version_id=uid(),
            ordinal=-1,
            title="bad",
            source_locator=locator(),
        )


def test_c09_last_progress_may_regress_but_furthest_is_monotonic() -> None:
    chapter_id = uid()
    current_scope = scope(chapter_id=chapter_id, furthest=9)
    position = ReadingPosition(
        chapter_id=chapter_id,
        block_id=uid(),
        block_offset=2,
        updated_at=datetime.now(UTC),
        device_id="device-a",
        row_version=2,
    )
    current = ReadingProgress(
        book_id=current_scope.book_id,
        user_id=current_scope.user_id,
        book_version_id=current_scope.book_version_id,
        chapter_id=chapter_id,
        last_chunk_index=9,
        furthest_chunk_index=9,
        position=position,
        updated_at=datetime.now(UTC),
        row_version=4,
    )
    update = ProgressUpdate(
        chapter_id=chapter_id,
        last_chunk_index=3,
        furthest_chunk_index=5,
        position=position,
        device_id="device-a",
    )
    result = apply_progress_update(scope= current_scope, current=current, update=update, expected_row_version=4)
    assert result.last_chunk_index == 3
    assert result.furthest_chunk_index == 9
    with pytest.raises(ContractViolation) as conflict:
        apply_progress_update(scope=current_scope, current=result, update=update, expected_row_version=4)
    assert conflict.value.code is ErrorCode.VERSION_CONFLICT


def test_c10_highlight_endpoints_quote_hash_and_scope_are_fail_closed() -> None:
    chapter_id = uid()
    current_scope = scope(chapter_id=chapter_id)
    block_id = uid()
    anchor = HighlightAnchor(
        highlight_id=uid(),
        user_id=current_scope.user_id,
        book_id=current_scope.book_id,
        book_version_id=current_scope.book_version_id,
        chapter_id=chapter_id,
        start=HighlightEndpoint(block_id=block_id, offset=0),
        end=HighlightEndpoint(block_id=block_id, offset=5),
        exact_quote="hello",
        prefix="",
        suffix=" world",
        text_sha256=sha256_text("hello"),
        created_at=datetime.now(UTC),
    )
    block = Block(
        block_id=block_id,
        chapter_id=chapter_id,
        book_id=current_scope.book_id,
        user_id=current_scope.user_id,
        book_version_id=current_scope.book_version_id,
        ordinal=0,
        text="hello world",
        text_sha256=sha256_text("hello world"),
        source_locator=locator(),
    )
    assert validate_highlight(scope=current_scope, anchor=anchor, blocks=[block]) == anchor
    with pytest.raises(ContractViolation):
        validate_highlight(
            scope=current_scope,
            anchor=anchor.model_copy(update={"text_sha256": sha256_text("wrong")}),
            blocks=[block],
        )
    with pytest.raises(ContractViolation):
        validate_highlight(
            scope=current_scope,
            anchor=anchor.model_copy(update={"user_id": uid()}),
            blocks=[block],
        )
    with pytest.raises(ValidationError):
        HighlightEndpoint(block_id=block_id, offset=-1)
    with pytest.raises(ContractViolation):
        validate_highlight(scope=current_scope, anchor=anchor, blocks=[block.model_copy(update={"text": "hell"})])
    with pytest.raises(ContractViolation):
        validate_highlight(
            scope=current_scope,
            anchor=anchor.model_copy(update={"exact_quote": "other", "text_sha256": sha256_text("other")}),
            blocks=[block],
        )


def test_c15_error_and_trace_redaction_are_whitelist_based() -> None:
    redacted = redact_trace_fields(
        {
            "trace_id": str(uid()),
            "run_id": str(uid()),
            "status": "failed",
            "prompt": "private question",
            "password": "secret",
            "cookie": "secret",
            "full_file": "book body",
            "stack": "traceback",
        }
    )
    assert set(redacted) == {"trace_id", "run_id", "status"}
    assert "private question" not in str(redacted)
