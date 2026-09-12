"""Bounded writeback from a completed learning turn into book memory."""

from __future__ import annotations

import re
from enum import Enum
from uuid import NAMESPACE_URL, UUID, uuid5

from .book_memory import (
    BookMemoryStore,
    EpisodeStatus,
    LearningDimension,
    LearningEpisode,
    LearningSignal,
    LearningSignalType,
    SignalPolarity,
    SignalSource,
    SignalStrength,
    SignalValidation,
)
from .contracts import (
    ComprehensionState,
    DialogueRelation,
    DialogueRoute,
    FrictionType,
    IntentFrame,
    StrictModel,
)
from .domain import utc_now


class WritebackReason(str, Enum):
    ROUTE_NOT_BOOK = "route_not_book"
    MEMORY_DISABLED = "memory_disabled"
    NO_LEARNING_SIGNAL = "no_learning_signal"
    NO_CONCEPT = "no_concept"
    RECORDED_CONFUSION = "recorded_confusion"
    RECORDED_PROGRESS = "recorded_progress"
    RECORDED_CORRECTION = "recorded_correction"


class MemoryWriteResult(StrictModel):
    committed: bool
    reason: WritebackReason
    episode_id: UUID | None = None
    signal_id: UUID | None = None
    concept_id: UUID | None = None


def _dimension(friction_type: FrictionType) -> LearningDimension:
    if friction_type in {FrictionType.TERM, FrictionType.TRANSLATION}:
        return LearningDimension.MEANING
    if friction_type is FrictionType.EXAMPLE:
        return LearningDimension.APPLICATION
    if friction_type is FrictionType.EVIDENCE:
        return LearningDimension.CRITIQUE
    if friction_type in {FrictionType.LOGIC, FrictionType.CONTRADICTION}:
        return LearningDimension.ARGUMENT
    return LearningDimension.RELATION


def _resolved_dimension(
    store: BookMemoryStore,
    concept_id: UUID,
    friction_type: FrictionType,
) -> LearningDimension:
    if friction_type is not FrictionType.UNKNOWN:
        return _dimension(friction_type)
    prior_signals = [
        item for item in store.signals.values() if item.concept_id == concept_id
    ]
    if prior_signals:
        return max(prior_signals, key=lambda item: item.created_at).dimension
    prior_states = [
        item for item in store.states.values() if item.concept_id == concept_id
    ]
    if prior_states:
        return max(prior_states, key=lambda item: item.updated_at).dimension
    return LearningDimension.RELATION


_CLEAR_TERMS = ("明白了", "懂了", "清楚了", "大概懂", "好像懂", "我理解")
_RESTATE_TERMS = ("我试着复述", "换句话说", "也就是说", "我的理解", "是不是可以理解为")
_CONFUSION_TERMS = ("没懂", "不懂", "不明白", "看不懂", "卡住")


def _contains(value: str, terms: tuple[str, ...]) -> bool:
    compact = re.sub(r"\s+", "", value.casefold())
    return any(term in compact for term in terms)


def _concept_label(intent: IntentFrame, question: str, evidence_excerpt: str | None) -> str | None:
    if intent.cognition.topic_label:
        return intent.cognition.topic_label
    quoted = re.search(r"[“「『\"]([^”」』\"]{2,80})[”」』\"]", question)
    if quoted:
        return quoted.group(1).strip()
    candidate = question
    for term in (
        "我还是不明白", "我还是不懂", "我还是没懂", "我不明白", "我不懂", "没懂",
        "怎么理解", "什么意思", "为什么", "作者", "这本书", "这一章", "这个", "这段",
        "请", "能不能", "帮我", "解释", "一下", "吗", "呢",
    ):
        candidate = candidate.replace(term, "")
    candidate = re.sub(r"[？?！!。,.，、：:；;\s]+", "", candidate).strip()
    if 2 <= len(candidate) <= 80:
        return candidate
    excerpt = re.sub(r"\s+", " ", evidence_excerpt or "").strip()
    return excerpt[:80].rstrip("，。；;,. ") if excerpt else None


def _current_quote(intent: IntentFrame, question: str) -> str | None:
    for quote in intent.cognition.evidence_quotes:
        if quote and quote in question:
            return quote[:160]
    return None


def _observation(
    intent: IntentFrame, question: str
) -> tuple[
    LearningSignalType,
    SignalPolarity,
    SignalStrength,
    SignalSource,
    SignalValidation,
    WritebackReason,
] | None:
    cognition = intent.cognition
    quote = _current_quote(intent, question)
    if (
        cognition.comprehension_state is ComprehensionState.CONFUSED
        and (quote or _contains(question, _CONFUSION_TERMS))
    ):
        return (
            LearningSignalType.EXPLICIT_CONFUSION,
            SignalPolarity.NEGATIVE,
            SignalStrength.STRONG if cognition.confidence >= 0.7 else SignalStrength.MEDIUM,
            SignalSource.EXPLICIT_USER,
            SignalValidation.CANDIDATE,
            WritebackReason.RECORDED_CONFUSION,
        )
    if intent.relation is DialogueRelation.CORRECTION and quote:
        return (
            LearningSignalType.USER_CORRECTION,
            SignalPolarity.NEGATIVE,
            SignalStrength.STRONG,
            SignalSource.EXPLICIT_USER,
            SignalValidation.ACCEPTED,
            WritebackReason.RECORDED_CORRECTION,
        )
    if cognition.comprehension_state is ComprehensionState.LIKELY_CLEAR and quote:
        if cognition.friction_type is FrictionType.LOGIC:
            signal_type = LearningSignalType.CORRECT_RECONSTRUCTION
        elif cognition.friction_type is FrictionType.EXAMPLE:
            signal_type = LearningSignalType.CORRECT_APPLICATION
        else:
            signal_type = LearningSignalType.CORRECT_RESTATEMENT
        validated = cognition.confidence >= 0.8
        return (
            signal_type,
            SignalPolarity.POSITIVE,
            SignalStrength.STRONG if validated else SignalStrength.MEDIUM,
            SignalSource.MODEL_CANDIDATE,
            SignalValidation.ACCEPTED if validated else SignalValidation.CANDIDATE,
            WritebackReason.RECORDED_PROGRESS,
        )
    if (
        cognition.comprehension_state is ComprehensionState.PARTIAL
        and quote
        and _contains(question, _CLEAR_TERMS + _RESTATE_TERMS)
    ):
        return (
            LearningSignalType.SELF_REPORT_CLEAR,
            SignalPolarity.POSITIVE,
            SignalStrength.MEDIUM,
            SignalSource.EXPLICIT_USER,
            SignalValidation.ACCEPTED,
            WritebackReason.RECORDED_PROGRESS,
        )
    return None


