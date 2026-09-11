"""Small context-budget primitives for the single reading Agent runtime."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable

from .contracts import EvidenceRef


MemoryContextItem = dict[str, str | int | bool]


def estimate_tokens(text: str) -> int:
    """Conservative dependency-free estimate for mixed Chinese/English text."""

    if not text:
        return 0
    ascii_count = sum(1 for char in text if ord(char) < 128)
    non_ascii = len(text) - ascii_count
    return max(1, non_ascii + (ascii_count + 3) // 4)


def truncate_to_tokens(text: str, limit: int) -> str:
    """Keep the largest prefix whose conservative estimate fits ``limit``."""

    if limit <= 0 or not text:
        return ""
    if estimate_tokens(text) <= limit:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip()


@dataclass(frozen=True)
class ContextPackage:
    question: str
    skill_context: str
    history: tuple[dict[str, str], ...]
    memory: tuple[MemoryContextItem, ...]
    evidence: tuple[EvidenceRef, ...]
    estimated_tokens: int
    budget_tokens: int
    pruned_history: int
    pruned_memory: int
    pruned_evidence: int
    selection_preserved: bool
    truncated_question: bool
    truncated_skill_context: bool

    def trace_summary(self) -> dict[str, int | bool]:
        return {
            "budget_tokens": self.budget_tokens,
            "estimated_tokens": self.estimated_tokens,
            "history_items": len(self.history),
            "memory_items": len(self.memory),
            "evidence_items": len(self.evidence),
            "pruned_history": self.pruned_history,
            "pruned_memory": self.pruned_memory,
            "pruned_evidence": self.pruned_evidence,
            "selection_preserved": self.selection_preserved,
            "truncated_question": self.truncated_question,
            "truncated_skill_context": self.truncated_skill_context,
        }

    def model_history(self) -> tuple[dict[str, str], ...]:
        """Expose memory as clearly labelled background, never as evidence."""

        memory_history = tuple(
            {
                "question": "[书籍学习记忆；仅作背景，不是原文证据]",
                "answer": str(item.get("text", "")),
            }
            for item in self.memory
            if str(item.get("text", "")).strip()
        )
        return memory_history + self.history


class ContextAssembler:
    """Assemble a bounded package without dropping primary selection evidence."""

    def __init__(self, budget_tokens: int = 4200) -> None:
        self.budget_tokens = max(1200, min(budget_tokens, 8000))

    def build(
        self,
        *,
        question: str,
        skill_context: str,
        history: Iterable[dict[str, str]],
        evidence: Iterable[EvidenceRef],
        memory: Iterable[MemoryContextItem] = (),
        selection_evidence_id: object | None = None,
    ) -> ContextPackage:
        history_values = list(history)
        memory_values = list(memory)
        evidence_values = list(evidence)
        bounded_question = truncate_to_tokens(question, min(600, max(200, self.budget_tokens // 5)))
        bounded_skill = truncate_to_tokens(skill_context, min(500, max(150, self.budget_tokens // 8)))
        overhead = min(500, max(200, self.budget_tokens // 10))
        fixed = estimate_tokens(bounded_question) + estimate_tokens(bounded_skill) + overhead
        remaining = max(0, self.budget_tokens - fixed)

        kept_evidence: list[EvidenceRef] = []
        evidence_tokens = 0
        ordered_evidence = sorted(
            evidence_values,
            key=lambda item: 0 if selection_evidence_id is not None and item.evidence_id == selection_evidence_id else 1,
        )
        for item in ordered_evidence:
            cost = estimate_tokens(item.quote) + 28
            is_selection = selection_evidence_id is not None and item.evidence_id == selection_evidence_id
            available = remaining - evidence_tokens
            if is_selection and cost > available and available > 28:
                quote = truncate_to_tokens(item.quote, available - 28)
                if quote:
                    item = item.model_copy(
                        update={
                            "quote": quote,
                            "content_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                        }
                    )
                    cost = estimate_tokens(quote) + 28
            if cost <= available:
                kept_evidence.append(item)
                evidence_tokens += cost

        kept_memory: list[MemoryContextItem] = []
        memory_tokens = 0
        memory_budget = max(0, remaining - evidence_tokens)
        for item in memory_values:
            text = str(item.get("text", ""))
            cost = estimate_tokens(text) + 16
            if memory_tokens + cost > memory_budget:
                continue
            kept_memory.append(item)
            memory_tokens += cost

        kept_history_reversed: list[dict[str, str]] = []
        history_tokens = 0
        history_budget = max(0, remaining - evidence_tokens - memory_tokens)
        for item in reversed(history_values):
            cost = estimate_tokens(item.get("question", "")) + estimate_tokens(item.get("answer", "")) + 20
            if history_tokens + cost > history_budget:
                continue
            kept_history_reversed.append(item)
            history_tokens += cost
        kept_history = list(reversed(kept_history_reversed))
        estimated = fixed + evidence_tokens + memory_tokens + history_tokens
        return ContextPackage(
            question=bounded_question,
            skill_context=bounded_skill,
            history=tuple(kept_history),
            memory=tuple(kept_memory),
            evidence=tuple(kept_evidence),
            estimated_tokens=estimated,
            budget_tokens=self.budget_tokens,
            pruned_history=len(history_values) - len(kept_history),
            pruned_memory=len(memory_values) - len(kept_memory),
            pruned_evidence=len(evidence_values) - len(kept_evidence),
            selection_preserved=(
                selection_evidence_id is not None
                and any(item.evidence_id == selection_evidence_id for item in kept_evidence)
            ),
            truncated_question=bounded_question != question,
            truncated_skill_context=bounded_skill != skill_context,
        )
