"""Server-owned conversation routing for the Stage 05 Agent Core preview.

Trusted UI events remain deterministic.  Free text can be classified by a
small semantic adapter, with this module's rules retained only as an explicit
degraded fallback when the model is unavailable or invalid.
"""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import Sequence
from typing import Any

from .contracts import (
    AnswerRunStatus,
    CognitiveFrame,
    CognitiveMode,
    ComprehensionState,
    ContextNeed,
    ContextSource,
    DialogueGoal,
    DialogueRelation,
    DialogueRoute,
    ExternalAccess,
    IntentFrame,
    IntentScope,
    IntentTarget,
    IntentTargetKind,
    IntentTask,
    FrictionType,
    LearningGoal,
    ResponseStrategy,
    TopicSource,
)


_DIRECT_UI_ACTION_TERMS = (
    "怎么划线",
    "如何划线",
    "怎么换章节",
    "如何换章节",
    "目录在哪",
    "怎么打开目录",
    "如何打开目录",
    "怎么返回书架",
    "如何返回书架",
    "进度怎么保存",
    "如何保存进度",
)
_UI_NOUNS = ("阅读器", "目录", "章节", "划线", "进度", "书架", "正文", "一起读")
_UI_ACTIONS = ("怎么用", "如何用", "打开", "关闭", "切换", "换章节", "划线", "返回", "保存", "在哪")
_OPEN_TERMS = (
    "你好",
    "嗨",
    "hello",
    "hi",
    "谢谢",
    "感谢",
    "你是谁",
    "你叫什么名字",
    "你叫啥",
    "你的名字",
    "陪我聊",
    "聊聊天",
    "早上好",
    "晚上好",
    "辛苦了",
    "我有点累",
    "我很累",
    "我很开心",
    "我有点难过",
)
_CONFUSION_TERMS = ("什么意思", "没懂", "不懂", "不明白", "看不懂", "怎么理解", "为什么", "卡住")
_CLEAR_TERMS = ("明白了", "懂了", "清楚了", "大概懂", "好像懂", "我理解")
_RESTATE_TERMS = ("我试着复述", "换句话说", "也就是说", "我的理解", "是不是可以理解为")
_BOOK_TERMS = (
    "这本书",
    "本书",
    "这一章",
    "这章",
    "原文",
    "作者",
    "观点",
    "论点",
    "论证",
    "段落",
    "书中",
    "书里",
    "概念",
    "主题",
    "情节",
    "人物",
    "举例",
    "解释",
    "总结",
    "分析",
    "批评",
    "质疑",
    "对比",
)
_CORRECTION_TERMS = ("不对", "不是这个意思", "我觉得不是", "你说错", "纠正一下", "应该是")
_FOLLOWUP_TERMS = (
    "为什么", "然后呢", "再说说", "举个例子", "具体一点", "什么意思", "怎么理解", "那",
    "换个说法", "继续深入", "我试着复述", "还是不懂", "还是没懂",
)
_LIGHT_OPEN_FOLLOWUPS = {"为什么", "然后呢", "再说说", "还有呢", "继续", "真的吗", "怎么了"}
_BOUND_BOOK_REFERENTS = ("这本书", "本书", "这一章", "本章", "书中", "书里")
_DEICTIC_TERMS = ("这个", "这段", "它", "刚才", "上面", "这里")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value.casefold())


def _contains_any(value: str, terms: tuple[str, ...]) -> bool:
    return any(term in value for term in terms)


def _is_reader_action(value: str) -> bool:
    if _contains_any(value, _DIRECT_UI_ACTION_TERMS):
        return True
    return _contains_any(value, _UI_NOUNS) and _contains_any(value, _UI_ACTIONS)


def _is_light_open_followup(value: str) -> bool:
    bare = re.sub(r"[？?！!。,.，、]+", "", value)
    return bare in _LIGHT_OPEN_FOLLOWUPS


def _has_bound_book_referent(value: str, *, has_current_view: bool) -> bool:
    """Resolve explicit book/chapter deixis only when the server bound a view."""

    return has_current_view and _contains_any(value, _BOUND_BOOK_REFERENTS)


