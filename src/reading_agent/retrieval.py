"""Small deterministic retrieval primitives for the Stage 05 preview."""

from __future__ import annotations

import math
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Iterable, TypeVar

import httpx


DEFAULT_TARGET_TOKENS = 420
DEFAULT_MAX_TOKENS = 650
EMBEDDING_DIMENSION = 1024
EMBEDDING_BATCH_SIZE = 20
DEFAULT_EMBEDDING_MODEL = "qwen3.7-text-embedding"

_LATIN_WORD = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")

T = TypeVar("T")


def validate_embeddings(values: Any, *, expected_count: int, dimension: int = EMBEDDING_DIMENSION) -> list[list[float]]:
    """Validate provider output before it can touch a book."""
    if not isinstance(values, list) or len(values) != expected_count:
        raise ValueError("embedding count does not match input order")
    result: list[list[float]] = []
    for vector in values:
        if not isinstance(vector, list) or len(vector) != dimension:
            raise ValueError("embedding dimension is invalid")
        if any(not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(float(item)) for item in vector):
            raise ValueError("embedding contains a non-finite value")
        result.append([float(item) for item in vector])
    return result


class DashScopeEmbeddingClient:
    """Opt-in, OpenAI-compatible DashScope embeddings client."""

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 model: str = DEFAULT_EMBEDDING_MODEL, timeout: float = 15.0,
                 client: Any | None = None) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("DASHSCOPE_API_KEY", "")
        self.base_url = (base_url or os.environ.get(
            "DASHSCOPE_COMPATIBLE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )).rstrip("/")
        self.model = model
        self.timeout = timeout
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and os.environ.get("READING_AGENT_ENABLE_EMBEDDINGS", "0") == "1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.enabled:
            raise RuntimeError("embedding is disabled or API key is unavailable")
        all_vectors: list[list[float]] = []
        own_client = self._client is None
        client = self._client or httpx.Client(timeout=self.timeout)
        try:
            for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
                batch = texts[start:start + EMBEDDING_BATCH_SIZE]
                response = client.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": batch, "dimensions": EMBEDDING_DIMENSION},
                )
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, list):
                    raise ValueError("embedding response data is invalid")
                ordered = sorted(data, key=lambda item: item.get("index", -1) if isinstance(item, dict) else -1)
                if [item.get("index") for item in ordered] != list(range(len(batch))):
                    raise ValueError("embedding response order is invalid")
                all_vectors.extend(validate_embeddings([item.get("embedding") for item in ordered], expected_count=len(batch)))
            return validate_embeddings(all_vectors, expected_count=len(texts))
        finally:
            if own_client:
                client.close()


def cosine_rank(query: list[float], documents: Iterable[tuple[T, list[float]]], *, top_k: int | None = None) -> list[tuple[float, T]]:
    query = validate_embeddings([query], expected_count=1)[0]
    scored: list[tuple[float, int, T]] = []
    qnorm = math.sqrt(sum(item * item for item in query))
    if not qnorm:
        return []
    for index, (item, vector) in enumerate(documents):
        try:
            vector = validate_embeddings([vector], expected_count=1)[0]
            norm = math.sqrt(sum(value * value for value in vector))
            score = sum(a * b for a, b in zip(query, vector)) / (qnorm * norm) if norm else 0.0
        except ValueError:
            continue
        if score > 0 and math.isfinite(score):
            scored.append((score, index, item))
    scored.sort(key=lambda value: (-value[0], value[1]))
    if top_k is not None:
        scored = scored[:top_k]
    return [(score, item) for score, _, item in scored]


def reciprocal_rank_fusion(*rankings: Iterable[tuple[float, T]], top_k: int, k: int = 60) -> list[tuple[float, T]]:
    ranks: dict[Any, float] = {}
    items: dict[Any, T] = {}
    for ranking in rankings:
        for rank, (_, item) in enumerate(ranking, 1):
            key = getattr(item, "chunk_id", item)
            items[key] = item
            ranks[key] = ranks.get(key, 0.0) + 1.0 / (k + rank)
    ordered = sorted(ranks.items(), key=lambda pair: (-pair[1], str(pair[0])))
    return [(score, items[item]) for item, score in ordered[:top_k]]


