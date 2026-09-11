"""User-controlled, book-scoped reading memory for the local Agent preview."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import Field

from .contracts import StrictModel
from .domain import utc_now


class ReadingMemory(StrictModel):
    memory_id: UUID
    user_id: UUID
    book_id: UUID
    source_run_id: UUID
    kind: Literal["discussion"] = "discussion"
    summary: str = Field(min_length=1, max_length=900)
    created_at: datetime


class ReadingMemoryStore:
    """Keep compact discussion memories separate from factual evidence."""

    def __init__(self) -> None:
        self.records: dict[UUID, ReadingMemory] = {}

    def remember_discussion(
        self,
        *,
        user_id: UUID,
        book_id: UUID,
        source_run_id: UUID,
        question: str,
        answer: str,
    ) -> ReadingMemory:
        summary = f"用户曾问：{question.strip()[:280]}\n当时讨论：{answer.strip()[:560]}".strip()
        existing = next(
            (item for item in self.records.values() if item.source_run_id == source_run_id),
            None,
        )
        if existing is not None:
            return existing
        record = ReadingMemory(
            memory_id=uuid4(),
            user_id=user_id,
            book_id=book_id,
            source_run_id=source_run_id,
            summary=summary,
            created_at=utc_now(),
        )
        self.records[record.memory_id] = record
        return record

    def list_for_book(self, *, user_id: UUID, book_id: UUID) -> list[ReadingMemory]:
        values = [
            item for item in self.records.values() if item.user_id == user_id and item.book_id == book_id
        ]
        return sorted(values, key=lambda item: item.created_at, reverse=True)

    def search(self, *, user_id: UUID, book_id: UUID, query: str, limit: int = 3) -> list[ReadingMemory]:
        terms = {term for term in re.findall(r"[\w\u3400-\u9fff]+", query.casefold()) if len(term) > 1}
        candidates = self.list_for_book(user_id=user_id, book_id=book_id)
        scored = []
        for item in candidates:
            text = item.summary.casefold()
            score = sum(1 for term in terms if term in text)
            if score:
                scored.append((score, item))
        scored.sort(key=lambda value: (value[0], value[1].created_at), reverse=True)
        return [item for _, item in scored[: max(0, min(limit, 5))]]

    def clear_book(self, *, user_id: UUID, book_id: UUID) -> int:
        ids = [
            key for key, item in self.records.items() if item.user_id == user_id and item.book_id == book_id
        ]
        for key in ids:
            del self.records[key]
        return len(ids)

    def dump(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.records.values()]

    def restore(self, raw: Any) -> None:
        if raw is None:
            return
        if not isinstance(raw, list):
            raise ValueError("memory state must be a list")
        values = [ReadingMemory.model_validate_json(json.dumps(item, ensure_ascii=False)) for item in raw]
        self.records = {item.memory_id: item for item in values}