def _goals(value: str) -> list[DialogueGoal]:
    goals: list[DialogueGoal] = []
    mappings = (
        (("解释", "什么意思", "怎么理解", "没懂", "不明白"), DialogueGoal.EXPLAIN),
        (("举例", "例子", "生活中", "比方说"), DialogueGoal.EXAMPLE),
        (("总结", "概括", "核心", "主旨"), DialogueGoal.SUMMARIZE),
        (("论证", "理由", "依据", "怎么证明"), DialogueGoal.ANALYZE_ARGUMENT),
        (("对比", "区别", "异同"), DialogueGoal.COMPARE),
        (("质疑", "批评", "靠谱吗", "前提"), DialogueGoal.CRITIQUE),
    )
    for terms, goal in mappings:
        if _contains_any(value, terms):
            goals.append(goal)
    return goals or [DialogueGoal.EXPLAIN]


def _topic_label(value: str) -> str | None:
    """Extract a conservative user-owned label for degraded routing."""

    quoted = re.search(r"[“「『\"]([^”」』\"]{2,80})[”」』\"]", value)
    if quoted:
        return quoted.group(1).strip()
    candidate = value
    for term in (
        *_CONFUSION_TERMS,
        *_CLEAR_TERMS,
        *_RESTATE_TERMS,
        "作者", "这本书", "本书", "这一章", "这章", "原文", "这个", "这段",
        "请", "能不能", "可以", "帮我", "一下", "吗", "呢",
    ):
        candidate = candidate.replace(term, "")
    candidate = re.sub(r"[？?！!。,.，、：:；;\s]+", "", candidate).strip()
    return candidate[:80] if 2 <= len(candidate) <= 80 else None


def _fallback_cognition(question: str, route: DialogueRoute) -> CognitiveFrame:
    """Keep explicit learning signals useful when the semantic model is down."""

    if route is not DialogueRoute.BOOK_DIALOGUE:
        return CognitiveFrame(
            mode=CognitiveMode.CONVERSATION if route is DialogueRoute.OPEN_DIALOGUE else CognitiveMode.NONE
        )
    value = _compact(question)
    if _contains_any(value, _CONFUSION_TERMS):
        state = ComprehensionState.CONFUSED
        confidence = 0.72
    elif _contains_any(value, _CLEAR_TERMS + _RESTATE_TERMS):
        state = ComprehensionState.PARTIAL
        confidence = 0.66
    else:
        state = ComprehensionState.UNKNOWN
        confidence = 0.0
    if _contains_any(value, ("术语", "词", "什么意思", "定义")):
        friction = FrictionType.TERM
    elif _contains_any(value, ("例子", "举例", "应用", "实际")):
        friction = FrictionType.EXAMPLE
    elif _contains_any(value, ("证据", "依据", "证明")):
        friction = FrictionType.EVIDENCE
    elif _contains_any(value, ("矛盾", "冲突", "不对")):
        friction = FrictionType.CONTRADICTION
    elif _contains_any(value, ("为什么", "推理", "论证", "前提", "结论")):
        friction = FrictionType.LOGIC
    else:
        friction = FrictionType.UNKNOWN
    goal = (
        LearningGoal.VERIFY
        if _contains_any(value, _CLEAR_TERMS + _RESTATE_TERMS)
        else LearningGoal.UNDERSTAND
    )
    return CognitiveFrame(
        mode=CognitiveMode.LEARNING,
        topic_label=_topic_label(question),
        learning_goal=goal,
        comprehension_state=state,
        friction_type=friction,
        evidence_quotes=[question.strip()[:160]] if state is not ComprehensionState.UNKNOWN else [],
        confidence=confidence,
    )


def compact_intent_context(intent: IntentFrame) -> str:
    """Return only answer-shaping intent data, not the full routing contract."""

    cognition = intent.cognition
    return (
        f"任务={','.join(item.value for item in intent.tasks) or 'discuss'}；"
        f"关系={intent.relation.value}；策略={intent.response_strategy.value}；"
        f"学习目标={cognition.learning_goal.value}；理解状态={cognition.comprehension_state.value}；"
        f"卡点={cognition.friction_type.value}；话题={cognition.topic_label or '未明确'}"
    )


