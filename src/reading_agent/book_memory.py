"""Book-scoped learning memory domain primitives for the Stage 05 preview.

This module deliberately stops at the domain boundary.  It does not replace the
existing ``ReadingMemoryStore``, call a model, or expose an API.  Its purpose is
to make the relationship between an observed learning episode, validated
learning signals, and the current concept state executable.  ``dump``/``restore``
only provide a validated preview snapshot; production repository wiring remains
outside this module.
"""

from __future__ import annotations

import re
import json
from datetime import datetime
from enum import Enum
from typing import Iterable
from uuid import UUID, uuid4

from pydantic import Field

from .contracts import FrictionType, LearningGoal, ResponseStrategy, StrictModel
from .domain import utc_now


class ConceptStatus(str, Enum):
    PROVISIONAL = "provisional"
    ACTIVE = "active"
    MERGED = "merged"
    RETIRED = "retired"


class ConceptSource(str, Enum):
    IMPORT = "import"
    RUNTIME = "runtime"
    USER = "user"


class EpisodeStatus(str, Enum):
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    CLOSED = "closed"


class LearningDimension(str, Enum):
    MEANING = "meaning"
    ARGUMENT = "argument"
    RELATION = "relation"
    APPLICATION = "application"
    CRITIQUE = "critique"


class UnderstandingState(str, Enum):
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    PARTIAL = "partial"
    VERIFIED = "verified"
    CONFLICTED = "conflicted"


class LearningSignalType(str, Enum):
    EXPLICIT_CONFUSION = "explicit_confusion"
    PROBLEM_CONFIRMED = "problem_confirmed"
    SELF_REPORT_CLEAR = "self_report_clear"
    CORRECT_RESTATEMENT = "correct_restatement"
    CORRECT_RECONSTRUCTION = "correct_reconstruction"
    CORRECT_APPLICATION = "correct_application"
    MISAPPLICATION = "misapplication"
    REPEATED_BASIC_QUESTION = "repeated_basic_question"
    USER_CORRECTION = "user_correction"


class SignalPolarity(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class SignalStrength(str, Enum):
    WEAK = "weak"
    MEDIUM = "medium"
    STRONG = "strong"


class SignalSource(str, Enum):
    EXPLICIT_USER = "explicit_user"
    OBSERVED_BEHAVIOR = "observed_behavior"
    MODEL_CANDIDATE = "model_candidate"


class SignalValidation(str, Enum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class BookConcept(StrictModel):
    """A stable, book-version-scoped concept anchor."""

    concept_id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    book_id: UUID
    book_version_id: UUID
    canonical_name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    source_chapter_ids: list[UUID] = Field(default_factory=list, max_length=50)
    source_block_ids: list[UUID] = Field(default_factory=list, max_length=100)
    status: ConceptStatus = ConceptStatus.PROVISIONAL
    merged_into_id: UUID | None = None
    created_source: ConceptSource = ConceptSource.RUNTIME
    created_at: datetime = Field(default_factory=utc_now)


class LearningEpisode(StrictModel):
    """A compact record of a completed book-learning interaction."""

    episode_id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    book_id: UUID
    book_version_id: UUID
    conversation_id: UUID
    answer_run_id: UUID
    concept_id: UUID | None = None
    learning_goal: LearningGoal = LearningGoal.UNKNOWN
    friction_type: FrictionType = FrictionType.UNKNOWN
    question_summary: str = Field(min_length=1, max_length=400)
    response_strategy: ResponseStrategy = ResponseStrategy.EXPLAIN
    evidence_ref_ids: list[UUID] = Field(default_factory=list, max_length=20)
    status: EpisodeStatus = EpisodeStatus.UNCONFIRMED
    created_at: datetime = Field(default_factory=utc_now)


class LearningSignal(StrictModel):
    """A candidate or accepted observation that may change one dimension."""

    signal_id: UUID = Field(default_factory=uuid4)
    episode_id: UUID
    user_id: UUID
    book_id: UUID
    book_version_id: UUID
    concept_id: UUID | None = None
    dimension: LearningDimension
    signal_type: LearningSignalType
    polarity: SignalPolarity
    strength: SignalStrength = SignalStrength.MEDIUM
    source_kind: SignalSource
    evidence_quote: str | None = Field(default=None, max_length=160)
    validation_status: SignalValidation = SignalValidation.CANDIDATE
    created_at: datetime = Field(default_factory=utc_now)


class ConceptState(StrictModel):
    """The rebuildable current projection for one concept and one dimension."""

    user_id: UUID
    book_id: UUID
    book_version_id: UUID
    concept_id: UUID
    dimension: LearningDimension
    state: UnderstandingState = UnderstandingState.UNKNOWN
    supporting_signal_ids: list[UUID] = Field(default_factory=list, max_length=128)
    opposing_signal_ids: list[UUID] = Field(default_factory=list, max_length=128)
    last_signal_id: UUID | None = None
    last_signal_polarity: SignalPolarity | None = None
    last_signal_strength: SignalStrength | None = None
    row_version: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=utc_now)


class BookLearnerProfileView(StrictModel):
    """A query result, not a second source-of-truth profile table."""

    user_id: UUID
    book_id: UUID
    book_version_id: UUID
    concept_states: list[ConceptState] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=20)
    unconfirmed_episode_count: int = Field(default=0, ge=0)
    generated_at: datetime = Field(default_factory=utc_now)


