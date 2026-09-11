from __future__ import annotations

from uuid import uuid4

from reading_agent.agent_core import ContextAssembler


def test_memory_is_separate_from_history_and_budgeted_before_history() -> None:
    package = ContextAssembler(1200).build(
        question="为什么能推出这个结论",
        skill_context="先拆开论证链",
        history=[{"question": "旧问题" * 300, "answer": "旧回答" * 500}],
        memory=[
            {
                "text": "用户曾在这个概念上表示仍然困惑",
                "kind": "concept_state",
                "memory_id": str(uuid4()),
                "conflicted": False,
            }
        ],
        evidence=[],
    )

    assert len(package.memory) == 1
    assert package.model_history()[0]["question"].startswith("[书籍学习记忆")
    assert package.estimated_tokens <= package.budget_tokens
    assert package.pruned_history == 1


def test_memory_budget_can_prune_memory_without_dropping_primary_evidence() -> None:
    package = ContextAssembler(1200).build(
        question="解释这段",
        skill_context="",
        history=[],
        memory=[{"text": "记忆" * 2000, "kind": "learning_episode"}],
        evidence=[],
    )

    assert package.memory == ()
    assert package.pruned_memory == 1
    assert package.estimated_tokens <= package.budget_tokens
