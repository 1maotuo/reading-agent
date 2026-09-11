"""Bounded live semantic-routing check using synthetic, non-private prompts."""

from __future__ import annotations

import argparse
import json
import sys

from reading_agent.contracts import (
    IntentScope,
    IntentTarget,
    IntentTargetKind,
    IntentTask,
)
from reading_agent.dialogue import HybridIntentRouter
from reading_agent.stage05 import QwenReaderModel


def main() -> int:
    model = QwenReaderModel()
    if not model.available:
        print(json.dumps({"status": "blocked", "reason": "DASHSCOPE_API_KEY unavailable"}))
        return 2
    router = HybridIntentRouter(model)
    cases = [
        {
            "name": "social_identity",
            "question": "你好，你叫什么名字？",
            "allowed_scopes": {IntentScope.OPEN},
            "task": IntentTask.DISCUSS,
        },
        {
            "name": "social_emotion",
            "question": "我今天有点累，不太想看书，陪我聊一会儿吧。",
            "allowed_scopes": {IntentScope.OPEN},
            "task": IntentTask.DISCUSS,
        },
        {
            "name": "book_summary",
            "question": "这本书这一章的核心观点是什么？",
            "allowed_scopes": {IntentScope.CURRENT_BOOK},
            "task": IntentTask.SUMMARIZE,
            "kwargs": {"has_current_view": True, "current_chapter_id": "chapter-test"},
        },
        {
            "name": "explicit_passage",
            "question": "怎么理解这段话？",
            "allowed_scopes": {IntentScope.PASSAGE},
            "task": IntentTask.EXPLAIN,
            "kwargs": {
                "has_current_view": True,
                "explicit_target": IntentTarget(
                    kind=IntentTargetKind.SELECTION,
                    identifier="server-selection-test",
                    explicit=True,
                ),
            },
        },
        {
            "name": "multi_task_passage",
            "question": "先总结这段，再把它记成笔记。",
            "allowed_scopes": {IntentScope.PASSAGE},
            "tasks": {IntentTask.SUMMARIZE, IntentTask.NOTE},
            "kwargs": {
                "explicit_target": IntentTarget(
                    kind=IntentTargetKind.SELECTION,
                    identifier="server-selection-test-2",
                    explicit=True,
                ),
            },
        },
        {
            "name": "external_current",
            "question": "帮我查一下这位作者到2026年最近发表了什么。",
            "allowed_scopes": {IntentScope.EXTERNAL, IntentScope.MIXED},
            "external": True,
        },
        {
            "name": "mixed_current",
            "question": "书里的人工智能预测到2026年还成立吗？请结合最新资料判断。",
            "allowed_scopes": {IntentScope.MIXED},
            "external": True,
            "kwargs": {"has_current_view": True, "current_chapter_id": "chapter-test"},
        },
        {
            "name": "ambiguous_reference",
            "question": "这个怎么理解？",
            "allowed_scopes": {IntentScope.AMBIGUOUS},
            "clarification": True,
        },
    ]
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="Run three critical cases after a focused fix")
    parser.add_argument("--case", choices=[case["name"] for case in cases])
    args = parser.parse_args()
    if args.case:
        cases = [case for case in cases if case["name"] == args.case]
    elif args.smoke:
        smoke_names = {"social_identity", "multi_task_passage", "mixed_current"}
        cases = [case for case in cases if case["name"] in smoke_names]
    results = []
    failures = []
    for case in cases:
        intent = router.classify(case["question"], **case.get("kwargs", {}))
        errors = []
        if intent.scope not in case["allowed_scopes"]:
            errors.append(f"scope={intent.scope.value}")
        if case.get("task") is not None and case["task"] not in intent.tasks:
            errors.append(f"tasks={[item.value for item in intent.tasks]}")
        expected_tasks = case.get("tasks")
        if expected_tasks is not None and not expected_tasks.issubset(set(intent.tasks)):
            errors.append(f"tasks={[item.value for item in intent.tasks]}")
        if case.get("external") and intent.external_access.value == "not_needed":
            errors.append("external_access=not_needed")
        if case.get("clarification") and not intent.clarification_required:
            errors.append("clarification_required=false")
        if intent.resolved_by != "semantic_model":
            errors.append(f"resolved_by={intent.resolved_by}")
        result = {
            "name": case["name"],
            "passed": not errors,
            "scope": intent.scope.value,
            "tasks": [item.value for item in intent.tasks],
            "relation": intent.relation.value,
            "external_access": intent.external_access.value,
            "clarification_required": intent.clarification_required,
            "confidence": intent.confidence,
            "routing_ms": intent.routing_ms,
            "degraded_reason": intent.degraded_reason,
            "errors": errors,
        }
        results.append(result)
        if errors:
            failures.append(case["name"])
    print(
        json.dumps(
            {
                "status": "passed" if not failures else "failed",
                "model": model.intent_model_name,
                "passed": len(results) - len(failures),
                "total": len(results),
                "failures": failures,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