ScopeKey = tuple[UUID, UUID, UUID]
StateKey = tuple[UUID, UUID, UUID, UUID, LearningDimension]


def _scope_key(user_id: UUID, book_id: UUID, book_version_id: UUID) -> ScopeKey:
    return user_id, book_id, book_version_id


def _normalize_label(value: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", "", value.casefold())


_STATE_SIGNAL_TYPES = {
    LearningSignalType.EXPLICIT_CONFUSION,
    LearningSignalType.SELF_REPORT_CLEAR,
    LearningSignalType.CORRECT_RESTATEMENT,
    LearningSignalType.CORRECT_RECONSTRUCTION,
    LearningSignalType.CORRECT_APPLICATION,
    LearningSignalType.MISAPPLICATION,
    LearningSignalType.REPEATED_BASIC_QUESTION,
}


class BookMemoryStore:
    """In-memory M1 domain store with scope checks and deterministic reduction."""

    def __init__(self) -> None:
        self.concepts: dict[UUID, BookConcept] = {}
        self.episodes: dict[UUID, LearningEpisode] = {}
        self.signals: dict[UUID, LearningSignal] = {}
        self.states: dict[StateKey, ConceptState] = {}

    def dump(self) -> dict[str, list[dict[str, object]]]:
        """Return a deterministic, JSON-safe preview snapshot.

        ``ConceptState`` is a derived projection and is intentionally not
        persisted.  Rebuilding it from accepted signals keeps restart behavior
        deterministic and avoids two durable sources of truth.
        """

        return {
            "concepts": [
                value.model_dump(mode="json")
                for value in sorted(self.concepts.values(), key=lambda item: item.concept_id.hex)
            ],
            "episodes": [
                value.model_dump(mode="json")
                for value in sorted(
                    self.episodes.values(), key=lambda item: (item.created_at, item.episode_id.hex)
                )
            ],
            "signals": [
                value.model_dump(mode="json")
                for value in sorted(
                    self.signals.values(), key=lambda item: (item.created_at, item.signal_id.hex)
                )
            ],
        }

    def restore(self, raw: object) -> None:
        """Replace this store from a validated preview snapshot.

        Parsing happens in a temporary store first.  A malformed snapshot or
        a cross-scope relationship therefore cannot partially replace live
        memory.  Derived states are rebuilt only from accepted signals.
        """

        if raw is None:
            return
        if not isinstance(raw, dict):
            raise ValueError("book memory snapshot must be an object")

        def items(name: str) -> list[object]:
            value = raw.get(name, [])
            if not isinstance(value, list):
                raise ValueError(f"book memory {name} must be a list")
            return value

        restored = BookMemoryStore()
        try:
            concepts = [
                BookConcept.model_validate_json(json.dumps(item, ensure_ascii=False))
                for item in items("concepts")
            ]
            for concept in concepts:
                restored.add_concept(concept)
            for concept in restored.concepts.values():
                if concept.status is ConceptStatus.MERGED:
                    restored._follow_merge(concept)

            episodes = [
                LearningEpisode.model_validate_json(json.dumps(item, ensure_ascii=False))
                for item in items("episodes")
            ]
            for episode in sorted(episodes, key=lambda item: (item.created_at, item.episode_id.hex)):
                restored.commit_episode(episode, completed=True)

            signals = [
                LearningSignal.model_validate_json(json.dumps(item, ensure_ascii=False))
                for item in items("signals")
            ]
            for signal in sorted(signals, key=lambda item: (item.created_at, item.signal_id.hex)):
                restored.add_signal(signal)
            restored.rebuild_states()
        except Exception as exc:
            raise ValueError("invalid book memory snapshot") from exc

        self.concepts = restored.concepts
        self.episodes = restored.episodes
        self.signals = restored.signals
        self.states = restored.states

    @staticmethod
    def _matches_scope(
        *,
        user_id: UUID,
        book_id: UUID,
        book_version_id: UUID,
        item: BookConcept | LearningEpisode | LearningSignal | ConceptState,
    ) -> bool:
        return _scope_key(user_id, book_id, book_version_id) == _scope_key(
            item.user_id, item.book_id, item.book_version_id
        )

    def add_concept(self, concept: BookConcept) -> BookConcept:
        if concept.status is ConceptStatus.MERGED and concept.merged_into_id is None:
            raise ValueError("merged concept requires merged_into_id")
        if concept.concept_id in self.concepts:
            existing = self.concepts[concept.concept_id]
            if existing != concept:
                raise ValueError("concept_id already exists with different payload")
            return existing
        self.concepts[concept.concept_id] = concept
        return concept

    def _follow_merge(self, concept: BookConcept) -> BookConcept:
        current = concept
        seen: set[UUID] = set()
        while current.status is ConceptStatus.MERGED and current.merged_into_id is not None:
            if current.concept_id in seen:
                raise ValueError("concept merge cycle")
            seen.add(current.concept_id)
            target = self.concepts.get(current.merged_into_id)
            if target is None:
                raise ValueError("merged concept target not found")
            if not self._matches_scope(
                user_id=current.user_id,
                book_id=current.book_id,
                book_version_id=current.book_version_id,
                item=target,
            ):
                raise ValueError("concept merge crosses scope")
            current = target
        return current

    def match_concept(
        self,
        *,
        user_id: UUID,
        book_id: UUID,
        book_version_id: UUID,
        label: str,
    ) -> BookConcept | None:
        """Match a canonical name or alias only inside the exact book version."""

        normalized = _normalize_label(label)
        if not normalized:
            return None
        for concept in self.concepts.values():
            if not self._matches_scope(
                user_id=user_id,
                book_id=book_id,
                book_version_id=book_version_id,
                item=concept,
            ) or concept.status is ConceptStatus.RETIRED:
                continue
            labels = [concept.canonical_name, *concept.aliases]
            if any(_normalize_label(item) == normalized for item in labels):
                return self._follow_merge(concept)
        return None

    def merge_concept(
        self,
        *,
        user_id: UUID,
        book_id: UUID,
        book_version_id: UUID,
        source_id: UUID,
        target_id: UUID,
    ) -> BookConcept:
        source = self._require_concept(source_id, user_id, book_id, book_version_id)
        target = self._require_concept(target_id, user_id, book_id, book_version_id)
        if source.concept_id == target.concept_id:
            raise ValueError("concept cannot merge into itself")
        merged = source.model_copy(
            update={"status": ConceptStatus.MERGED, "merged_into_id": target.concept_id}
        )
        self.concepts[source.concept_id] = merged
        return merged

    def _require_concept(
        self, concept_id: UUID, user_id: UUID, book_id: UUID, book_version_id: UUID
    ) -> BookConcept:
        concept = self.concepts.get(concept_id)
        if concept is None or not self._matches_scope(
            user_id=user_id,
            book_id=book_id,
            book_version_id=book_version_id,
            item=concept,
        ):
            raise KeyError("concept not found in scope")
        return concept

    def commit_episode(self, episode: LearningEpisode, *, completed: bool) -> LearningEpisode | None:
        """Commit only a completed episode; cancellation and failure stay history/Trace-only."""

        if not completed:
            return None
        if episode.concept_id is not None:
            self._require_concept(
                episode.concept_id, episode.user_id, episode.book_id, episode.book_version_id
            )
        existing = self.episodes.get(episode.episode_id)
        if existing is not None:
            if existing != episode:
                raise ValueError("episode_id already exists with different payload")
            return existing
        self.episodes[episode.episode_id] = episode
        return episode

    def add_signal(self, signal: LearningSignal) -> LearningSignal:
        episode = self.episodes.get(signal.episode_id)
        if episode is None or not self._matches_scope(
            user_id=signal.user_id,
            book_id=signal.book_id,
            book_version_id=signal.book_version_id,
            item=episode,
        ):
            raise KeyError("episode not found in scope")
        if episode.concept_id is not None and signal.concept_id != episode.concept_id:
            raise ValueError("signal concept does not match episode concept")
        if signal.concept_id is not None:
            self._require_concept(
                signal.concept_id, signal.user_id, signal.book_id, signal.book_version_id
            )
        if signal.source_kind is SignalSource.EXPLICIT_USER and not signal.evidence_quote:
            raise ValueError("explicit user signal requires evidence_quote")
        existing = self.signals.get(signal.signal_id)
        if existing is not None:
            if existing != signal:
                raise ValueError("signal_id already exists with different payload")
            return existing
        self.signals[signal.signal_id] = signal
        if signal.validation_status is SignalValidation.ACCEPTED:
            self._apply_accepted_signal(signal)
        return signal

    def accept_signal(self, signal_id: UUID) -> LearningSignal:
        signal = self.signals.get(signal_id)
        if signal is None:
            raise KeyError("signal not found")
        if signal.validation_status is SignalValidation.REJECTED:
            raise ValueError("rejected signal cannot be accepted")
        if signal.validation_status is SignalValidation.ACCEPTED:
            return signal
        accepted = signal.model_copy(update={"validation_status": SignalValidation.ACCEPTED})
        self.signals[signal_id] = accepted
        self._apply_accepted_signal(accepted)
        return accepted

    def reject_signal(self, signal_id: UUID) -> LearningSignal:
        signal = self.signals.get(signal_id)
        if signal is None:
            raise KeyError("signal not found")
        if signal.validation_status is SignalValidation.ACCEPTED:
            raise ValueError("accepted signal cannot be rejected")
        rejected = signal.model_copy(update={"validation_status": SignalValidation.REJECTED})
        self.signals[signal_id] = rejected
        return rejected

    def _apply_accepted_signal(self, signal: LearningSignal) -> None:
        episode = self.episodes[signal.episode_id]
        if signal.signal_type is LearningSignalType.PROBLEM_CONFIRMED:
            if episode.status is EpisodeStatus.UNCONFIRMED:
                self.episodes[episode.episode_id] = episode.model_copy(
                    update={"status": EpisodeStatus.CONFIRMED}
                )
            return
        if signal.signal_type is LearningSignalType.USER_CORRECTION:
            self.episodes[episode.episode_id] = episode.model_copy(
                update={"status": EpisodeStatus.CORRECTED}
            )
            return
        if signal.signal_type not in _STATE_SIGNAL_TYPES or signal.concept_id is None:
            return

        key: StateKey = (
            signal.user_id,
            signal.book_id,
            signal.book_version_id,
            signal.concept_id,
            signal.dimension,
        )
        current = self.states.get(key)
        target = self._target_state(current, signal)
        if target is None:
            return
        conflict = False
        previous = self.signals.get(current.last_signal_id) if current else None
        if (
            current is not None
            and previous is not None
            and previous.episode_id == signal.episode_id
            and previous.polarity is not signal.polarity
            and previous.strength is signal.strength
            and previous.created_at == signal.created_at
        ):
            conflict = True
        if conflict:
            target = UnderstandingState.CONFLICTED
        elif current is not None and current.state is UnderstandingState.CONFLICTED:
            # A later strong, validated signal is allowed to resolve a conflict.
            if signal.strength is not SignalStrength.STRONG:
                target = UnderstandingState.CONFLICTED

        supporting = list(current.supporting_signal_ids) if current else []
        opposing = list(current.opposing_signal_ids) if current else []
        destination = supporting if signal.polarity is SignalPolarity.POSITIVE else opposing
        if signal.signal_id not in destination:
            destination.append(signal.signal_id)
        state = ConceptState(
            user_id=signal.user_id,
            book_id=signal.book_id,
            book_version_id=signal.book_version_id,
            concept_id=signal.concept_id,
            dimension=signal.dimension,
            state=target,
            supporting_signal_ids=supporting,
            opposing_signal_ids=opposing,
            last_signal_id=signal.signal_id,
            last_signal_polarity=signal.polarity,
            last_signal_strength=signal.strength,
            row_version=(current.row_version + 1) if current else 1,
            updated_at=signal.created_at,
        )
        self.states[key] = state

    @staticmethod
    def _target_state(
        current: ConceptState | None, signal: LearningSignal
    ) -> UnderstandingState | None:
        old = current.state if current else UnderstandingState.UNKNOWN
        signal_type = signal.signal_type
        if signal_type in {
            LearningSignalType.EXPLICIT_CONFUSION,
            LearningSignalType.REPEATED_BASIC_QUESTION,
        }:
            return UnderstandingState.BLOCKED
        if signal_type in {
            LearningSignalType.SELF_REPORT_CLEAR,
            LearningSignalType.CORRECT_RESTATEMENT,
        }:
            return UnderstandingState.VERIFIED if old is UnderstandingState.VERIFIED else UnderstandingState.PARTIAL
        if signal_type in {
            LearningSignalType.CORRECT_RECONSTRUCTION,
            LearningSignalType.CORRECT_APPLICATION,
        }:
            return UnderstandingState.VERIFIED
        if signal_type is LearningSignalType.MISAPPLICATION:
            return UnderstandingState.PARTIAL if old is not UnderstandingState.UNKNOWN else UnderstandingState.BLOCKED
        return None

    def get_state(
        self,
        *,
        user_id: UUID,
        book_id: UUID,
        book_version_id: UUID,
        concept_id: UUID,
        dimension: LearningDimension,
    ) -> ConceptState:
        self._require_concept(concept_id, user_id, book_id, book_version_id)
        return self.states.get(
            (user_id, book_id, book_version_id, concept_id, dimension),
            ConceptState(
                user_id=user_id,
                book_id=book_id,
                book_version_id=book_version_id,
                concept_id=concept_id,
                dimension=dimension,
            ),
        )

    def list_states(
        self, *, user_id: UUID, book_id: UUID, book_version_id: UUID
    ) -> list[ConceptState]:
        scope = _scope_key(user_id, book_id, book_version_id)
        return sorted(
            [
                state
                for state in self.states.values()
                if _scope_key(state.user_id, state.book_id, state.book_version_id) == scope
            ],
            key=lambda item: (str(item.concept_id), item.dimension.value),
        )

    def build_profile_view(
        self, *, user_id: UUID, book_id: UUID, book_version_id: UUID
    ) -> BookLearnerProfileView:
        """Build a view on demand; no profile collection is maintained."""

        episodes = [
            episode
            for episode in self.episodes.values()
            if _scope_key(episode.user_id, episode.book_id, episode.book_version_id)
            == _scope_key(user_id, book_id, book_version_id)
        ]
        unresolved = [
            episode.question_summary
            for episode in sorted(episodes, key=lambda item: item.created_at, reverse=True)
            if episode.status is EpisodeStatus.UNCONFIRMED
        ][:20]
        unconfirmed_count = sum(
            1 for episode in episodes if episode.status is EpisodeStatus.UNCONFIRMED
        )
        return BookLearnerProfileView(
            user_id=user_id,
            book_id=book_id,
            book_version_id=book_version_id,
            concept_states=self.list_states(
                user_id=user_id, book_id=book_id, book_version_id=book_version_id
            ),
            unresolved_questions=unresolved,
            unconfirmed_episode_count=unconfirmed_count,
        )

    def clear_scope(self, *, user_id: UUID, book_id: UUID) -> int:
        """Delete all book-learning memory for one user/book scope."""

        ids = {
            "concepts": [
                key
                for key, value in self.concepts.items()
                if value.user_id == user_id and value.book_id == book_id
            ],
            "episodes": [
                key
                for key, value in self.episodes.items()
                if value.user_id == user_id and value.book_id == book_id
            ],
            "signals": [
                key
                for key, value in self.signals.items()
                if value.user_id == user_id and value.book_id == book_id
            ],
        }
        for key in ids["concepts"]:
            del self.concepts[key]
        for key in ids["episodes"]:
            del self.episodes[key]
        for key in ids["signals"]:
            del self.signals[key]
        self.rebuild_states()
        return sum(len(value) for value in ids.values())

    def rebuild_states(self) -> None:
        """Recompute projections from accepted signals in a stable order."""

        self.states.clear()
        accepted = sorted(
            (
                signal
                for signal in self.signals.values()
                if signal.validation_status is SignalValidation.ACCEPTED
            ),
            key=lambda item: (item.created_at, item.signal_id.hex),
        )
        for signal in accepted:
            self._apply_accepted_signal(signal)

    def signals_for_scope(
        self, *, user_id: UUID, book_id: UUID, book_version_id: UUID
    ) -> Iterable[LearningSignal]:
        scope = _scope_key(user_id, book_id, book_version_id)
        return tuple(
            signal
            for signal in self.signals.values()
            if _scope_key(signal.user_id, signal.book_id, signal.book_version_id) == scope
        )
