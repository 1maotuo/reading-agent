from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from reading_agent.api import ApiServices, CSRF_COOKIE
from reading_agent.contracts import (
    Block,
    Chapter,
    Chunk,
    ReadBookBlocksArgs,
    ScopeContext,
    SearchBookArgs,
    SourceLocator,
    ToolCallStatus,
    ToolName,
)
from reading_agent.domain import chunk_body_sha256, dispatch_tool, sha256_text
from reading_agent.retrieval import bm25_rank, estimate_tokens, merge_section_blocks
from reading_agent.stage05 import DEMO_IDENTIFIER, DEMO_PASSWORD, LocalBookToolProvider, create_stage05_app


def parsed_block(chapter_id: str, section_id: str, ordinal: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        chapter_id=chapter_id,
        section_id=section_id,
        block_id=f"block-{ordinal}",
        ordinal=ordinal,
        text=text,
    )


def test_structural_chunks_are_stable_and_stop_at_section_boundaries() -> None:
    chapter_id = "chapter-1"
    section_a = SimpleNamespace(
        section_id="section-a",
        blocks=[
            parsed_block(chapter_id, "section-a", 0, "alpha " * 150),
            parsed_block(chapter_id, "section-a", 1, "beta " * 150),
            parsed_block(chapter_id, "section-a", 2, "gamma " * 150),
        ],
    )
    section_b = SimpleNamespace(
        section_id="section-b",
        blocks=[parsed_block(chapter_id, "section-b", 3, "delta " * 20)],
    )

    first = merge_section_blocks([section_a, section_b])
    second = merge_section_blocks([section_a, section_b])

    assert first == second
    assert len(first) == 3
    assert first[0].section_id == "section-a"
    assert first[0].block_ids == ("block-0", "block-1")
    assert first[0].text == "\n".join(block.text for block in section_a.blocks[:2])
    assert first[0].token_count == estimate_tokens(first[0].text)
    assert first[1].section_id == "section-a"
    assert first[1].block_ids == ("block-2",)
    assert first[2].section_id == "section-b"
    assert first[2].block_ids == ("block-3",)


def test_structural_chunks_split_at_maximum_without_splitting_a_source_block() -> None:
    section = SimpleNamespace(
        section_id="section-a",
        blocks=[
            parsed_block("chapter-1", "section-a", 0, "longword " * 2200),
            parsed_block("chapter-1", "section-a", 1, "b " * 150),
            parsed_block("chapter-1", "section-a", 2, "c " * 150),
        ],
    )

    chunks = merge_section_blocks([section])

    assert [chunk.block_ids for chunk in chunks] == [("block-0",), ("block-1", "block-2")]
    assert chunks[0].token_count > 650
    assert chunks[0].text == "longword " * 2200
    assert all(chunk.token_count <= 650 for chunk in chunks[1:])


def test_bm25_ranks_chinese_and_english_and_drops_zero_matches() -> None:
    documents = [
        ("english", "A quiet mind can understand a difficult book."),
        ("chinese", "真正理解一本书，需要不断解释和复述。"),
        ("other", "天气和旅行计划与阅读无关。"),
    ]

    assert bm25_rank("理解 一本书", documents)[0][1] == "chinese"
    assert bm25_rank("quiet mind", documents)[0][1] == "english"
    assert bm25_rank("nonexistenttermzz", documents) == []
    assert bm25_rank("ＡＩ", [("normalized", "ai assistant")])[0][1] == "normalized"


