"""Bounded writeback from a completed learning turn into book memory."""

from __future__ import annotations

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
    RECORDED_CONFUSION = "recorded_confusion"


class MemoryWriteResult(StrictModel):
    committed: bool
    reason: WritebackReason
    episode_id: UUID | None = None
    signal_id: UUID | None = None


def _dimension(friction_type: FrictionType) -> LearningDimension:
    if friction_type in {FrictionType.TERM, FrictionType.TRANSLATION}:
        return LearningDimension.MEANING
    if friction_type is FrictionType.EXAMPLE:
        return LearningDimension.APPLICATION
    if friction_type is FrictionType.EVIDENCE:
        return LearningDimension.CRITIQUE
    return LearningDimension.ARGUMENT


class BookMemoryWriter:
    """Write only explicit confusion observed on a completed book turn.

    The signal remains a candidate.  It is never applied to ``ConceptState``
    until a later validation step accepts it, so a model hypothesis cannot
    silently become the user's learning state.
    """

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
        memory_enabled: bool = True,
    ) -> MemoryWriteResult:
        if intent.route is not DialogueRoute.BOOK_DIALOGUE:
            return MemoryWriteResult(committed=False, reason=WritebackReason.ROUTE_NOT_BOOK)
        if not memory_enabled:
            return MemoryWriteResult(committed=False, reason=WritebackReason.MEMORY_DISABLED)
        if intent.cognition.comprehension_state is not ComprehensionState.CONFUSED:
            return MemoryWriteResult(committed=False, reason=WritebackReason.NO_LEARNING_SIGNAL)

        episode_id = uuid5(NAMESPACE_URL, f"reading-agent/learning-episode/{answer_run_id}")
        created_at = utc_now()
        candidate_episode = LearningEpisode(
            episode_id=episode_id,
            user_id=user_id,
            book_id=book_id,
            book_version_id=book_version_id,
            conversation_id=conversation_id,
            answer_run_id=answer_run_id,
            concept_id=None,
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

        signal_id = uuid5(NAMESPACE_URL, f"reading-agent/learning-signal/{episode_id}")
        if signal_id in self.store.signals:
            return MemoryWriteResult(
                committed=True,
                reason=WritebackReason.RECORDED_CONFUSION,
                episode_id=episode_id,
                signal_id=signal_id,
            )
        signal = LearningSignal(
            signal_id=signal_id,
            episode_id=episode_id,
            user_id=user_id,
            book_id=book_id,
            book_version_id=book_version_id,
            concept_id=None,
            dimension=_dimension(intent.cognition.friction_type),
            signal_type=LearningSignalType.EXPLICIT_CONFUSION,
            polarity=SignalPolarity.NEGATIVE,
            strength=(
                SignalStrength.STRONG
                if intent.cognition.confidence >= 0.7
                else SignalStrength.MEDIUM
            ),
            source_kind=SignalSource.EXPLICIT_USER,
            evidence_quote=(intent.cognition.evidence_quotes[0][:160]
                            if intent.cognition.evidence_quotes else question.strip()[:160]),
            validation_status=SignalValidation.CANDIDATE,
            created_at=episode.created_at,
        )
        self.store.add_signal(signal)
        return MemoryWriteResult(
            committed=True,
            reason=WritebackReason.RECORDED_CONFUSION,
            episode_id=episode_id,
            signal_id=signal_id,
        )
