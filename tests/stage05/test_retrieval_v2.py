from __future__ import annotations

import math
import os
import sqlite3
from pathlib import Path

import pytest
from uuid import uuid4

from fastapi.testclient import TestClient
from reading_agent.api import ApiServices, CSRF_COOKIE
from reading_agent.contracts import (
    Book, BookFormat, BookVersion, BookVersionStatus, Chapter, ContextSource,
    DialogueGoal, DialogueRelation, DialogueRoute, IntentFrame, QuestionCreate,
    Block, Chunk, ScopeContext, SelectionContext, SourceLocator,
)
from reading_agent.domain import sha256_text, utc_now
from reading_agent.persistence import SQLitePreviewStateStore
from reading_agent.stage05 import DEMO_IDENTIFIER, DEMO_PASSWORD, LocalBookToolProvider, ReaderAnswerHandler, create_stage05_app
from reading_agent.retrieval import (
    DashScopeEmbeddingClient,
    cosine_rank,
    reciprocal_rank_fusion,
    validate_embeddings,
)


def vector(index: int) -> list[float]:
    value = [0.0] * 1024
    value[index] = 1.0
    return value


def test_embedding_validation_is_strict_and_ordered() -> None:
    assert validate_embeddings([vector(1)], expected_count=1)[0][1] == 1.0
    with pytest.raises(ValueError):
        validate_embeddings([vector(1)[:-1]], expected_count=1)
    bad = vector(1)
    bad[2] = math.nan
    with pytest.raises(ValueError):
        validate_embeddings([bad], expected_count=1)


def test_cosine_and_rrf_are_stable_and_deduplicated() -> None:
    first = cosine_rank(vector(1), [("a", vector(1)), ("b", vector(2))], top_k=4)
    second = cosine_rank(vector(1), [("b", vector(2)), ("a", vector(1))], top_k=4)
    result = reciprocal_rank_fusion(first, second, top_k=2)
    assert [item for _, item in result] == ["a"]


def test_embedding_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("READING_AGENT_ENABLE_EMBEDDINGS", raising=False)
    client = DashScopeEmbeddingClient(api_key="test")
    assert not client.enabled


def test_full_book_route_overrides_current_view_but_selection_stays_narrow() -> None:
    services = ApiServices()
    user_id, book_id, version_id, chapter_id = uuid4(), uuid4(), uuid4(), uuid4()
    now = utc_now()
    services.books.add_book(Book(book_id=book_id, user_id=user_id, title="t", format=BookFormat.TXT,
                                 active_version_id=version_id, created_at=now, row_version=1))
    services.books.add_version(BookVersion(book_version_id=version_id, book_id=book_id, user_id=user_id,
                                           file_sha256="0" * 64, pipeline_version="t",
                                           status=BookVersionStatus.READY, created_at=now, published_at=now))
    services.books.add_chapter(Chapter(chapter_id=chapter_id, book_id=book_id, user_id=user_id,
                                       book_version_id=version_id, ordinal=0, title="c",
                                       source_locator=SourceLocator(kind="synthetic", value="t")))
    provider = LocalBookToolProvider(services)
    handler = ReaderAnswerHandler(services, provider, object())
    root = ScopeContext(session_id=uuid4(), user_id=user_id, book_id=book_id, book_version_id=version_id,
                        chapter_id=None, furthest_chunk_index=None, request_id=uuid4(), trace_id=uuid4())
    intent = IntentFrame(route=DialogueRoute.BOOK_DIALOGUE, relation=DialogueRelation.NEW,
                         goals=[DialogueGoal.COMPARE], context_sources=[ContextSource.CURRENT_VIEW])
    payload = QuestionCreate(question="比较全书主题", current_chapter_id=chapter_id, client_request_id=uuid4())
    assert handler.resolve_scope(services=services, root_scope=root, payload=payload, intent=intent) == root
    selection = SelectionContext(chapter_id=chapter_id, block_id=uuid4(), start_offset=0, end_offset=1,
                                 exact_quote="x", text_sha256="".join("0" for _ in range(64)))
    narrow = QuestionCreate(question="比较这段", current_chapter_id=chapter_id,
                            selection_context=selection, client_request_id=uuid4())
    assert handler.resolve_scope(services=services, root_scope=root, payload=narrow, intent=intent).chapter_id == chapter_id


