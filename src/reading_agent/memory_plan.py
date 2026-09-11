"""Deterministic book-memory planning and retrieval for the Stage 05 preview.

This module is deliberately one layer above :mod:`book_memory`.  It turns the
already validated V3 intent/cognition result into a small read plan, then
retrieves only exact-scope memories with a bounded, reproducible ranking.  It
does not call a model, write memory, expose an API, or replace persistence.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from .agent_core import estimate_tokens
from .book_memory import (
    BookConcept,
    BookLearnerProfileView,
    BookMemoryStore,
    ConceptState,
    EpisodeStatus,
    LearningEpisode,
    UnderstandingState,
)
from .contracts import (
    ComprehensionState,
    DialogueRoute,
    IntentFrame,
    StrictModel,
)


class MemoryKind(str, Enum):
    """Kinds that may be requested by a memory plan."""

    LEARNING_EPISODE = "learning_episode"
    CONCEPT_STATE = "concept_state"
    BOOK_PROFILE = "book_profile"
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"


class MemoryPlan(StrictModel):
    """A bounded, server-owned request for memory reads."""

    user_id: UUID
    book_id: UUID
    book_version_id: UUID
    route: DialogueRoute
    query: str = Field(default="", max_length=2000)
    concept_id: UUID | None = None
    kinds: list[MemoryKind] = Field(default_factory=list, max_length=4)
    kind_limits: dict[MemoryKind, int] = Field(default_factory=dict, max_length=4)
    token_budget: int = Field(default=0, ge=0, le=1600)
    include_unconfirmed: bool = False
    notes: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def _plan_is_bounded(self) -> "MemoryPlan":
        if len(set(self.kinds)) != len(self.kinds):
            raise ValueError("memory plan kinds must be unique")
        if any(kind not in self.kinds for kind in self.kind_limits):
            raise ValueError("kind limit must belong to requested kinds")
        if any(limit < 0 or limit > 5 for limit in self.kind_limits.values()):
            raise ValueError("kind limits must be between 0 and 5")
        return self


class MemoryHit(StrictModel):
    """A safe, compact memory excerpt; it is not book evidence."""

    kind: MemoryKind
    memory_id: UUID
    concept_id: UUID | None = None
    excerpt: str = Field(min_length=1, max_length=700)
    score: float = Field(ge=0, le=100)
    source_status: str = Field(min_length=1, max_length=40)
    conflicted: bool = False
    estimated_tokens: int = Field(ge=1, le=1600)
    reason: str = Field(min_length=1, max_length=240)
    created_at: datetime


class MemoryBundle(StrictModel):
    """Bounded retrieval output plus enough counters for a trace."""

    plan: MemoryPlan
    hits: list[MemoryHit] = Field(default_factory=list, max_length=20)
    estimated_tokens: int = Field(default=0, ge=0, le=1600)
    pruned_count: int = Field(default=0, ge=0)
    excluded_count: int = Field(default=0, ge=0)


def _terms(text: str) -> set[str]:
    """Extract stable ASCII words and Chinese runs/bigrams without a model."""

    normalized = text.casefold()
    terms = set(re.findall(r"[a-z0-9_]+", normalized))
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        if len(run) <= 2:
            terms.add(run)
        else:
            terms.add(run)
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def _overlap(query: str, candidate: str) -> float:
    query_terms = _terms(query)
    if not query_terms:
        return 0.0
    candidate_terms = _terms(candidate)
    return len(query_terms & candidate_terms) / len(query_terms)


def _scope_matches(item: object, plan: MemoryPlan) -> bool:
    return all(
        getattr(item, field, None) == getattr(plan, field)
        for field in ("user_id", "book_id", "book_version_id")
    )


def _profile_id(profile: BookLearnerProfileView) -> UUID:
    scope = f"{profile.user_id}:{profile.book_id}:{profile.book_version_id}"
    return uuid5(NAMESPACE_URL, f"reading-agent/book-profile/{scope}")


class MemoryPlanner:
    """Build a memory plan from the existing V3 routing result."""

    @staticmethod
    def build(
        *,
        intent: IntentFrame,
        user_id: UUID,
        book_id: UUID,
        book_version_id: UUID,
        query: str,
        concept_id: UUID | None = None,
    ) -> MemoryPlan:
        if intent.route is not DialogueRoute.BOOK_DIALOGUE:
            return MemoryPlan(
                user_id=user_id,
                book_id=book_id,
                book_version_id=book_version_id,
                route=intent.route,
                query=query,
                concept_id=concept_id,
                notes=["当前路由不读取书籍学习记忆"],
            )

        cognition = intent.cognition
        if concept_id is not None:
            kinds = [MemoryKind.CONCEPT_STATE, MemoryKind.LEARNING_EPISODE]
            notes = ["先取当前概念状态，再取相关学习事件"]
        else:
            kinds = [MemoryKind.LEARNING_EPISODE, MemoryKind.CONCEPT_STATE]
            notes = ["未锁定概念，先按当前问题检索学习事件"]
        include_unconfirmed = cognition.comprehension_state in {
            ComprehensionState.UNKNOWN,
            ComprehensionState.CONFUSED,
        }
        if include_unconfirmed:
            notes.append("用户理解状态不确定，保留未确认问题供澄清")
        return MemoryPlan(
            user_id=user_id,
            book_id=book_id,
            book_version_id=book_version_id,
            route=intent.route,
            query=query,
            concept_id=concept_id,
            kinds=kinds,
            kind_limits={
                MemoryKind.CONCEPT_STATE: 1,
                MemoryKind.LEARNING_EPISODE: 3,
            },
            token_budget=900,
            include_unconfirmed=include_unconfirmed,
            notes=notes,
        )


class MemoryRetriever:
    """Retrieve exact-scope book memories with deterministic ranking."""

    _KIND_ORDER = {
        MemoryKind.CONCEPT_STATE: 0,
        MemoryKind.LEARNING_EPISODE: 1,
        MemoryKind.BOOK_PROFILE: 2,
    }

    def __init__(self, store: BookMemoryStore) -> None:
        self.store = store

    def retrieve(self, plan: MemoryPlan) -> MemoryBundle:
        candidates: list[MemoryHit] = []
        excluded = 0
        for kind in plan.kinds:
            if kind is MemoryKind.CONCEPT_STATE:
                values, skipped = self._state_hits(plan)
            elif kind is MemoryKind.LEARNING_EPISODE:
                values, skipped = self._episode_hits(plan)
            elif kind is MemoryKind.BOOK_PROFILE:
                values, skipped = self._profile_hits(plan)
            else:
                values, skipped = [], 0
            candidates.extend(values)
            excluded += skipped

        candidates.sort(key=self._sort_key)
        selected: list[MemoryHit] = []
        used_by_kind: dict[MemoryKind, int] = {}
        used_tokens = 0
        pruned = 0
        for hit in candidates:
            limit = plan.kind_limits.get(hit.kind, 0)
            if used_by_kind.get(hit.kind, 0) >= limit:
                pruned += 1
                continue
            if used_tokens + hit.estimated_tokens > plan.token_budget:
                pruned += 1
                continue
            selected.append(hit)
            used_by_kind[hit.kind] = used_by_kind.get(hit.kind, 0) + 1
            used_tokens += hit.estimated_tokens

        return MemoryBundle(
            plan=plan,
            hits=selected,
            estimated_tokens=used_tokens,
            pruned_count=pruned,
            excluded_count=excluded,
        )

    @staticmethod
    def _sort_key(hit: MemoryHit) -> tuple[float, float, int, str]:
        return (
            -hit.score,
            -hit.created_at.timestamp(),
            MemoryRetriever._KIND_ORDER.get(hit.kind, 99),
            hit.memory_id.hex,
        )

    def _concepts(self, plan: MemoryPlan) -> dict[UUID, BookConcept]:
        return {
            concept.concept_id: concept
            for concept in self.store.concepts.values()
            if _scope_matches(concept, plan) and concept.status.value != "retired"
        }

    def _state_hits(self, plan: MemoryPlan) -> tuple[list[MemoryHit], int]:
        concepts = self._concepts(plan)
        hits: list[MemoryHit] = []
        excluded = 0
        for state in self.store.states.values():
            if not _scope_matches(state, plan):
                continue
            if state.state is UnderstandingState.UNKNOWN:
                excluded += 1
                continue
            if plan.concept_id is not None and state.concept_id != plan.concept_id:
                continue
            concept = concepts.get(state.concept_id)
            if concept is None:
                excluded += 1
                continue
            label = ", ".join([concept.canonical_name, *concept.aliases])
            exact = 5.0 if plan.concept_id == state.concept_id else 0.0
            overlap = _overlap(plan.query, label)
            state_bonus = {
                UnderstandingState.BLOCKED: 1.0,
                UnderstandingState.PARTIAL: 0.7,
                UnderstandingState.VERIFIED: 0.4,
                UnderstandingState.CONFLICTED: 0.2,
            }.get(state.state, 0.0)
            excerpt = f"概念「{concept.canonical_name}」的{state.dimension.value}理解状态：{state.state.value}"
            hits.append(
                self._hit(
                    kind=MemoryKind.CONCEPT_STATE,
                    memory_id=uuid5(
                        NAMESPACE_URL,
                        f"reading-agent/concept-state/{state.user_id}:{state.book_id}:{state.book_version_id}:{state.concept_id}:{state.dimension.value}",
                    ),
                    concept_id=state.concept_id,
                    excerpt=excerpt,
                    score=exact + overlap * 3.0 + state_bonus,
                    source_status=state.state.value,
                    conflicted=state.state is UnderstandingState.CONFLICTED,
                    created_at=state.updated_at,
                    reason="当前概念状态与问题相关" if exact or overlap else "同书概念状态",
                )
            )
        return hits, excluded

    def _episode_hits(self, plan: MemoryPlan) -> tuple[list[MemoryHit], int]:
        concepts = self._concepts(plan)
        hits: list[MemoryHit] = []
        excluded = 0
        for episode in self.store.episodes.values():
            if not _scope_matches(episode, plan):
                continue
            if episode.status is EpisodeStatus.UNCONFIRMED and not plan.include_unconfirmed:
                excluded += 1
                continue
            if plan.concept_id is not None and episode.concept_id not in {
                None,
                plan.concept_id,
            }:
                continue
            concept = concepts.get(episode.concept_id) if episode.concept_id else None
            concept_text = ""
            if concept is not None:
                concept_text = " ".join([concept.canonical_name, *concept.aliases])
            exact = 5.0 if plan.concept_id is not None and episode.concept_id == plan.concept_id else 0.0
            overlap = _overlap(plan.query, f"{episode.question_summary} {concept_text}")
            status_bonus = {
                EpisodeStatus.CONFIRMED: 0.8,
                EpisodeStatus.CORRECTED: 0.6,
                EpisodeStatus.CLOSED: 0.2,
                EpisodeStatus.UNCONFIRMED: 0.0,
            }[episode.status]
            excerpt = f"用户曾问：{episode.question_summary}（策略：{episode.response_strategy.value}）"
            hits.append(
                self._hit(
                    kind=MemoryKind.LEARNING_EPISODE,
                    memory_id=episode.episode_id,
                    concept_id=episode.concept_id,
                    excerpt=excerpt,
                    score=exact + overlap * 3.0 + status_bonus,
                    source_status=episode.status.value,
                    conflicted=False,
                    created_at=episode.created_at,
                    reason="同概念学习事件" if exact else "问题文本相关",
                )
            )
        return hits, excluded

    def _profile_hits(self, plan: MemoryPlan) -> tuple[list[MemoryHit], int]:
        profile = self.store.build_profile_view(
            user_id=plan.user_id,
            book_id=plan.book_id,
            book_version_id=plan.book_version_id,
        )
        if not profile.concept_states and not profile.unresolved_questions:
            return [], 0
        conflict = any(
            state.state is UnderstandingState.CONFLICTED
            for state in profile.concept_states
        )
        excerpt = (
            f"本书已有{len(profile.concept_states)}个概念状态，"
            f"{profile.unconfirmed_episode_count}个未确认学习问题"
        )
        return [
            self._hit(
                kind=MemoryKind.BOOK_PROFILE,
                memory_id=_profile_id(profile),
                concept_id=None,
                excerpt=excerpt,
                score=0.5,
                source_status="derived_view",
                conflicted=conflict,
                created_at=profile.generated_at,
                reason="按需生成的书籍学习概览",
            )
        ], 0

    @staticmethod
    def _hit(
        *,
        kind: MemoryKind,
        memory_id: UUID,
        concept_id: UUID | None,
        excerpt: str,
        score: float,
        source_status: str,
        conflicted: bool,
        created_at: datetime,
        reason: str,
    ) -> MemoryHit:
        bounded_excerpt = excerpt[:700]
        return MemoryHit(
            kind=kind,
            memory_id=memory_id,
            concept_id=concept_id,
            excerpt=bounded_excerpt,
            score=round(max(0.0, min(score, 100.0)), 6),
            source_status=source_status,
            conflicted=conflicted,
            estimated_tokens=estimate_tokens(bounded_excerpt) + 16,
            reason=reason,
            created_at=created_at.astimezone(timezone.utc),
        )
