from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from reading_agent.book_memory import (
    BookConcept,
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
from reading_agent.contracts import (
    ComprehensionState,
    CognitiveMode,
    ContextSource,
    DialogueGoal,
    DialogueRelation,
    DialogueRoute,
    FrictionType,
    IntentFrame,
    IntentScope,
    IntentTask,
    LearningGoal,
    ResponseStrategy,
    CognitiveFrame,
)
from reading_agent.memory_plan import MemoryKind, MemoryPlanner, MemoryRetriever


UTC = timezone.utc


def _scope() -> dict[str, UUID]:
    return {"user_id": uuid4(), "book_id": uuid4(), "book_version_id": uuid4()}


def _intent(
    route: DialogueRoute = DialogueRoute.BOOK_DIALOGUE,
    *,
    comprehension: ComprehensionState = ComprehensionState.PARTIAL,
) -> IntentFrame:
    if route is DialogueRoute.BOOK_DIALOGUE:
        return IntentFrame(
            route=route,
            relation=DialogueRelation.NEW,
            goals=[DialogueGoal.EXPLAIN],
            context_sources=[ContextSource.CURRENT_VIEW],
            scope=IntentScope.CURRENT_BOOK,
            tasks=[IntentTask.EXPLAIN],
            cognition=CognitiveFrame(
                mode=CognitiveMode.LEARNING,
                learning_goal=LearningGoal.UNDERSTAND,
                comprehension_state=comprehension,
                friction_type=FrictionType.LOGIC,
                confidence=0.8,
            ),
            response_strategy=ResponseStrategy.RECONSTRUCT,
        )
    return IntentFrame(
        route=route,
        relation=DialogueRelation.NEW,
        context_sources=[ContextSource.NONE],
        scope=IntentScope.OPEN if route is DialogueRoute.OPEN_DIALOGUE else IntentScope.AMBIGUOUS,
        tasks=[],
        response_strategy=ResponseStrategy.CONVERSE
        if route is DialogueRoute.OPEN_DIALOGUE
        else ResponseStrategy.CLARIFY,
        requires_evidence=False,
    )


def _concept(scope: dict[str, UUID], *, name: str = "论证链") -> BookConcept:
    return BookConcept(**scope, canonical_name=name, status=ConceptStatus.ACTIVE)


def _episode(
    scope: dict[str, UUID],
    concept_id: UUID | None,
    *,
    question: str,
    created_at: datetime,
    status: EpisodeStatus = EpisodeStatus.CONFIRMED,
) -> LearningEpisode:
    return LearningEpisode(
        **scope,
        conversation_id=uuid4(),
        answer_run_id=uuid4(),
        concept_id=concept_id,
        learning_goal=LearningGoal.UNDERSTAND,
        friction_type=FrictionType.LOGIC,
        question_summary=question,
        response_strategy=ResponseStrategy.RECONSTRUCT,
        status=status,
        created_at=created_at,
    )


def _accepted_signal(
    scope: dict[str, UUID], episode: LearningEpisode, concept_id: UUID, *, blocked: bool
) -> LearningSignal:
    return LearningSignal(
        **scope,
        episode_id=episode.episode_id,
        concept_id=concept_id,
        dimension=LearningDimension.ARGUMENT,
        signal_type=LearningSignalType.EXPLICIT_CONFUSION
        if blocked
        else LearningSignalType.CORRECT_RECONSTRUCTION,
        polarity=SignalPolarity.NEGATIVE if blocked else SignalPolarity.POSITIVE,
        strength=SignalStrength.STRONG,
        source_kind=SignalSource.EXPLICIT_USER if blocked else SignalSource.OBSERVED_BEHAVIOR,
        evidence_quote="用户原话",
        created_at=episode.created_at,
        validation_status=SignalValidation.ACCEPTED,
    )


def test_non_book_route_does_not_read_book_memory() -> None:
    scope = _scope()
    plan = MemoryPlanner.build(
        intent=_intent(DialogueRoute.OPEN_DIALOGUE), **scope, query="你好"
    )

    assert plan.kinds == []
    assert plan.token_budget == 0


def test_book_plan_uses_concept_state_before_recent_episodes() -> None:
    scope = _scope()
    concept = _concept(scope)
    plan = MemoryPlanner.build(
        intent=_intent(), **scope, query="为什么能推出结论", concept_id=concept.concept_id
    )

    assert plan.kinds == [MemoryKind.CONCEPT_STATE, MemoryKind.LEARNING_EPISODE]
    assert plan.kind_limits[MemoryKind.CONCEPT_STATE] == 1
    assert plan.kind_limits[MemoryKind.LEARNING_EPISODE] == 3
    assert plan.token_budget == 900


def test_blocked_cognition_keeps_unconfirmed_episode_for_clarification() -> None:
    scope = _scope()
    plan = MemoryPlanner.build(
        intent=_intent(comprehension=ComprehensionState.CONFUSED),
        **scope,
        query="我还是不懂",
    )

    assert plan.include_unconfirmed is True
    assert "未确认" in "".join(plan.notes)


def test_retrieval_isolation_happens_before_ranking() -> None:
    scope = _scope()
    other = {**scope, "user_id": uuid4()}
    store = BookMemoryStore()
    wanted = _concept(scope)
    store.add_concept(wanted)
    store.add_concept(_concept(other, name="论证链"))
    episode = store.commit_episode(
        _episode(
            scope,
            wanted.concept_id,
            question="作者为什么能从前提推出结论",
            created_at=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        ),
        completed=True,
    )
    assert episode is not None
    plan = MemoryPlanner.build(intent=_intent(), **scope, query="论证链")
    bundle = MemoryRetriever(store).retrieve(plan)

    assert bundle.hits
    assert all(hit.memory_id == episode.episode_id for hit in bundle.hits)
    assert bundle.excluded_count == 0


def test_exact_concept_match_outscores_unrelated_recent_episode() -> None:
    scope = _scope()
    store = BookMemoryStore()
    concept = store.add_concept(_concept(scope))
    older = store.commit_episode(
        _episode(
            scope,
            concept.concept_id,
            question="作者为什么能从前提推出结论",
            created_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        ),
        completed=True,
    )
    recent = store.commit_episode(
        _episode(
            scope,
            None,
            question="今天的天气和晚饭怎么安排",
            created_at=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        ),
        completed=True,
    )
    assert older is not None and recent is not None
    plan = MemoryPlanner.build(
        intent=_intent(), **scope, query="天气", concept_id=concept.concept_id
    )
    bundle = MemoryRetriever(store).retrieve(plan)

    assert bundle.hits[0].memory_id == older.episode_id


def test_conflicted_state_is_returned_with_explicit_flag() -> None:
    scope = _scope()
    store = BookMemoryStore()
    concept = store.add_concept(_concept(scope))
    moment = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    first = store.commit_episode(
        _episode(scope, concept.concept_id, question="我懂了吗", created_at=moment),
        completed=True,
    )
    assert first is not None
    store.add_signal(_accepted_signal(scope, first, concept.concept_id, blocked=False))
    store.add_signal(_accepted_signal(scope, first, concept.concept_id, blocked=True))
    plan = MemoryPlanner.build(
        intent=_intent(), **scope, query="论证链", concept_id=concept.concept_id
    )
    bundle = MemoryRetriever(store).retrieve(plan)

    state_hits = [hit for hit in bundle.hits if hit.kind is MemoryKind.CONCEPT_STATE]
    assert len(state_hits) == 1
    assert state_hits[0].conflicted is True
    assert state_hits[0].source_status == UnderstandingState.CONFLICTED.value


def test_kind_limit_and_token_budget_are_visible_and_deterministic() -> None:
    scope = _scope()
    store = BookMemoryStore()
    concept = store.add_concept(_concept(scope))
    for offset in range(4):
        episode = store.commit_episode(
            _episode(
                scope,
                concept.concept_id,
                question=f"第{offset}次我想理解论证链",
                created_at=datetime(2026, 9, 11, 12, offset, tzinfo=UTC),
            ),
            completed=True,
        )
        assert episode is not None
    plan = MemoryPlanner.build(intent=_intent(), **scope, query="论证链")
    first = MemoryRetriever(store).retrieve(plan)
    second = MemoryRetriever(store).retrieve(plan)

    assert len([hit for hit in first.hits if hit.kind is MemoryKind.LEARNING_EPISODE]) == 3
    assert first.pruned_count == 1
    assert [hit.memory_id for hit in first.hits] == [hit.memory_id for hit in second.hits]
    assert first.estimated_tokens <= plan.token_budget


def test_profile_is_on_demand_and_retrieval_has_no_profile_storage_side_effect() -> None:
    scope = _scope()
    store = BookMemoryStore()
    concept = store.add_concept(_concept(scope))
    episode = store.commit_episode(
        _episode(
            scope,
            concept.concept_id,
            question="我想知道论证链",
            created_at=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        ),
        completed=True,
    )
    assert episode is not None
    store.add_signal(_accepted_signal(scope, episode, concept.concept_id, blocked=True))
    plan = MemoryPlanner.build(intent=_intent(), **scope, query="论证链")
    bundle = MemoryRetriever(store).retrieve(plan)

    assert not hasattr(store, "profiles")
    assert all(hit.kind is not MemoryKind.BOOK_PROFILE for hit in bundle.hits)