def teaching_guidance(intent: IntentFrame, *, has_prior_learning_state: bool = False) -> str:
    """Translate cognition into one compact teaching move for the answer model."""

    if intent.route is not DialogueRoute.BOOK_DIALOGUE:
        return ""
    cognition = intent.cognition
    moves = {
        FrictionType.TERM: "先说明这个词在作者语境中的含义，再用一句通俗话和一个短例子解释。",
        FrictionType.TRANSLATION: "先还原原句含义，再解释译法差异，不把换词当成解释。",
        FrictionType.BACKGROUND: "只补理解当前原文所需的最少背景，并标明哪些不是书中原话。",
        FrictionType.LOGIC: "按前提、推理步骤、结论重建论证，明确指出用户卡住的连接。",
        FrictionType.EXAMPLE: "先给一个贴近生活的例子，再逐点映射回原文概念，并说明类比边界。",
        FrictionType.EVIDENCE: "区分作者结论、支持证据和仍然不能确定的部分。",
        FrictionType.CONTRADICTION: "把看似冲突的两点并列，检查语境、层级和隐含前提。",
        FrictionType.UNKNOWN: "先直接回应，再用最短路径解释关键概念与原文关系。",
    }
    state_moves = {
        ComprehensionState.CONFUSED: "用户明确卡住：先帮助，不立即考试；避免重复术语堆砌。",
        ComprehensionState.PARTIAL: "用户已有部分理解：保留正确部分，只补缺口，最后最多问一个轻量确认问题。",
        ComprehensionState.LIKELY_CLEAR: "用户可能已经理解：简短核对其复述，不直接宣称已经掌握。",
        ComprehensionState.UNKNOWN: "不要猜用户懂不懂；回答后只在确有必要时提出一个澄清问题。",
    }
    prior = "参考已召回的学习状态，避免从头重复讲解。" if has_prior_learning_state else ""
    return f"{moves[cognition.friction_type]}{state_moves[cognition.comprehension_state]}{prior}"


_GOAL_TASK = {
    DialogueGoal.EXPLAIN: IntentTask.EXPLAIN,
    DialogueGoal.EXAMPLE: IntentTask.EXAMPLE,
    DialogueGoal.SUMMARIZE: IntentTask.SUMMARIZE,
    DialogueGoal.ANALYZE_ARGUMENT: IntentTask.ANALYZE,
    DialogueGoal.COMPARE: IntentTask.COMPARE,
    DialogueGoal.CRITIQUE: IntentTask.CRITIQUE,
}


def _scope_for_route(route: DialogueRoute, *, has_selection: bool = False) -> IntentScope:
    if has_selection:
        return IntentScope.PASSAGE
    return {
        DialogueRoute.OPEN_DIALOGUE: IntentScope.OPEN,
        DialogueRoute.BOOK_DIALOGUE: IntentScope.CURRENT_BOOK,
        DialogueRoute.READER_ACTION: IntentScope.SYSTEM,
        DialogueRoute.CLARIFICATION: IntentScope.AMBIGUOUS,
    }[route]


def _route_for_scope(scope: IntentScope) -> DialogueRoute:
    if scope in {IntentScope.CURRENT_BOOK, IntentScope.PASSAGE, IntentScope.MIXED}:
        return DialogueRoute.BOOK_DIALOGUE
    if scope is IntentScope.SYSTEM:
        return DialogueRoute.READER_ACTION
    if scope is IntentScope.AMBIGUOUS:
        return DialogueRoute.CLARIFICATION
    return DialogueRoute.OPEN_DIALOGUE


def _tasks_for_goals(goals: Sequence[DialogueGoal], route: DialogueRoute) -> list[IntentTask]:
    if route is DialogueRoute.READER_ACTION:
        return [IntentTask.NAVIGATE]
    if route is DialogueRoute.OPEN_DIALOGUE:
        return [IntentTask.DISCUSS]
    if route is DialogueRoute.CLARIFICATION:
        return []
    return [_GOAL_TASK[item] for item in goals] or [IntentTask.EXPLAIN]


def _default_needs(
    *, scope: IntentScope, has_selection: bool, has_previous: bool
) -> list[ContextNeed]:
    needs: list[ContextNeed] = []
    if has_selection:
        needs.extend([ContextNeed.QUOTED_TEXT, ContextNeed.SURROUNDING_BOOK])
    elif scope in {IntentScope.CURRENT_BOOK, IntentScope.MIXED}:
        needs.append(ContextNeed.CURRENT_BOOK)
    if has_previous:
        needs.append(ContextNeed.RECENT_TURNS)
    if scope in {IntentScope.OPEN, IntentScope.CURRENT_BOOK, IntentScope.PASSAGE, IntentScope.MIXED}:
        needs.append(ContextNeed.USER_PREFERENCES)
    if scope in {IntentScope.EXTERNAL, IntentScope.MIXED}:
        needs.append(ContextNeed.EXTERNAL_SOURCES)
    return list(dict.fromkeys(needs))


