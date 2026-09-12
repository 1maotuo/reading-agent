from __future__ import annotations

from reading_agent.contracts import (
    CognitiveMode,
    ComprehensionState,
    ContextNeed,
    DialogueRelation,
    DialogueRoute,
    ExternalAccess,
    IntentScope,
    IntentTarget,
    IntentTargetKind,
    IntentTask,
    FrictionType,
    ResponseStrategy,
    TopicSource,
)
from reading_agent.dialogue import HybridIntentRouter, compact_intent_context, teaching_guidance


class SemanticPlanModel:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def classify_intent(self, *, question, previous_route=None, routing_context=None):
        self.calls.append(
            {
                "question": question,
                "previous_route": previous_route,
                "routing_context": routing_context,
            }
        )
        if "书里的人工智能预测" in question:
            return {
                "route": "clarification",
                "scope": "ambiguous",
                "relation": "new",
                "tasks": ["analyze"],
                "context_needs": ["external_sources"],
                "external_access": "required",
                "clarification_required": True,
                "confidence": 0.95,
            }
        if "最新" in question:
            return {
                "route": "open_dialogue",
                "scope": "external",
                "relation": "new",
                "tasks": ["analyze"],
                "context_needs": ["external_sources"],
                "external_access": "required",
                "confidence": 0.96,
            }
        if "怎么理解" in question:
            return {
                "route": "open_dialogue",
                "scope": "open",
                "relation": "followup" if previous_route else "new",
                "goals": ["explain"],
                "tasks": ["explain"],
                "targets": [{"kind": "previous_turn", "identifier": "model-must-not-own-this"}],
                "confidence": 0.92,
            }
        if "不确定" in question:
            return {
                "route": "open_dialogue",
                "scope": "open",
                "relation": "new",
                "tasks": ["discuss"],
                "confidence": 0.2,
            }
        return {
            "route": "open_dialogue",
            "scope": "open",
            "relation": "new",
            "tasks": ["discuss"],
            "context_needs": ["user_preferences"],
            "external_access": "not_needed",
            "confidence": 0.98,
        }


class InvalidSemanticModel:
    model_name = "invalid-test-router"

    def classify_intent(self, **_kwargs):
        return {"route": "not-a-route"}


