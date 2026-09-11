from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from reading_agent.book_memory import (
    BookConcept,
    BookLearnerProfileView,
    BookMemoryStore,
    ConceptStatus,
    EpisodeStatus,
    LearningDimension,
    LearningEpisode,
    LearningSignal,
    LearningSignalType,
    SignalPolarity,
    SignalSource,
    SignalStrength,
    SignalValidation,
    UnderstandingState,
)
from reading_agent.contracts import FrictionType, LearningGoal, ResponseStrategy


UTC = timezone.utc


def _scope() -> dict[str, UUID]:
    return {"user_id": uuid4(), "book_id": uuid4(), "book_version_id": uuid4()}


def _concept(scope: dict[str, UUID], *, name: str = "因果关系", **updates: object) -> BookConcept:
    values: dict[str, object] = {"status": ConceptStatus.ACTIVE, **updates}
    return BookConcept(
        **scope,
        canonical_name=name,
        **values,
    )


def _episode(scope: dict[str, UUID], concept_id: UUID | None) -> LearningEpisode:
    return LearningEpisode(
        **scope,
        conversation_id=uuid4(),
        answer_run_id=uuid4(),
        concept_id=concept_id,
        learning_goal=LearningGoal.UNDERSTAND,
        friction_type=FrictionType.LOGIC,
        question_summary="我不明白作者为什么能从前提推出结论",
        response_strategy=ResponseStrategy.RECONSTRUCT,
    )


def _signal(
    scope: dict[str, UUID],
    episode_id: UUID,
    concept_id: UUID,
    signal_type: LearningSignalType,
    *,
    dimension: LearningDimension = LearningDimension.ARGUMENT,
    polarity: SignalPolarity,
    strength: SignalStrength = SignalStrength.MEDIUM,
    source_kind: SignalSource = SignalSource.EXPLICIT_USER,
    quote: str | None = "用户原话",
    created_at: datetime | None = None,
    validation_status: SignalValidation = SignalValidation.ACCEPTED,
) -> LearningSignal:
    return LearningSignal(
        **scope,
        episode_id=episode_id,
        concept_id=concept_id,
        dimension=dimension,
        signal_type=signal_type,
        polarity=polarity,
        strength=strength,
        source_kind=source_kind,
        evidence_quote=quote,
        created_at=created_at or datetime.now(UTC),
        validation_status=validation_status,
    )


def test_concept_alias_is_stable_and_scope_bound() -> None:
    scope = _scope()
    store = BookMemoryStore()
    concept = store.add_concept(_concept(scope, aliases=["因果", "causality"]))

    assert store.match_concept(**scope, label="Causality") == concept
    other_scope = {**scope, "user_id": uuid4()}
    assert store.match_concept(**other_scope, label="因果关系") is None


def test_unknown_label_can_start_as_provisional_concept() -> None:
    scope = _scope()
    store = BookMemoryStore()
    assert store.match_concept(**scope, label="新的论证概念") is None
    concept = store.add_concept(_concept(scope, name="新的论证概念", status=ConceptStatus.PROVISIONAL))

    assert store.match_concept(**scope, label="新的论证概念") == concept
    assert concept.status is ConceptStatus.PROVISIONAL


def test_confirming_the_problem_does_not_upgrade_understanding() -> None:
    scope = _scope()
    store = BookMemoryStore()
    concept = store.add_concept(_concept(scope))
    episode = store.commit_episode(_episode(scope, concept.concept_id), completed=True)
    assert episode is not None
    signal = _signal(
        scope,
        episode.episode_id,
        concept.concept_id,
        LearningSignalType.PROBLEM_CONFIRMED,
        polarity=SignalPolarity.POSITIVE,
        quote="对，就是这个问题",
    )

    store.add_signal(signal)
    assert store.get_state(**scope, concept_id=concept.concept_id, dimension=LearningDimension.ARGUMENT).state is UnderstandingState.UNKNOWN
    assert store.episodes[episode.episode_id].status is EpisodeStatus.CONFIRMED


def test_explicit_confusion_moves_argument_to_blocked() -> None:
    scope = _scope()
    store = BookMemoryStore()
    concept = store.add_concept(_concept(scope))
    episode = store.commit_episode(_episode(scope, concept.concept_id), completed=True)
    assert episode is not None

    store.add_signal(
        _signal(
            scope,
            episode.episode_id,
            concept.concept_id,
            LearningSignalType.EXPLICIT_CONFUSION,
            polarity=SignalPolarity.NEGATIVE,
            strength=SignalStrength.STRONG,
            quote="我还是不懂作者为什么能这样推",
        )
    )
    assert store.get_state(**scope, concept_id=concept.concept_id, dimension=LearningDimension.ARGUMENT).state is UnderstandingState.BLOCKED


def test_self_report_clear_reaches_partial_not_verified() -> None:
    scope = _scope()
    store = BookMemoryStore()
    concept = store.add_concept(_concept(scope))
    episode = store.commit_episode(_episode(scope, concept.concept_id), completed=True)
    assert episode is not None

    first_episode = episode
    store.add_signal(
        _signal(
            scope,
            first_episode.episode_id,
            concept.concept_id,
            LearningSignalType.EXPLICIT_CONFUSION,
            polarity=SignalPolarity.NEGATIVE,
            quote="我没懂",
        )
    )
    second_episode = store.commit_episode(_episode(scope, concept.concept_id), completed=True)
    assert second_episode is not None
    store.add_signal(
        _signal(
            scope,
            second_episode.episode_id,
            concept.concept_id,
            LearningSignalType.SELF_REPORT_CLEAR,
            polarity=SignalPolarity.POSITIVE,
            quote="现在好像懂了",
        )
    )
    assert store.get_state(**scope, concept_id=concept.concept_id, dimension=LearningDimension.ARGUMENT).state is UnderstandingState.PARTIAL