def _external_for_scope(scope: IntentScope) -> ExternalAccess:
    if scope is IntentScope.EXTERNAL:
        return ExternalAccess.REQUIRED
    if scope is IntentScope.MIXED:
        return ExternalAccess.RECOMMENDED
    return ExternalAccess.NOT_NEEDED


def _response_strategy(
    *,
    route: DialogueRoute,
    goals: Sequence[DialogueGoal],
    cognition: CognitiveFrame | None,
) -> ResponseStrategy:
    if route is DialogueRoute.OPEN_DIALOGUE:
        return ResponseStrategy.CONVERSE
    if route is DialogueRoute.CLARIFICATION:
        return ResponseStrategy.CLARIFY
    if cognition is not None and cognition.mode is CognitiveMode.LEARNING:
        goal = cognition.learning_goal
        if goal is LearningGoal.VERIFY:
            return ResponseStrategy.VERIFY
        if goal is LearningGoal.CRITIQUE:
            return ResponseStrategy.CRITIQUE
        if goal is LearningGoal.APPLY:
            return ResponseStrategy.APPLY
    if DialogueGoal.CRITIQUE in goals:
        return ResponseStrategy.CRITIQUE
    if DialogueGoal.ANALYZE_ARGUMENT in goals:
        return ResponseStrategy.RECONSTRUCT
    return ResponseStrategy.EXPLAIN


def _valid_cognitive(
    raw: Any,
    *,
    route: DialogueRoute,
    question: str,
    previous_question: str | None,
    previous_answer: str | None,
) -> CognitiveFrame:
    """Normalize the model hypothesis; current text outranks old context."""

    if route is DialogueRoute.OPEN_DIALOGUE:
        return CognitiveFrame(mode=CognitiveMode.CONVERSATION, confidence=0.0)
    if route is not DialogueRoute.BOOK_DIALOGUE or not isinstance(raw, dict):
        return CognitiveFrame()
    try:
        goal = LearningGoal(raw.get("learning_goal", LearningGoal.UNKNOWN.value))
    except ValueError:
        goal = LearningGoal.UNKNOWN
    try:
        state = ComprehensionState(raw.get("comprehension_state", ComprehensionState.UNKNOWN.value))
    except ValueError:
        state = ComprehensionState.UNKNOWN
    try:
        friction = FrictionType(raw.get("friction_type", FrictionType.UNKNOWN.value))
    except ValueError:
        friction = FrictionType.UNKNOWN
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    # The assistant's prior answer can resolve a topic, but it is not evidence
    # of the user's cognitive state.
    allowed = [question, previous_question or ""]
    quotes: list[str] = []
    for item in (raw.get("evidence_quotes") or [])[:3]:
        if not isinstance(item, str):
            continue
        quote = item.strip()[:160]
        if quote and any(quote in source for source in allowed):
            quotes.append(quote)
    topic_label = str(raw.get("topic_label") or "").strip()[:120] or None
    if topic_label is not None:
        compact_label = _compact(topic_label)
        if not compact_label or not any(compact_label in _compact(source) for source in allowed):
            topic_label = None
    # No verifiable user wording means we do not label their comprehension.
    if not quotes and (state is not ComprehensionState.UNKNOWN or friction is not FrictionType.UNKNOWN):
        state = ComprehensionState.UNKNOWN
        friction = FrictionType.UNKNOWN
        confidence = min(confidence, 0.4)
    return CognitiveFrame(
        mode=CognitiveMode.LEARNING,
        topic_label=topic_label,
        learning_goal=goal,
        comprehension_state=state,
        friction_type=friction,
        evidence_quotes=quotes,
        confidence=confidence,
    )


