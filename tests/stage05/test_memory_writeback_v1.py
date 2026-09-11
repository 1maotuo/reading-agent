from __future__ import annotations

from uuid import UUID, uuid4

from reading_agent.book_memory import BookMemoryStore, SignalValidation
from reading_agent.contracts import (
    CognitiveFrame,
    CognitiveMode,
    ComprehensionState,
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
)
from reading_agent.memory_writeback import BookMemoryWriter, WritebackReason


def _scope() -> dict[str, UUID]:
    return {"user_id": uuid4(), "book_id": uuid4(), "book_version_id": uuid4()}


def _intent(state: ComprehensionState) -> IntentFrame:
    return IntentFrame(
        route=DialogueRoute.BOOK_DIALOGUE,
        relation=DialogueRelation.FOLLOWUP,
        goals=[DialogueGoal.EXPLAIN],
        context_sources=[ContextSource.CURRENT_VIEW],
        scope=IntentScope.CURRENT_BOOK,
        tasks=[IntentTask.EXPLAIN],
        cognition=CognitiveFrame(
            mode=CognitiveMode.LEARNING,
            learning_goal=LearningGoal.UNDERSTAND,
            comprehension_state=state,
            friction_type=FrictionType.LOGIC,
            evidence_quotes=["我还是不明白这一步"],
            confidence=0.9,
        ),
        response_strategy=ResponseStrategy.RECONSTRUCT,
    )


def test_non_book_turn_is_not_written() -> None:
    scope = _scope()
    result = BookMemoryWriter(BookMemoryStore()).record_completed_turn(
        intent=_intent(ComprehensionState.CONFUSED).model_copy(
            update={"route": DialogueRoute.OPEN_DIALOGUE}
        ),
        **scope,
        conversation_id=uuid4(),
        answer_run_id=uuid4(),
        question="你好",
    )

    assert result.committed is False
    assert result.reason is WritebackReason.ROUTE_NOT_BOOK


def test_clear_or_unknown_turn_does_not_create_learning_episode() -> None:
    store = BookMemoryStore()
    scope = _scope()
    result = BookMemoryWriter(store).record_completed_turn(
        intent=_intent(ComprehensionState.PARTIAL),
        **scope,
        conversation_id=uuid4(),
        answer_run_id=uuid4(),
        question="我大概懂了",
    )

    assert result.committed is False
    assert result.reason is WritebackReason.NO_LEARNING_SIGNAL
    assert store.episodes == {}
    assert store.signals == {}


def test_confusion_writes_candidate_and_is_idempotent() -> None:
    store = BookMemoryStore()
    scope = _scope()
    run_id = uuid4()
    writer = BookMemoryWriter(store)
    first = writer.record_completed_turn(
        intent=_intent(ComprehensionState.CONFUSED),
        **scope,
        conversation_id=uuid4(),
        answer_run_id=run_id,
        question="我还是不明白作者为什么能从前提推出结论",
    )
    second = writer.record_completed_turn(
        intent=_intent(ComprehensionState.CONFUSED),
        **scope,
        conversation_id=uuid4(),
        answer_run_id=run_id,
        question="我还是不明白作者为什么能从前提推出结论",
    )

    assert first.committed is True
    assert second.episode_id == first.episode_id
    assert second.signal_id == first.signal_id
    assert len(store.episodes) == 1
    assert len(store.signals) == 1
    signal = next(iter(store.signals.values()))
    assert signal.validation_status is SignalValidation.CANDIDATE
    assert store.states == {}


def test_disabled_memory_does_not_write() -> None:
    store = BookMemoryStore()
    result = BookMemoryWriter(store).record_completed_turn(
        intent=_intent(ComprehensionState.CONFUSED),
        **_scope(),
        conversation_id=uuid4(),
        answer_run_id=uuid4(),
        question="我还是不懂",
        memory_enabled=False,
    )

    assert result.committed is False
    assert result.reason is WritebackReason.MEMORY_DISABLED
    assert store.episodes == {}