def test_provider_filters_chapters_and_deduplicates_multiblock_evidence() -> None:
    from reading_agent.api import ApiServices
    services = ApiServices()
    user_id, book_id, version_id = uuid4(), uuid4(), uuid4()
    chapter_a, chapter_b = uuid4(), uuid4()
    block_a1, block_a2, block_b = uuid4(), uuid4(), uuid4()
    locator = SourceLocator(kind="synthetic", value="test")

    for chapter_id, ordinal, title in ((chapter_a, 0, "A"), (chapter_b, 1, "B")):
        services.books.add_chapter(
            Chapter(
                chapter_id=chapter_id,
                book_id=book_id,
                user_id=user_id,
                book_version_id=version_id,
                ordinal=ordinal,
                title=title,
                source_locator=locator,
            )
        )

    blocks = [
        Block(
            block_id=block_a1,
            chapter_id=chapter_a,
            book_id=book_id,
            user_id=user_id,
            book_version_id=version_id,
            ordinal=0,
            text="理解 论证 的第一段",
            text_sha256=sha256_text("理解 论证 的第一段"),
            source_locator=locator,
        ),
        Block(
            block_id=block_a2,
            chapter_id=chapter_a,
            book_id=book_id,
            user_id=user_id,
            book_version_id=version_id,
            ordinal=1,
            text="理解 论证 的第二段",
            text_sha256=sha256_text("理解 论证 的第二段"),
            source_locator=locator,
        ),
        Block(
            block_id=block_b,
            chapter_id=chapter_b,
            book_id=book_id,
            user_id=user_id,
            book_version_id=version_id,
            ordinal=2,
            text="另一个章节的内容",
            text_sha256=sha256_text("另一个章节的内容"),
            source_locator=locator,
        ),
    ]
    for block in blocks:
        services.books.add_block(block)
    chunk_a = Chunk(
        chunk_id=uuid4(),
        chapter_id=chapter_a,
        book_id=book_id,
        user_id=user_id,
        book_version_id=version_id,
        chunk_index=0,
        block_ids=[block_a1, block_a2],
        text_sha256=chunk_body_sha256(blocks[:2]),
        token_count=estimate_tokens("\n".join(block.text for block in blocks[:2])),
        embedding_model="local-bm25-preview-1",
        chunker_version="section-structure-chunker-v1",
    )
    chunk_b = chunk_a.model_copy(
        update={
            "chunk_id": uuid4(),
            "chapter_id": chapter_b,
            "chunk_index": 2,
            "block_ids": [block_b],
            "text_sha256": blocks[2].text_sha256,
            "token_count": estimate_tokens(blocks[2].text),
        }
    )
    services.books.add_chunk(chunk_a, "\n".join(block.text for block in blocks[:2]))
    services.books.add_chunk(chunk_b, blocks[2].text)
    provider = LocalBookToolProvider(services)
    scope = ScopeContext(
        session_id=uuid4(),
        user_id=user_id,
        book_id=book_id,
        book_version_id=version_id,
        chapter_id=None,
        furthest_chunk_index=None,
        request_id=uuid4(),
        trace_id=uuid4(),
    )

    result = provider.call(
        ToolName.SEARCH_BOOK,
        SearchBookArgs(query="理解 论证", top_k=5, chapter_ids=[chapter_a]),
        scope,
        uuid4(),
    )
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.data.items[0].chapter_id == chapter_a
    assert all(item.chapter_id == chapter_a for item in result.data.items)

    bound_scope = scope.model_copy(update={"chapter_id": chapter_a})
    expanded = provider.call(
        ToolName.SEARCH_BOOK,
        SearchBookArgs(query="另一个章节", top_k=5, chapter_ids=[chapter_b]),
        bound_scope,
        uuid4(),
    )
    assert expanded.status is ToolCallStatus.FAILED

    read = provider.call(
        ToolName.READ_BOOK_BLOCKS,
        ReadBookBlocksArgs(block_ids=[block_a1, block_a2]),
        scope,
        uuid4(),
    )
    assert read.status is ToolCallStatus.SUCCEEDED
    assert len(read.evidence_refs) == 1
    evidence = read.evidence_refs[0]
    assert evidence.block_ids == [block_a1, block_a2]
    assert evidence.quote == "理解 论证 的第一段\n理解 论证 的第二段"
    assert evidence.content_sha256 == sha256_text(evidence.quote)


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/sessions",
        json={"identifier": DEMO_IDENTIFIER, "password": DEMO_PASSWORD},
    )
    assert response.status_code == 200, response.text
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf
    return csrf