def _frame(
    *,
    route: DialogueRoute,
    relation: DialogueRelation,
    goals: Sequence[DialogueGoal],
    context_sources: list[ContextSource],
    confidence: float,
    resolved_by: str,
    scope: IntentScope | None = None,
    tasks: Sequence[IntentTask] | None = None,
    targets: Sequence[IntentTarget] = (),
    context_needs: Sequence[ContextNeed] | None = None,
    external_access: ExternalAccess | None = None,
    clarification_required: bool | None = None,
    topic_source: TopicSource = TopicSource.NONE,
    topic_confidence: float = 0.0,
    cognition: CognitiveFrame | None = None,
) -> IntentFrame:
    resolved_scope = scope or _scope_for_route(route, has_selection=bool(targets))
    supplied_external = external_access or ExternalAccess.NOT_NEEDED
    if supplied_external is not ExternalAccess.NOT_NEEDED:
        if resolved_scope in {IntentScope.CURRENT_BOOK, IntentScope.PASSAGE}:
            resolved_scope = IntentScope.MIXED
        elif resolved_scope is IntentScope.OPEN:
            resolved_scope = IntentScope.EXTERNAL
    resolved_route = _route_for_scope(resolved_scope)
    resolved_goals = list(goals) if resolved_route is DialogueRoute.BOOK_DIALOGUE else []
    has_selection = any(
        item.kind in {IntentTargetKind.SELECTION, IntentTargetKind.HIGHLIGHT}
        for item in targets
    )
    baseline_needs = _default_needs(
        scope=resolved_scope,
        has_selection=has_selection,
        has_previous=ContextSource.PREVIOUS_TURN in context_sources,
    )
    resolved_needs = list(dict.fromkeys([*(context_needs or ()), *baseline_needs]))
    resolved_cognition = cognition or CognitiveFrame()
    return IntentFrame(
        route=resolved_route,
        relation=relation,
        goals=resolved_goals,
        context_sources=context_sources,
        scope=resolved_scope,
        tasks=list(tasks) if tasks is not None else _tasks_for_goals(resolved_goals, resolved_route),
        targets=list(targets),
        context_needs=resolved_needs,
        external_access=(
            _external_for_scope(resolved_scope)
            if supplied_external is ExternalAccess.NOT_NEEDED
            else supplied_external
        ),
        clarification_required=(
            resolved_scope is IntentScope.AMBIGUOUS
            if clarification_required is None
            else clarification_required
        ),
        confidence=confidence,
        topic_source=topic_source,
        topic_confidence=topic_confidence,
        cognition=resolved_cognition,
        response_strategy=_response_strategy(
            route=resolved_route,
            goals=resolved_goals,
            cognition=resolved_cognition,
        ),
        requires_evidence=resolved_route is DialogueRoute.BOOK_DIALOGUE,
        resolved_by=resolved_by,
    )


class IntentRouter:
    """Bounded fallback classifier used when semantic routing is unavailable."""

    def classify(
        self,
        question: str,
        *,
        has_highlight: bool = False,
        has_current_view: bool = False,
        previous_route: DialogueRoute | None = None,
    ) -> IntentFrame:
        value = _compact(question)
        context: list[ContextSource] = []
        if has_highlight:
            context.append(ContextSource.SELECTION)
        if has_current_view:
            context.append(ContextSource.CURRENT_VIEW)
        if previous_route is not None:
            context.append(ContextSource.PREVIOUS_TURN)
        if not context:
            context.append(ContextSource.NONE)

        has_book_semantics = _contains_any(value, _BOOK_TERMS)
        if _is_reader_action(value):
            route = DialogueRoute.READER_ACTION
        elif (
            previous_route is DialogueRoute.OPEN_DIALOGUE
            and _is_light_open_followup(value)
            and not has_highlight
            and not has_book_semantics
        ):
            route = DialogueRoute.OPEN_DIALOGUE
        elif _contains_any(value, _OPEN_TERMS) and not has_book_semantics:
            route = DialogueRoute.OPEN_DIALOGUE
        elif previous_route is not None and _contains_any(value, _FOLLOWUP_TERMS):
            route = previous_route
        elif previous_route is not None and _contains_any(value, _DEICTIC_TERMS):
            route = previous_route
        elif (
            _contains_any(value, _CONFUSION_TERMS)
            and not has_highlight
            and previous_route is None
            and not has_book_semantics
        ):
            route = DialogueRoute.CLARIFICATION
        elif has_book_semantics:
            route = DialogueRoute.BOOK_DIALOGUE
        else:
            # Preserve the frozen evidence gate for completely unknown text.
            # Known social turns are handled above; this path is observable as
            # degraded and must not create an unsupported open answer.
            route = DialogueRoute.BOOK_DIALOGUE

        if previous_route is not None and route is DialogueRoute.OPEN_DIALOGUE:
            relation = DialogueRelation.FOLLOWUP if _contains_any(value, _FOLLOWUP_TERMS) else DialogueRelation.NEW
        elif _contains_any(value, _CORRECTION_TERMS):
            relation = DialogueRelation.CORRECTION
        elif previous_route is not None and _contains_any(value, _FOLLOWUP_TERMS):
            relation = DialogueRelation.FOLLOWUP
        elif _contains_any(value, _CONFUSION_TERMS):
            relation = DialogueRelation.CONFUSED
        else:
            relation = DialogueRelation.NEW

        goals = _goals(value) if route is DialogueRoute.BOOK_DIALOGUE else []
        return _frame(
            route=route,
            relation=relation,
            goals=goals,
            context_sources=context,
            confidence=0.58,
            resolved_by="degraded_fallback",
            topic_source=(
                TopicSource.PREVIOUS_TURN
                if previous_route is not None and _contains_any(value, _DEICTIC_TERMS + _FOLLOWUP_TERMS)
                else TopicSource.CURRENT_VIEW
                if has_current_view and _contains_any(value, _DEICTIC_TERMS)
                else TopicSource.NONE
            ),
            topic_confidence=(
                0.82
                if previous_route is not None and _contains_any(value, _DEICTIC_TERMS + _FOLLOWUP_TERMS)
                else 0.72
                if has_current_view and _contains_any(value, _DEICTIC_TERMS)
                else 0.0
            ),
            cognition=_fallback_cognition(question, route),
        )


