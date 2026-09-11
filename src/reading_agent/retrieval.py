"""Small deterministic retrieval primitives for the Stage 05 preview."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Iterable, TypeVar


DEFAULT_TARGET_TOKENS = 420
DEFAULT_MAX_TOKENS = 650

_LATIN_WORD = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")

T = TypeVar("T")


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