def test_real_markdown_import_search_evidence_and_sqlite_restore(tmp_path) -> None:
    markdown = """# 第一章 理解

## 第一节 论证

理解不是记住结论，而是能够说明结论为什么成立。

论证需要把前提、推理过程和结论连接起来，读者要能复述这条链路。

## 第二节 复述

复述不是重复原句，而是用自己的话重新组织观点并检查遗漏。

# 第二章 应用

## 第一节 实践

把观点应用到新的例子中，才能发现自己是否真的理解。
"""
    app = create_stage05_app(tmp_path)
    with TestClient(app) as client:
        csrf = _login(client)
        uploaded = client.post(
            "/api/v1/books",
            data={"title": "真实分块测试", "format": "markdown"},
            files={"file": ("real.md", markdown.encode("utf-8"), "text/markdown")},
            headers={"Idempotency-Key": f"retrieval-{uuid4()}", "X-CSRF-Token": csrf},
        )
        assert uploaded.status_code == 202, uploaded.text
        book_id = UUID(uploaded.json()["book_id"])
        chapters = client.get(f"/api/v1/books/{book_id}/chapters").json()["items"]
        assert len(chapters) == 2

        services = app.state.services
        version_id = services.books.books[book_id].active_version_id
        assert version_id is not None
        imported_chunks = sorted(
            [chunk for chunk in services.books.chunks.values() if chunk.book_id == book_id],
            key=lambda chunk: (chunk.chapter_id, chunk.chunk_index),
        )
        imported_blocks = [block for block in services.books.blocks.values() if block.book_id == book_id]
        assert len(imported_chunks) < len(imported_blocks)
        assert any(len(chunk.block_ids) > 1 for chunk in imported_chunks)
        assert not any("论证需要" in text and "复述不是" in text for text in services.books.chunk_texts.values())
        before_snapshot = [
            (
                chunk.chunk_id,
                chunk.chunk_index,
                tuple(chunk.block_ids),
                chunk.text_sha256,
                services.books.chunk_texts[chunk.chunk_id],
            )
            for chunk in imported_chunks
        ]

        user_id = next(iter(services.auth.accounts.values()))[0]
        scope = ScopeContext(
            session_id=uuid4(),
            user_id=user_id,
            book_id=book_id,
            book_version_id=version_id,
            chapter_id=UUID(chapters[0]["chapter_id"]),
            furthest_chunk_index=None,
            request_id=uuid4(),
            trace_id=uuid4(),
        )
        provider = LocalBookToolProvider(services)
        result = dispatch_tool(
            scope=scope,
            name=ToolName.SEARCH_BOOK,
            args={"query": "前提 推理 结论", "top_k": 3},
            call_id=uuid4(),
            provider=provider,
            evidence_reader=provider.read_evidence,
        )
        assert result.status is ToolCallStatus.SUCCEEDED
        assert result.evidence_refs
        for ref in result.evidence_refs:
            reread = provider.read_evidence(scope, ref.evidence_id)
            assert reread == ref
            assert reread is not None
            assert reread.content_sha256 == sha256_text(reread.quote)

        services.storage_boundary.save(services)

    restored_app = create_stage05_app(tmp_path)
    restored_services = restored_app.state.services
    restored_chunks = sorted(
        [chunk for chunk in restored_services.books.chunks.values() if chunk.book_id == book_id],
        key=lambda chunk: (chunk.chapter_id, chunk.chunk_index),
    )
    after_snapshot = [
        (
            chunk.chunk_id,
            chunk.chunk_index,
            tuple(chunk.block_ids),
            chunk.text_sha256,
            restored_services.books.chunk_texts[chunk.chunk_id],
        )
        for chunk in restored_chunks
    ]
    assert after_snapshot == before_snapshot