class BookMemoryWriter:
    """Write a small, evidence-backed learning signal after a completed turn."""

    def __init__(self, store: BookMemoryStore) -> None:
        self.store = store

    def record_completed_turn(
        self,
        *,
        intent: IntentFrame,
        user_id: UUID,
        book_id: UUID,
        book_version_id: UUID,
        conversation_id: UUID,
        answer_run_id: UUID,
        question: str,
        evidence_ref_ids: list[UUID] | None = None,
        concept_label: str | None = None,
        previous_concept_id: UUID | None = None,
        source_chapter_ids: list[UUID] | None = None,
        source_block_ids: list[UUID] | None = None,
        evidence_excerpt: str | None = None,
        memory_enabled: bool = True,
    ) -> MemoryWriteResult:
        if intent.route is not DialogueRoute.BOOK_DIALOGUE:
            return MemoryWriteResult(committed=False, reason=WritebackReason.ROUTE_NOT_BOOK)
        if not memory_enabled:
            return MemoryWriteResult(committed=False, reason=WritebackReason.MEMORY_DISABLED)
        observation = _observation(intent, question)
        if observation is None:
            return MemoryWriteResult(committed=False, reason=WritebackReason.NO_LEARNING_SIGNAL)

        signal_type, polarity, strength, source_kind, validation, reason = observation
        label = concept_label
        if label is None and previous_concept_id is None:
            label = _concept_label(intent, question, evidence_excerpt)
        concept = self.store.resolve_concept(
            user_id=user_id,
            book_id=book_id,
            book_version_id=book_version_id,
            label=label,
            preferred_concept_id=previous_concept_id,
            source_chapter_ids=source_chapter_ids or (),
            source_block_ids=source_block_ids or (),
        )
        if concept is None:
            return MemoryWriteResult(committed=False, reason=WritebackReason.NO_CONCEPT)

        episode_id = uuid5(NAMESPACE_URL, f"reading-agent/learning-episode/{answer_run_id}")
        created_at = utc_now()
        candidate_episode = LearningEpisode(
            episode_id=episode_id,
            user_id=user_id,
            book_id=book_id,
            book_version_id=book_version_id,
            conversation_id=conversation_id,
            answer_run_id=answer_run_id,
            concept_id=concept.concept_id,
            learning_goal=intent.cognition.learning_goal,
            friction_type=intent.cognition.friction_type,
            question_summary=question.strip()[:400],
            response_strategy=intent.response_strategy,
            evidence_ref_ids=list(evidence_ref_ids or []),
            status=EpisodeStatus.UNCONFIRMED,
            created_at=created_at,
        )
        episode = self.store.episodes.get(episode_id)
        if episode is None:
            episode = self.store.commit_episode(candidate_episode, completed=True)
        elif (
            episode.user_id != user_id
            or episode.book_id != book_id
            or episode.book_version_id != book_version_id
            or episode.answer_run_id != answer_run_id
        ):
            raise ValueError("learning episode idempotency key collision")
        assert episode is not None

        # A second user turn on the same concept confirms that the preceding
        # explicit difficulty was real; the current turn then supplies the new
        # state signal. This avoids turning a one-shot model guess into memory.
        for prior in sorted(self.store.signals.values(), key=lambda item: item.created_at, reverse=True):
            if (
                prior.episode_id != episode_id
                and prior.concept_id == concept.concept_id
                and prior.validation_status is SignalValidation.CANDIDATE
                and prior.signal_type is LearningSignalType.EXPLICIT_CONFUSION
            ):
                self.store.accept_signal(prior.signal_id)
                break

        signal_id = uuid5(NAMESPACE_URL, f"reading-agent/learning-signal/{episode_id}")
        if signal_id in self.store.signals:
            return MemoryWriteResult(
                committed=True,
                reason=reason,
                episode_id=episode_id,
                signal_id=signal_id,
                concept_id=concept.concept_id,
            )
        signal = LearningSignal(
            signal_id=signal_id,
            episode_id=episode_id,
            user_id=user_id,
            book_id=book_id,
            book_version_id=book_version_id,
            concept_id=concept.concept_id,
            dimension=_resolved_dimension(
                self.store,
                concept.concept_id,
                intent.cognition.friction_type,
            ),
            signal_type=signal_type,
            polarity=polarity,
            strength=strength,
            source_kind=source_kind,
            evidence_quote=_current_quote(intent, question) or question.strip()[:160],
            validation_status=validation,
            created_at=episode.created_at,
        )
        self.store.add_signal(signal)
        return MemoryWriteResult(
            committed=True,
            reason=reason,
            episode_id=episode_id,
            signal_id=signal_id,
            concept_id=concept.concept_id,
        )