class FakeEmbedder:
    enabled = True
    model = "qwen3.7-text-embedding"

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.calls = 0
        self.fail_after = fail_after

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for start in range(0, len(texts), 20):
            self.calls += 1
            if self.fail_after is not None and self.calls > self.fail_after:
                raise TimeoutError("fake embedding timeout")
            result.extend(vector(7 if text == "无词面" else index % 1024)
                          for index, text in enumerate(texts[start:start + 20], start))
        return result


def _provider_fixture() -> tuple[ApiServices, LocalBookToolProvider, ScopeContext, list[Chunk]]:
    services = ApiServices()
    user_id, book_id, version_id, chapter_a, chapter_b = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    locator = SourceLocator(kind="synthetic", value="v2")
    now = utc_now()
    services.books.add_book(Book(book_id=book_id, user_id=user_id, title="v2", format=BookFormat.TXT,
                                 active_version_id=version_id, created_at=now, row_version=1))
    services.books.add_version(BookVersion(book_version_id=version_id, book_id=book_id, user_id=user_id,
                                           file_sha256="1" * 64, pipeline_version="v2",
                                           status=BookVersionStatus.READY, created_at=now, published_at=now))
    chunks: list[Chunk] = []
    for chapter_id, texts in ((chapter_a, ["邻居甲", "语义目标", "邻居乙"]), (chapter_b, ["另一章"] )):
        services.books.add_chapter(Chapter(chapter_id=chapter_id, book_id=book_id, user_id=user_id,
                                           book_version_id=version_id, ordinal=len(chunks), title="c",
                                           source_locator=locator))
        for ordinal, text in enumerate(texts):
            block_id, chunk_id = uuid4(), uuid4()
            services.books.add_block(Block(block_id=block_id, chapter_id=chapter_id, book_id=book_id,
                                            user_id=user_id, book_version_id=version_id, ordinal=ordinal,
                                            text=text, text_sha256=sha256_text(text), source_locator=locator))
            chunk = Chunk(chunk_id=chunk_id, chapter_id=chapter_id, book_id=book_id, user_id=user_id,
                          book_version_id=version_id, chunk_index=ordinal * 10, block_ids=[block_id],
                          text_sha256=sha256_text(text), token_count=1, embedding_model="test",
                          chunker_version="test")
            services.books.add_chunk(chunk, text)
            chunks.append(chunk)
    services.storage_boundary = type("Boundary", (), {"book_repository": None})()
    scope = ScopeContext(session_id=uuid4(), user_id=user_id, book_id=book_id,
                         book_version_id=version_id, chapter_id=chapter_a,
                         furthest_chunk_index=None, request_id=uuid4(), trace_id=uuid4())
    return services, LocalBookToolProvider(services), scope, chunks


def test_sqlite_embedding_sidecar_roundtrip_and_legacy_snapshot_compatibility(tmp_path: Path) -> None:
    services, _, _, chunks = _provider_fixture()
    services.books.chunk_embeddings[chunks[0].chunk_id] = vector(3)
    store = SQLitePreviewStateStore(tmp_path / "state.sqlite3")
    store.save(services)
    restored = ApiServices()
    SQLitePreviewStateStore(tmp_path / "state.sqlite3").load(restored)
    assert restored.books.chunk_embeddings == {chunks[0].chunk_id: vector(3)}
    with sqlite3.connect(tmp_path / "state.sqlite3") as db:
        payload, digest = db.execute("SELECT payload_json, payload_sha256 FROM preview_state").fetchone()
        import hashlib, json
        raw = json.loads(payload)
        raw.pop("chunk_embeddings", None)
        payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        db.execute("UPDATE preview_state SET payload_json=?, payload_sha256=?", (payload, hashlib.sha256(payload.encode()).hexdigest()))
        db.commit()
    legacy = ApiServices()
    assert SQLitePreviewStateStore(tmp_path / "state.sqlite3").load(legacy)
    assert legacy.books.chunk_embeddings == {}