@dataclass(frozen=True)
class StructuralChunk:
    """A group of adjacent parsed blocks that can become one stored Chunk."""

    chapter_id: str
    section_id: str
    block_ids: tuple[str, ...]
    first_ordinal: int
    text: str
    token_count: int


def estimate_tokens(text: str) -> int:
    """Return a stable, intentionally coarse token estimate.

    The preview does not need provider-specific tokenization.  Keeping this
    estimate stable is more useful here because it controls chunk boundaries
    and the existing context budget uses the same rough scale.
    """

    if not text:
        return 0
    return max(1, math.ceil(len(text) / 3))


def merge_section_blocks(
    sections: Iterable[Any],
    *,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[StructuralChunk]:
    """Merge continuous blocks within each section, never across boundaries."""

    if target_tokens <= 0 or max_tokens < target_tokens:
        raise ValueError("chunk token thresholds are invalid")

    merged: list[StructuralChunk] = []
    for section in sections:
        current: list[Any] = []
        current_tokens = 0

        def flush() -> None:
            nonlocal current, current_tokens
            if not current:
                return
            text = "\n".join(block.text for block in current)
            merged.append(
                StructuralChunk(
                    chapter_id=str(current[0].chapter_id),
                    section_id=str(current[0].section_id),
                    block_ids=tuple(str(block.block_id) for block in current),
                    first_ordinal=current[0].ordinal,
                    text=text,
                    token_count=estimate_tokens(text),
                )
            )
            current = []
            current_tokens = 0

        for block in section.blocks:
            candidate_text = "\n".join([item.text for item in current] + [block.text])
            candidate_tokens = estimate_tokens(candidate_text)
            # A block is the smallest source unit in 1A.  A single oversized
            # block therefore remains intact, even when it exceeds max_tokens.
            if current and (current_tokens >= target_tokens or candidate_tokens > max_tokens):
                flush()
            current.append(block)
            current_tokens = estimate_tokens("\n".join(item.text for item in current))
        flush()
    return merged


def extract_terms(value: str) -> list[str]:
    """Extract normalized English words plus useful Chinese n-grams."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    terms = list(_LATIN_WORD.findall(normalized))
    for run in _CJK_RUN.findall(normalized):
        terms.extend(run)
        terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return [term for term in terms if term.strip()]


def bm25_rank(
    query: str,
    documents: Iterable[tuple[T, str]],
    *,
    top_k: int | None = None,
    k1: float = 1.2,
    b: float = 0.75,
    text_normalizer: Callable[[str], str] | None = None,
) -> list[tuple[float, T]]:
    """Rank ``(item, text)`` pairs with a small in-memory BM25 calculation."""

    pairs = list(documents)
    if not pairs:
        return []
    query_terms = Counter(extract_terms(query))
    if not query_terms:
        return []

    normalized = text_normalizer or (lambda value: unicodedata.normalize("NFKC", value).casefold())
    document_terms = [Counter(extract_terms(text)) for _, text in pairs]
    lengths = [sum(counts.values()) for counts in document_terms]
    average_length = sum(lengths) / max(1, len(lengths))
    document_frequency: Counter[str] = Counter()
    for terms in document_terms:
        document_frequency.update(terms.keys())

    scored: list[tuple[float, int, T]] = []
    for index, ((item, text), terms, length) in enumerate(zip(pairs, document_terms, lengths)):
        score = 0.0
        for term, query_frequency in query_terms.items():
            term_frequency = terms.get(term, 0)
            if not term_frequency:
                continue
            idf = math.log(1.0 + (len(pairs) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            denominator = term_frequency + k1 * (1.0 - b + b * length / max(1.0, average_length))
            score += idf * ((term_frequency * (k1 + 1.0)) / denominator) * query_frequency

        compact_query = re.sub(r"\s+", "", normalized(query))
        compact_text = re.sub(r"\s+", "", normalized(text))
        if compact_query and compact_query in compact_text:
            score += 0.25
        if score > 0:
            scored.append((score, index, item))

    # The caller owns the stable source-order tie breaker.  Index is used here
    # only so this pure helper never depends on object ordering.
    scored.sort(key=lambda value: (-value[0], value[1]))
    if top_k is not None:
        scored = scored[:top_k]
    return [(score, item) for score, _, item in scored]
