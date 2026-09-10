"""Deterministic conversation routing for the bounded Stage 05 preview.

The router deliberately does not call a model.  It turns a user message and
server-validated context into one small intent frame; the answer handler then
chooses the evidence or local-response policy from that frame.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .contracts import (
    AnswerRunStatus,
    ContextSource,
    DialogueGoal,
    DialogueRelation,
    DialogueRoute,
    IntentFrame,
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
_CONFUSION_TERMS = ("什么意思", "没懂", "不懂", "不明白", "看不懂", "怎么理解", "为什么")
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
_FOLLOWUP_TERMS = ("为什么", "然后呢", "再说说", "举个例子", "具体一点", "什么意思", "怎么理解", "那")
_LIGHT_OPEN_FOLLOWUPS = {"为什么", "然后呢", "再说说", "还有呢", "继续", "真的吗", "怎么了"}


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


class IntentRouter:
    """Classify a message with bounded, auditable rules and no model call."""

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
        elif _contains_any(value, _CONFUSION_TERMS) and not (
            has_highlight or has_current_view or previous_route is not None
        ) and not has_book_semantics:
            route = DialogueRoute.CLARIFICATION
        else:
            # Uncertain messages conservatively enter the evidence-gated path.
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
        return IntentFrame(route=route, relation=relation, goals=goals, context_sources=context)


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