def test_pg_search_is_called_and_scope_filtered_then_degrades_to_local_vector() -> None:
    services, provider, scope, chunks = _provider_fixture()
    target = chunks[1]
    services.books.chunk_embeddings[target.chunk_id] = vector(7)
    class Repository:
        def __init__(self, failure: bool = False) -> None: self.failure = failure; self.calls = 0
        def search_embeddings(self, received_scope, query, *, top_k, chapter_ids=None):
            self.calls += 1
            if self.failure: raise RuntimeError("pg down")
            return [(0.9, target.chunk_id), (0.8, uuid4())]
    repo = Repository()
    services.storage_boundary.book_repository = repo
    fake = FakeEmbedder()
    provider = LocalBookToolProvider(services, fake)
    result = provider.call(__import__("reading_agent.contracts", fromlist=["ToolName"]).ToolName.SEARCH_BOOK,
                           __import__("reading_agent.contracts", fromlist=["SearchBookArgs"]).SearchBookArgs(query="无词面", top_k=1), scope, uuid4())
    assert repo.calls == 1 and result.evidence_refs[0].chunk_id == target.chunk_id
    services.storage_boundary.book_repository = Repository(failure=True)
    result = provider.call(__import__("reading_agent.contracts", fromlist=["ToolName"]).ToolName.SEARCH_BOOK,
                           __import__("reading_agent.contracts", fromlist=["SearchBookArgs"]).SearchBookArgs(query="无词面", top_k=1), scope, uuid4())
    assert result.status.value == "succeeded"


def test_pg_evidence_is_canonical_for_tool_result_and_readback() -> None:
    services, _, scope, chunks = _provider_fixture()
    canonical = {}
    class Repository:
        def put_evidence(self, received_scope, evidence):
            saved = evidence.model_copy(update={"evidence_id": uuid4()})
            canonical[saved.evidence_id] = saved
            return saved
        def read_evidence(self, received_scope, evidence_id):
            return canonical.get(evidence_id)
    services.storage_boundary.book_repository = Repository()
    provider = LocalBookToolProvider(services)
    from reading_agent.contracts import SearchBookArgs, ToolName
    result = provider.call(ToolName.SEARCH_BOOK, SearchBookArgs(query="语义目标", top_k=1), scope, uuid4())
    assert result.evidence_refs[0].evidence_id in canonical
    assert provider.read_evidence(scope, result.evidence_refs[0].evidence_id) == result.evidence_refs[0]

    block = services.books.blocks[chunks[0].block_ids[0]]
    payload = QuestionCreate(
        question="解释划选内容",
        selection_context=SelectionContext(
            chapter_id=block.chapter_id,
            block_id=block.block_id,
            start_offset=0,
            end_offset=len(block.text),
            exact_quote=block.text,
            text_sha256=block.text_sha256,
        ),
        client_request_id=uuid4(),
    )
    record = type("Record", (), {
        "scope": scope,
        "ledger": type("Ledger", (), {"run_id": uuid4()})(),
    })()
    primary = ReaderAnswerHandler(services, provider, object())._primary_evidence(
        services=services,
        record=record,
        payload=payload,
    )
    assert primary is not None and primary.evidence_id in canonical
    assert provider.read_evidence(scope, primary.evidence_id) == primary