class V3SemanticModel:
    intent_model_name = "v3-test-router"

    def __init__(self, response):
        self.response = response
        self.calls = []

    def classify_intent(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def test_free_text_uses_semantics_and_returns_orthogonal_plan() -> None:
    model = SemanticPlanModel()
    intent = HybridIntentRouter(model).classify("你叫什么名字？", has_current_view=True)

    assert len(model.calls) == 1
    assert intent.route is DialogueRoute.OPEN_DIALOGUE
    assert intent.scope is IntentScope.OPEN
    assert intent.tasks == [IntentTask.DISCUSS]
    assert intent.external_access is ExternalAccess.NOT_NEEDED
    assert intent.requires_evidence is False
    assert intent.resolved_by == "semantic_model"


def test_explicit_selection_locks_passage_scope_but_semantics_resolve_task() -> None:
    model = SemanticPlanModel()
    target = IntentTarget(kind=IntentTargetKind.SELECTION, identifier="server-block-1", explicit=True)
    intent = HybridIntentRouter(model).classify(
        "怎么理解？",
        has_current_view=True,
        explicit_target=target,
        current_chapter_id="server-chapter-1",
    )

    assert len(model.calls) == 1
    assert model.calls[0]["routing_context"]["has_selection"] is True
    assert intent.route is DialogueRoute.BOOK_DIALOGUE
    assert intent.scope is IntentScope.PASSAGE
    assert intent.tasks == [IntentTask.EXPLAIN]
    assert intent.targets == [target]
    assert ContextNeed.QUOTED_TEXT in intent.context_needs
    assert ContextNeed.SURROUNDING_BOOK in intent.context_needs
    assert intent.requires_evidence is True


def test_followup_context_is_bounded_and_binds_previous_turn_without_model_ids() -> None:
    model = SemanticPlanModel()
    router = HybridIntentRouter(model)
    previous = router.classify("我们聊聊阅读习惯")
    intent = router.classify(
        "怎么理解？",
        previous_intent=previous,
        previous_question="上一轮问题" * 200,
        previous_answer="上一轮回答" * 300,
    )

    routing_context = model.calls[-1]["routing_context"]
    assert len(routing_context["previous_question"]) == 800
    assert len(routing_context["previous_answer"]) == 1000
    assert intent.relation is DialogueRelation.FOLLOWUP
    assert intent.targets == [
        IntentTarget(kind=IntentTargetKind.PREVIOUS_TURN, identifier=None, explicit=False)
    ]
    assert ContextNeed.RECENT_TURNS in intent.context_needs


def test_external_evidence_is_planned_without_selecting_a_tool() -> None:
    intent = HybridIntentRouter(SemanticPlanModel()).classify("这项研究到2026年的最新结论是什么？")

    assert intent.scope is IntentScope.EXTERNAL
    assert intent.route is DialogueRoute.OPEN_DIALOGUE
    assert intent.external_access is ExternalAccess.REQUIRED
    assert ContextNeed.EXTERNAL_SOURCES in intent.context_needs
    assert intent.requires_evidence is False  # Current runtime only gates verified book evidence.


def test_bound_current_book_referent_overrides_model_clarification() -> None:
    intent = HybridIntentRouter(SemanticPlanModel()).classify(
        "书里的人工智能预测到2026年还成立吗？请结合最新资料判断。",
        has_current_view=True,
        current_chapter_id="chapter-test",
    )

    assert intent.scope is IntentScope.MIXED
    assert intent.route is DialogueRoute.BOOK_DIALOGUE
    assert intent.tasks == [IntentTask.ANALYZE]
    assert intent.external_access is ExternalAccess.REQUIRED
    assert intent.clarification_required is False


def test_low_confidence_and_unavailable_model_fail_safe_without_forcing_book_route() -> None:
    low = HybridIntentRouter(SemanticPlanModel()).classify("这个问题我也不确定")
    assert low.route is DialogueRoute.CLARIFICATION
    assert low.scope is IntentScope.AMBIGUOUS
    assert low.clarification_required is True
    assert low.resolved_by == "semantic_model"

    fallback = HybridIntentRouter().classify("你叫什么名字？")
    assert fallback.route is DialogueRoute.OPEN_DIALOGUE
    assert fallback.scope is IntentScope.OPEN
    assert fallback.resolved_by == "degraded_fallback"
    assert fallback.degraded_reason == "model_unavailable"

    vague = HybridIntentRouter().classify("怎么理解？", has_current_view=True)
    assert vague.route is DialogueRoute.CLARIFICATION
    assert vague.clarification_required is True

    book = HybridIntentRouter().classify("作者这一章的核心论点是什么？", has_current_view=True)
    assert book.route is DialogueRoute.BOOK_DIALOGUE
    assert book.scope is IntentScope.CURRENT_BOOK

    invalid = HybridIntentRouter(InvalidSemanticModel()).classify("你叫什么名字？")
    assert invalid.route is DialogueRoute.OPEN_DIALOGUE
    assert invalid.resolved_by == "degraded_fallback"
    assert invalid.degraded_reason == "invalid_response"
    assert invalid.router_model == "invalid-test-router"


def test_v3_resolves_vague_followup_to_previous_topic_and_builds_learning_strategy() -> None:
    model = V3SemanticModel({
        "route": "book_dialogue",
        "relation": "followup",
        "goals": ["explain"],
        "scope": "ambiguous",
        "tasks": ["explain"],
        "targets": [],
        "topic_source": "previous_turn",
        "topic_confidence": 0.91,
        "cognition": {
            "mode": "learning",
            "learning_goal": "verify",
            "comprehension_state": "partial",
            "friction_type": "logic",
            "evidence_quotes": ["我已经理解了，但不确定"],
            "confidence": 0.88,
        },
        "context_needs": ["recent_turns", "current_book"],
        "external_access": "not_needed",
        "clarification_required": False,
        "confidence": 0.86,
    })
    previous = HybridIntentRouter().classify("作者这一章的核心论点是什么？", has_current_view=True)
    intent = HybridIntentRouter(model).classify(
        "这个怎么回事？",
        has_current_view=True,
        current_chapter_id="chapter-1",
        previous_intent=previous,
        previous_question="我已经理解了，但不确定作者这一步为什么成立",
        previous_answer="作者先提出前提，再推出结论。",
    )

    assert intent.route is DialogueRoute.BOOK_DIALOGUE
    assert intent.clarification_required is False
    assert intent.topic_source is TopicSource.PREVIOUS_TURN
    assert intent.cognition.mode is CognitiveMode.LEARNING
    assert intent.cognition.comprehension_state is ComprehensionState.PARTIAL
    assert intent.cognition.friction_type is FrictionType.LOGIC
    assert intent.response_strategy is ResponseStrategy.VERIFY
    assert model.calls[0]["routing_context"]["topic_candidates"][0]["key"] == "previous_turn"


def test_v3_rejects_unverifiable_cognitive_evidence_without_forcing_clarification() -> None:
    model = V3SemanticModel({
        "route": "book_dialogue",
        "relation": "new",
        "goals": ["explain"],
        "scope": "current_book",
        "tasks": ["explain"],
        "targets": [{"kind": "user_text", "identifier": "must-ignore"}],
        "topic_source": "none",
        "topic_confidence": 0.2,
        "cognition": {
            "mode": "learning",
            "learning_goal": "understand",
            "comprehension_state": "confused",
            "friction_type": "logic",
            "evidence_quotes": ["用户从未说过的话"],
            "confidence": 0.99,
        },
        "context_needs": ["current_book"],
        "external_access": "not_needed",
        "clarification_required": False,
        "confidence": 0.8,
    })
    intent = HybridIntentRouter(model).classify("作者为什么这样安排？", has_current_view=True)

    assert intent.route is DialogueRoute.BOOK_DIALOGUE
    assert intent.cognition.comprehension_state is ComprehensionState.UNKNOWN
    assert intent.cognition.friction_type is FrictionType.UNKNOWN
    assert intent.cognition.confidence <= 0.4
    assert intent.targets[0].identifier is None


def test_v3_does_not_apply_learning_labels_to_open_dialogue() -> None:
    model = V3SemanticModel({
        "route": "open_dialogue",
        "relation": "new",
        "goals": [],
        "scope": "open",
        "tasks": ["discuss"],
        "targets": [],
        "topic_source": "none",
        "topic_confidence": 0.0,
        "cognition": {
            "mode": "learning",
            "learning_goal": "understand",
            "comprehension_state": "confused",
            "friction_type": "term",
            "evidence_quotes": ["你好"],
            "confidence": 0.99,
        },
        "context_needs": ["user_preferences"],
        "external_access": "not_needed",
        "clarification_required": False,
        "confidence": 0.98,
    })
    intent = HybridIntentRouter(model).classify("你好，陪我聊聊。")

    assert intent.route is DialogueRoute.OPEN_DIALOGUE
    assert intent.cognition.mode is CognitiveMode.CONVERSATION
    assert intent.cognition.comprehension_state is ComprehensionState.UNKNOWN
    assert intent.response_strategy is ResponseStrategy.CONVERSE


def test_v3_only_clarifies_when_topic_is_truly_ambiguous() -> None:
    model = V3SemanticModel({
        "route": "clarification",
        "relation": "new",
        "goals": [],
        "scope": "ambiguous",
        "tasks": [],
        "targets": [],
        "topic_source": "ambiguous",
        "topic_confidence": 0.2,
        "cognition": {
            "mode": "none",
            "learning_goal": "unknown",
            "comprehension_state": "unknown",
            "friction_type": "unknown",
            "evidence_quotes": [],
            "confidence": 0.0,
        },
        "context_needs": ["recent_turns"],
        "external_access": "not_needed",
        "clarification_required": True,
        "confidence": 0.9,
    })
    intent = HybridIntentRouter(model).classify("这个怎么回事？")

    assert intent.route is DialogueRoute.CLARIFICATION
    assert intent.clarification_required is True
    assert intent.topic_source is TopicSource.AMBIGUOUS


def test_v3_degraded_fallback_keeps_previous_topic_for_deictic_followup() -> None:
    router = HybridIntentRouter()
    previous = router.classify("作者这一章的核心论点是什么？", has_current_view=True)
    intent = router.classify("这个怎么回事？", previous_intent=previous, has_current_view=True)

    assert intent.route is DialogueRoute.BOOK_DIALOGUE
    assert intent.clarification_required is False
    assert intent.topic_source is TopicSource.PREVIOUS_TURN


def test_v3_rejects_previous_turn_source_when_no_previous_turn_exists() -> None:
    model = V3SemanticModel({
        "route": "book_dialogue",
        "relation": "new",
        "goals": ["explain"],
        "scope": "current_book",
        "tasks": ["explain"],
        "targets": [],
        "topic_source": "previous_turn",
        "topic_confidence": 0.99,
        "cognition": {
            "mode": "learning",
            "learning_goal": "unknown",
            "comprehension_state": "unknown",
            "friction_type": "unknown",
            "evidence_quotes": [],
            "confidence": 0.0,
        },
        "context_needs": ["current_book"],
        "external_access": "not_needed",
        "clarification_required": False,
        "confidence": 0.9,
    })
    intent = HybridIntentRouter(model).classify("作者为什么这样写？", has_current_view=True)

    assert intent.topic_source is TopicSource.NONE
    assert intent.topic_confidence == 0.0


def test_v3_rejects_current_view_source_when_no_current_view_exists() -> None:
    model = V3SemanticModel({
        "route": "book_dialogue",
        "relation": "new",
        "goals": ["explain"],
        "scope": "current_book",
        "tasks": ["explain"],
        "targets": [],
        "topic_source": "current_view",
        "topic_confidence": 0.99,
        "cognition": {
            "mode": "learning",
            "learning_goal": "unknown",
            "comprehension_state": "unknown",
            "friction_type": "unknown",
            "evidence_quotes": [],
            "confidence": 0.0,
        },
        "context_needs": ["current_book"],
        "external_access": "not_needed",
        "clarification_required": False,
        "confidence": 0.9,
    })
    intent = HybridIntentRouter(model).classify("这里为什么重要？")

    assert intent.topic_source is TopicSource.NONE
    assert intent.topic_confidence == 0.0


def test_degraded_learning_signal_builds_a_small_teaching_instruction() -> None:
    intent = HybridIntentRouter().classify(
        "我还是不懂作者为什么能从前提推出这个结论",
        has_current_view=True,
    )

    assert intent.cognition.comprehension_state is ComprehensionState.CONFUSED
    assert intent.cognition.friction_type is FrictionType.LOGIC
    guidance = teaching_guidance(intent)
    assert "前提、推理步骤、结论" in guidance
    assert "先帮助，不立即考试" in guidance
    compact = compact_intent_context(intent)
    assert "context_needs" not in compact
    assert "理解状态=confused" in compact