def test_verified_requires_observable_learning_behavior() -> None:
    scope = _scope()
    store = BookMemoryStore()
    concept = store.add_concept(_concept(scope))
    episode = store.commit_episode(_episode(scope, concept.concept_id), completed=True)
    assert episode is not None

    clear = _signal(
        scope,
        episode.episode_id,
        concept.concept_id,
        LearningSignalType.SELF_REPORT_CLEAR,
        polarity=SignalPolarity.POSITIVE,
        quote="我现在懂了",
    )
    store.add_signal(clear)
    assert store.get_state(**scope, concept_id=concept.concept_id, dimension=LearningDimension.ARGUMENT).state is UnderstandingState.PARTIAL

    store.add_signal(
        _signal(
            scope,
            episode.episode_id,
            concept.concept_id,
            LearningSignalType.CORRECT_RECONSTRUCTION,
            polarity=SignalPolarity.POSITIVE,
            strength=SignalStrength.STRONG,
            source_kind=SignalSource.OBSERVED_BEHAVIOR,
            quote="用户独立重建了从前提到结论的两步论证",
        )
    )
    assert store.get_state(**scope, concept_id=concept.concept_id, dimension=LearningDimension.ARGUMENT).state is UnderstandingState.VERIFIED


def test_cancelled_turn_does_not_commit_episode_or_signal() -> None:
    scope = _scope()
    store = BookMemoryStore()
    concept = store.add_concept(_concept(scope))
    episode = _episode(scope, concept.concept_id)

    assert store.commit_episode(episode, completed=False) is None
    assert episode.episode_id not in store.episodes
    assert store.signals == {}


def test_equal_strength_same_time_conflicts_and_projection_rebuilds() -> None:
    scope = _scope()
    store = BookMemoryStore()
    concept = store.add_concept(_concept(scope))
    episode = store.commit_episode(_episode(scope, concept.concept_id), completed=True)
    assert episode is not None
    moment = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

    store.add_signal(
        _signal(
            scope,
            episode.episode_id,
            concept.concept_id,
            LearningSignalType.SELF_REPORT_CLEAR,
            polarity=SignalPolarity.POSITIVE,
            quote="我懂了",
            created_at=moment,
        )
    )
    store.add_signal(
        _signal(
            scope,
            episode.episode_id,
            concept.concept_id,
            LearningSignalType.EXPLICIT_CONFUSION,
            polarity=SignalPolarity.NEGATIVE,
            quote="其实我还是不懂",
            created_at=moment,
        )
    )
    before = store.get_state(**scope, concept_id=concept.concept_id, dimension=LearningDimension.ARGUMENT)
    profile = store.build_profile_view(**scope)
    assert before.state is UnderstandingState.CONFLICTED
    assert isinstance(profile, BookLearnerProfileView)
    assert profile.unconfirmed_episode_count == 1
    assert not hasattr(store, "profiles")

    store.rebuild_states()
    after = store.get_state(**scope, concept_id=concept.concept_id, dimension=LearningDimension.ARGUMENT)
    assert after.state is before.state
    assert after.supporting_signal_ids == before.supporting_signal_ids
    assert after.opposing_signal_ids == before.opposing_signal_ids


def test_snapshot_round_trip_rebuilds_only_accepted_projection() -> None:
    scope = _scope()
    store = BookMemoryStore()
    concept = store.add_concept(_concept(scope))
    episode = store.commit_episode(_episode(scope, concept.concept_id), completed=True)
    assert episode is not None
    store.add_signal(
        _signal(
            scope,
            episode.episode_id,
            concept.concept_id,
            LearningSignalType.EXPLICIT_CONFUSION,
            polarity=SignalPolarity.NEGATIVE,
            validation_status=SignalValidation.ACCEPTED,
        )
    )

    restored = BookMemoryStore()
    restored.restore(store.dump())

    assert restored.concepts == store.concepts
    assert restored.episodes == store.episodes
    assert restored.signals == store.signals
    assert restored.get_state(
        **scope, concept_id=concept.concept_id, dimension=LearningDimension.ARGUMENT
    ).state is UnderstandingState.BLOCKED


def test_clear_scope_removes_only_one_books_memory() -> None:
    target = _scope()
    other = _scope()
    store = BookMemoryStore()
    target_concept = store.add_concept(_concept(target))
    other_concept = store.add_concept(_concept(other, name="其他概念"))
    target_episode = store.commit_episode(_episode(target, target_concept.concept_id), completed=True)
    other_episode = store.commit_episode(_episode(other, other_concept.concept_id), completed=True)
    assert target_episode is not None and other_episode is not None
    store.add_signal(
        _signal(
            target,
            target_episode.episode_id,
            target_concept.concept_id,
            LearningSignalType.EXPLICIT_CONFUSION,
            polarity=SignalPolarity.NEGATIVE,
        )
    )

    assert store.clear_scope(user_id=target["user_id"], book_id=target["book_id"]) == 3
    assert not store.concepts or set(store.concepts) == {other_concept.concept_id}
    assert not store.episodes or set(store.episodes) == {other_episode.episode_id}
    assert store.signals == {}