class HybridIntentRouter:
    """Use hard server signals first, then a semantic model for free text."""

    def __init__(self, semantic_model: object | None = None) -> None:
        self.semantic_model = semantic_model
        self.fallback = IntentRouter()

    @staticmethod
    def _context(
        *,
        has_selection: bool,
        has_current_view: bool,
        previous_route: DialogueRoute | None,
    ) -> list[ContextSource]:
        values: list[ContextSource] = []
        if has_selection:
            values.append(ContextSource.SELECTION)
        if has_current_view:
            values.append(ContextSource.CURRENT_VIEW)
        if previous_route is not None:
            values.append(ContextSource.PREVIOUS_TURN)
        return values or [ContextSource.NONE]

    def classify(
        self,
        question: str,
        *,
        has_selection: bool = False,
        has_current_view: bool = False,
        previous_route: DialogueRoute | None = None,
        explicit_target: IntentTarget | None = None,
        current_chapter_id: str | None = None,
        previous_intent: IntentFrame | None = None,
        previous_question: str | None = None,
        previous_answer: str | None = None,
    ) -> IntentFrame:
        started = time.perf_counter()
        compact = _compact(question)
        if previous_intent is not None:
            previous_route = previous_intent.route
        has_selection = has_selection or explicit_target is not None
        context = self._context(
            has_selection=has_selection,
            has_current_view=has_current_view,
            previous_route=previous_route,
        )
        targets = [explicit_target] if explicit_target is not None else []

        classifier = getattr(self.semantic_model, "classify_intent", None)
        degraded_reason = "model_unavailable"
        if callable(classifier):
            try:
                routing_context: dict[str, Any] = {
                    "has_selection": has_selection,
                    "current_chapter_id": current_chapter_id,
                    "previous_intent": previous_intent.model_dump(mode="json") if previous_intent else None,
                    "previous_question": (previous_question or "")[:800] or None,
                    "previous_answer": (previous_answer or "")[:1000] or None,
                    "topic_candidates": (
                        ([{"key": "explicit_target", "kind": explicit_target.kind.value}]
                         if explicit_target is not None else [])
                        + ([{
                            "key": "previous_turn",
                            "kind": "previous_turn",
                            "route": previous_intent.route.value,
                            "scope": previous_intent.scope.value,
                            "question": (previous_question or "")[:400] or None,
                            "answer": (previous_answer or "")[:600] or None,
                        }] if previous_intent is not None else [])
                        + ([{"key": "current_view", "kind": "current_chapter", "bound": True}]
                           if has_current_view else [])
                    ),
                }
                kwargs: dict[str, Any] = {
                    "question": question,
                    "previous_route": previous_route,
                    "routing_context": routing_context,
                }
                try:
                    signature = inspect.signature(classifier)
                    accepts_kwargs = any(
                        item.kind is inspect.Parameter.VAR_KEYWORD
                        for item in signature.parameters.values()
                    )
                    if not accepts_kwargs:
                        kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
                except (TypeError, ValueError):
                    kwargs.pop("routing_context", None)
                raw = classifier(**kwargs)
                route = DialogueRoute(raw.get("route", DialogueRoute.CLARIFICATION.value))
                relation = DialogueRelation(raw.get("relation", DialogueRelation.NEW.value))
                raw_goals = raw.get("goals") or []
                goals = [DialogueGoal(value) for value in raw_goals][:4]
                confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
                scope = IntentScope(raw.get("scope", _scope_for_route(route).value))
                if has_selection:
                    scope = (
                        IntentScope.MIXED
                        if scope in {IntentScope.EXTERNAL, IntentScope.MIXED}
                        else IntentScope.PASSAGE
                    )
                    route = DialogueRoute.BOOK_DIALOGUE
                raw_tasks = raw.get("tasks") or []
                tasks = [IntentTask(value) for value in raw_tasks][:4]
                raw_needs = raw.get("context_needs") or []
                needs = [ContextNeed(value) for value in raw_needs][:6]
                external_access = ExternalAccess(
                    raw.get("external_access", _external_for_scope(scope).value)
                )
                try:
                    topic_source = TopicSource(raw.get("topic_source", TopicSource.NONE.value))
                except ValueError:
                    topic_source = TopicSource.NONE
                try:
                    topic_confidence = max(0.0, min(1.0, float(raw.get("topic_confidence", 0.0))))
                except (TypeError, ValueError):
                    topic_confidence = 0.0
                if explicit_target is not None:
                    topic_source = TopicSource.EXPLICIT
                    topic_confidence = 1.0
                elif topic_source is TopicSource.NONE and previous_intent is not None and relation in {
                    DialogueRelation.FOLLOWUP,
                    DialogueRelation.CORRECTION,
                    DialogueRelation.CONFUSED,
                }:
                    topic_source = TopicSource.PREVIOUS_TURN
                    topic_confidence = max(topic_confidence, 0.82)
                elif topic_source is TopicSource.NONE and has_current_view and _contains_any(compact, _DEICTIC_TERMS):
                    topic_source = TopicSource.CURRENT_VIEW
                    topic_confidence = max(topic_confidence, 0.72)
                available_topic_sources = {TopicSource.NONE, TopicSource.AMBIGUOUS}
                if explicit_target is not None:
                    available_topic_sources.add(TopicSource.EXPLICIT)
                if previous_intent is not None:
                    available_topic_sources.add(TopicSource.PREVIOUS_TURN)
                if has_current_view:
                    available_topic_sources.add(TopicSource.CURRENT_VIEW)
                if topic_source not in available_topic_sources:
                    topic_source = TopicSource.NONE
                    topic_confidence = 0.0
                clarification = bool(raw.get("clarification_required", False)) or scope is IntentScope.AMBIGUOUS
                if scope is IntentScope.AMBIGUOUS and topic_source in {
                    TopicSource.PREVIOUS_TURN,
                    TopicSource.CURRENT_VIEW,
                }:
                    if topic_source is TopicSource.PREVIOUS_TURN and previous_intent is not None:
                        scope = previous_intent.scope
                        route = previous_intent.route
                    else:
                        scope = IntentScope.CURRENT_BOOK
                        route = DialogueRoute.BOOK_DIALOGUE
                    clarification = False
                if scope is IntentScope.AMBIGUOUS and _has_bound_book_referent(
                    compact, has_current_view=has_current_view
                ):
                    scope = (
                        IntentScope.MIXED
                        if external_access is not ExternalAccess.NOT_NEEDED
                        else IntentScope.CURRENT_BOOK
                    )
                    route = DialogueRoute.BOOK_DIALOGUE
                    clarification = False
                if clarification and not has_selection:
                    scope = IntentScope.AMBIGUOUS
                    route = DialogueRoute.CLARIFICATION
                semantic_targets: list[IntentTarget] = []
                if not targets:
                    for item in (raw.get("targets") or [])[:4]:
                        if isinstance(item, dict):
                            semantic_targets.append(
                                IntentTarget(
                                    kind=IntentTargetKind(item.get("kind", "unspecified")),
                                    explicit=False,
                                )
                            )
                    if (
                        not semantic_targets
                        and previous_intent is not None
                        and (
                            relation in {
                                DialogueRelation.FOLLOWUP,
                                DialogueRelation.CORRECTION,
                                DialogueRelation.CONFUSED,
                            }
                            or topic_source is TopicSource.PREVIOUS_TURN
                        )
                    ):
                        semantic_targets.append(
                            IntentTarget(kind=IntentTargetKind.PREVIOUS_TURN, explicit=False)
                        )
                if confidence < 0.55 and not has_selection:
                    if previous_intent is not None and topic_source is TopicSource.PREVIOUS_TURN:
                        confidence = max(confidence, 0.55)
                    else:
                        scope = IntentScope.AMBIGUOUS
                        route = DialogueRoute.CLARIFICATION
                        clarification = True
                        needs = (
                            [ContextNeed.RECENT_TURNS]
                            if previous_intent is not None
                            else []
                        )
                        external_access = ExternalAccess.NOT_NEEDED
                cognition = _valid_cognitive(
                    raw.get("cognition"),
                    route=route,
                    question=question,
                    previous_question=previous_question,
                    previous_answer=previous_answer,
                )
                result = _frame(
                    route=route,
                    relation=relation,
                    goals=goals,
                    context_sources=context,
                    scope=scope,
                    tasks=tasks or None,
                    targets=targets or semantic_targets,
                    context_needs=needs or None,
                    external_access=external_access,
                    clarification_required=clarification,
                    confidence=confidence,
                    resolved_by="semantic_model",
                    topic_source=topic_source,
                    topic_confidence=topic_confidence,
                    cognition=cognition,
                )
                return result.model_copy(
                    update={
                        "routing_ms": max(0, round((time.perf_counter() - started) * 1000)),
                        "router_model": (
                            getattr(self.semantic_model, "intent_model_name", None)
                            or getattr(self.semantic_model, "model_name", None)
                        ),
                    }
                )
            except (KeyError, TypeError, ValueError):
                degraded_reason = "invalid_response"
            except Exception:
                # Routing is availability-sensitive, never a reason to lose a
                # user's question.  The degraded source remains observable.
                degraded_reason = "model_error"

        result = self.fallback.classify(
            question,
            has_highlight=has_selection,
            has_current_view=has_current_view,
            previous_route=previous_route,
        )
        fallback_scope = result.scope
        if has_selection:
            fallback_scope = IntentScope.PASSAGE
        elif previous_intent is not None and result.relation in {
            DialogueRelation.FOLLOWUP,
            DialogueRelation.CORRECTION,
            DialogueRelation.CONFUSED,
        }:
            fallback_scope = previous_intent.scope
        is_previous_reference = previous_intent is not None and (
            result.relation in {
                DialogueRelation.FOLLOWUP,
                DialogueRelation.CORRECTION,
                DialogueRelation.CONFUSED,
            }
            or _contains_any(_compact(question), _DEICTIC_TERMS)
        )
        if not targets and is_previous_reference:
            targets = [IntentTarget(kind=IntentTargetKind.PREVIOUS_TURN, explicit=False)]
        fallback = _frame(
            route=result.route,
            relation=result.relation,
            goals=result.goals,
            context_sources=context,
            scope=fallback_scope,
            targets=targets,
            confidence=result.confidence,
            resolved_by="degraded_fallback",
            topic_source=TopicSource.PREVIOUS_TURN if is_previous_reference else TopicSource.NONE,
            topic_confidence=0.82 if is_previous_reference else 0.0,
            cognition=_fallback_cognition(question, result.route),
        )
        return fallback.model_copy(
            update={
                "routing_ms": max(0, round((time.perf_counter() - started) * 1000)),
                "router_model": (
                    getattr(self.semantic_model, "intent_model_name", None)
                    or getattr(self.semantic_model, "model_name", None)
                ),
                "degraded_reason": degraded_reason,
            }
        )


def recent_history(records: Sequence[object], *, limit: int = 4) -> list[dict[str, str]]:
    """Return a tiny, bounded history shape for model adapters."""

    result: list[dict[str, str]] = []
    bounded_limit = max(0, min(limit, 4))
    if bounded_limit == 0:
        return result
    completed = [
        record
        for record in records
        if getattr(getattr(record, "ledger", None), "status", None) is AnswerRunStatus.COMPLETED
    ]
    for record in completed[-bounded_limit:]:
        question = getattr(record, "question", "")
        answer = getattr(record, "answer_text", "")
        if question and answer:
            result.append({"question": str(question)[:800], "answer": str(answer)[:1600]})
    return result