def test_noncontiguous_neighbors_and_read_context_never_cross_chapter() -> None:
    services, provider, scope, chunks = _provider_fixture()
    from reading_agent.contracts import ReadBookBlocksArgs, SearchBookArgs, ToolName
    services.books.chunk_embeddings[chunks[1].chunk_id] = vector(7)
    provider = LocalBookToolProvider(services, FakeEmbedder())
    result = provider.call(ToolName.SEARCH_BOOK, SearchBookArgs(query="无词面", top_k=1), scope, uuid4())
    assert {ref.chunk_id for ref in result.evidence_refs} >= {chunks[0].chunk_id, chunks[1].chunk_id, chunks[2].chunk_id}
    block = services.books.chunks[chunks[1].chunk_id].block_ids[0]
    read = provider.call(ToolName.READ_BOOK_BLOCKS, ReadBookBlocksArgs(block_ids=[block], context_before=2, context_after=2), scope, uuid4())
    assert len(read.data.items) == 3
    other_block = services.books.chunks[chunks[-1].chunk_id].block_ids[0]
    assert provider.call(ToolName.READ_BOOK_BLOCKS, ReadBookBlocksArgs(block_ids=[other_block]), scope, uuid4()).status.value == "failed"
    assert provider.read_evidence(scope, result.evidence_refs[0].evidence_id) == result.evidence_refs[0]


def test_top_k_ten_keeps_primary_hits_and_caps_neighbor_evidence() -> None:
    services, _, scope, _ = _provider_fixture()
    from reading_agent.contracts import SearchBookArgs, ToolName

    locator = SourceLocator(kind="synthetic", value="top-k-boundary")
    for ordinal in range(30):
        text = "边界关键词" if ordinal % 2 == 0 else f"相邻上下文 {ordinal}"
        block_id, chunk_id = uuid4(), uuid4()
        services.books.add_block(Block(
            block_id=block_id, chapter_id=scope.chapter_id, book_id=scope.book_id,
            user_id=scope.user_id, book_version_id=scope.book_version_id,
            ordinal=ordinal + 10, text=text, text_sha256=sha256_text(text),
            source_locator=locator,
        ))
        services.books.add_chunk(Chunk(
            chunk_id=chunk_id, chapter_id=scope.chapter_id, book_id=scope.book_id,
            user_id=scope.user_id, book_version_id=scope.book_version_id,
            chunk_index=100 + ordinal * 10, block_ids=[block_id],
            text_sha256=sha256_text(text), token_count=1,
            embedding_model="test", chunker_version="test",
        ), text)

    result = LocalBookToolProvider(services).call(
        ToolName.SEARCH_BOOK,
        SearchBookArgs(query="边界关键词", top_k=10),
        scope,
        uuid4(),
    )
    assert result.status.value == "succeeded"
    assert len(result.data.items) == 10
    assert len(result.evidence_refs) == 20
    assert {item.evidence_id for item in result.data.items} <= {
        item.evidence_id for item in result.evidence_refs
    }


def test_import_embedding_is_atomic_and_bm25_remains_available(tmp_path: Path) -> None:
    texts = [f"unique topic {index}" for index in range(21)]
    for embedder, expected in ((FakeEmbedder(), 21), (FakeEmbedder(fail_after=1), 0)):
        sidecar = {}
        try:
            vectors = embedder.embed(texts)
            sidecar = {index: value for index, value in enumerate(vectors)}
        except Exception:
            sidecar.clear()
        assert len(sidecar) == expected
    services, provider, scope, _ = _provider_fixture()
    assert provider.best_chapter(scope, "语义目标") == scope.chapter_id


@pytest.mark.skipif(
    os.environ.get("READING_AGENT_LIVE_EMBEDDING_TEST") != "1"
    or not os.environ.get("DASHSCOPE_API_KEY"),
    reason="explicit opt-in live DashScope test",
)
def test_live_dashscope_embedding() -> None:
    client = DashScopeEmbeddingClient()
    documents = ["第一条极小实时测试文本", "第二条关于阅读理解的短文本", "第三条用于排序校验的短文本"]
    vectors = client.embed(documents)
    query = client.embed(["阅读理解"])[0]
    assert len(vectors) == 3 and all(len(item) == 1024 for item in vectors)
    assert all(math.isfinite(item) for vector_item in vectors for item in vector_item)
    assert cosine_rank(query, list(zip(range(3), vectors)), top_k=2)[0][1] == 1
