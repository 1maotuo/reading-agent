from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from reading_agent.api import CSRF_COOKIE
from reading_agent.contracts import (
    ContextSource,
    DialogueRelation,
    DialogueRoute,
    ErrorCode,
    QuestionCreate,
)
from reading_agent.dialogue import IntentRouter, recent_history
from reading_agent.domain import AnswerEventLedger, ContractViolation
from reading_agent.stage05 import DEMO_IDENTIFIER, DEMO_PASSWORD, create_stage05_app


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "experiments" / "f03_01_document_parsing" / "fixtures" / "thinking_clearly.md"


class ConversationModel:
    model_name = "conversation-test"

    def __init__(self) -> None:
        self.calls = 0
        self.histories: list[list[dict[str, str]]] = []

    def generate(self, *, question, evidence, history=(), intent=None):
        self.calls += 1
        self.histories.append(list(history))
        if evidence:
            return f"书内回答：{question} [E1]", {"input_tokens": 4, "output_tokens": 5}
        return f"陪伴回答：{question}", {"input_tokens": 2, "output_tokens": 3}


def _login(client: TestClient) -> str:
    response = client.post("/api/v1/sessions", json={"identifier": DEMO_IDENTIFIER, "password": DEMO_PASSWORD})
    assert response.status_code == 200, response.text
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf
    return csrf


def _book(client: TestClient, csrf: str) -> tuple[str, str]:
    response = client.post(
        "/api/v1/books",
        data={"title": "conversation-first", "format": "markdown"},
        files={"file": (FIXTURE.name, FIXTURE.read_bytes(), "text/markdown")},
        headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
    )
    assert response.status_code == 202, response.text
    book_id = response.json()["book_id"]
    chapter_id = client.get(f"/api/v1/books/{book_id}/chapters").json()["items"][0]["chapter_id"]
    return book_id, chapter_id


def _highlight(client: TestClient, csrf: str, book_id: str, chapter_id: str) -> str:
    block = client.get(f"/api/v1/books/{book_id}/chapters/{chapter_id}/blocks").json()["items"][0]
    quote = block["text"][: min(20, len(block["text"]))]
    response = client.post(
        f"/api/v1/books/{book_id}/highlights",
        json={
            "chapter_id": chapter_id,
            "start": {"block_id": block["block_id"], "offset": 0},
            "end": {"block_id": block["block_id"], "offset": len(quote)},
            "exact_quote": quote,
            "prefix": "",
            "suffix": block["text"][len(quote) : len(quote) + 32],
            "text_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        },
        headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text
    return response.json()["highlight_id"]


def test_intent_router_has_four_routes_without_model_call() -> None:
    router = IntentRouter()
    assert router.classify("你好").route is DialogueRoute.OPEN_DIALOGUE
    for message in ("这个阅读器怎么换章节", "这个阅读器怎么划线", "目录在哪"):
        assert router.classify(message).route is DialogueRoute.READER_ACTION
    for message in ("这本书里的阅读器是什么意思？", "作者说的方法怎么用？"):
        assert router.classify(message).route is DialogueRoute.BOOK_DIALOGUE
    open_followup = router.classify("为什么？", previous_route=DialogueRoute.OPEN_DIALOGUE, has_current_view=True)
    assert open_followup.route is DialogueRoute.OPEN_DIALOGUE
    assert open_followup.relation is DialogueRelation.FOLLOWUP
    assert router.classify("为什么？").route is DialogueRoute.CLARIFICATION
    book = router.classify("作者这一章的核心论点是什么？", has_current_view=True)
    assert book.route is DialogueRoute.BOOK_DIALOGUE
    assert ContextSource.CURRENT_VIEW in book.context_sources


@pytest.mark.parametrize("forbidden", ["route", "evidence_required"])
def test_question_create_rejects_client_owned_routing_fields(forbidden: str) -> None:
    payload = {
        "question": "你好",
        "client_request_id": str(uuid4()),
        forbidden: "open_dialogue" if forbidden == "route" else False,
    }
    with pytest.raises(ValidationError):
        QuestionCreate.model_validate_json(json.dumps(payload))


def test_recent_history_is_completed_only_bounded_and_truncated() -> None:
    def complete(question: str, answer: str):
        ledger = AnswerEventLedger(run_id=uuid4(), trace_id=uuid4(), evidence_required=False)
        ledger.accepted()
        ledger.answer_delta("ok")
        ledger.complete(answer_id=uuid4(), conversation_id=uuid4(), evidence_ids=[])
        return SimpleNamespace(question=question, answer_text=answer, ledger=ledger)

    failed_ledger = AnswerEventLedger(run_id=uuid4(), trace_id=uuid4(), evidence_required=False)
    failed_ledger.accepted()
    failed_ledger.fail(ErrorCode.INTERNAL_ERROR, "failed")
    failed = SimpleNamespace(question="不应进入历史", answer_text="不应进入模型", ledger=failed_ledger)
    records = [complete(f"q{index}" + "问" * 900, f"a{index}" + "答" * 1700) for index in range(6)]
    records.append(failed)
    history = recent_history(records, limit=99)
    assert len(history) == 4
    assert history[0]["question"].startswith("q2")
    assert all(len(item["question"]) == 800 and len(item["answer"]) == 1600 for item in history)
    assert all("不应进入" not in item["question"] for item in history)


def test_non_book_ledger_can_complete_without_evidence_but_default_cannot() -> None:
    local = AnswerEventLedger(run_id=uuid4(), trace_id=uuid4(), evidence_required=False)
    local.accepted()
    local.answer_delta("你好")
    local.complete(answer_id=uuid4(), conversation_id=uuid4(), evidence_ids=[])
    assert local.status.value == "completed"

    strict = AnswerEventLedger(run_id=uuid4(), trace_id=uuid4())
    strict.accepted()
    try:
        strict.answer_delta("不应提前输出")
    except ContractViolation as exc:
        assert exc.code is ErrorCode.EVIDENCE_REQUIRED
    else:  # pragma: no cover - protects the invariant if the ledger regresses
        raise AssertionError("default ledger allowed answer without evidence")


def test_conversation_first_direct_question_routes_and_reuses_scope(tmp_path: Path) -> None:
    app = create_stage05_app(tmp_path)
    model = ConversationModel()
    app.state.services.answer_handler.model = model
    with TestClient(app) as client:
        csrf = _login(client)
        book_id, chapter_id = _book(client, csrf)

        open_response = client.post(
            f"/api/v1/books/{book_id}/questions",
            json={"question": "你好，陪我聊聊天", "current_chapter_id": chapter_id, "client_request_id": str(uuid4())},
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
        )
        assert open_response.status_code == 202, open_response.text
        open_run = open_response.json()
        assert open_run["intent"]["route"] == "open_dialogue"
        open_events = client.get(f"/api/v1/answer-runs/{open_run['run_id']}/events").text
        assert "event: evidence" not in open_events
        assert client.get(f"/api/v1/traces/{open_run['trace_id']}").json()["tool_call_count"] == 0

        open_record = app.state.services.answers.records[UUID(open_run["run_id"])]
        failed = app.state.services.answers.create(
            open_record.scope,
            "失败轮不应决定路由",
            conversation_id=UUID(open_run["conversation_id"]),
            intent=IntentRouter().classify("作者说了什么？"),
        )
        failed.answer_text = "不应进入模型历史"
        failed.ledger.fail(ErrorCode.INTERNAL_ERROR, "failed")
        open_followup = client.post(
            f"/api/v1/books/{book_id}/questions",
            json={
                "question": "为什么？",
                "current_chapter_id": chapter_id,
                "conversation_id": open_run["conversation_id"],
                "client_request_id": str(uuid4()),
            },
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
        )
        assert open_followup.status_code == 202, open_followup.text
        followup_intent = open_followup.json()["intent"]
        assert followup_intent["route"] == "open_dialogue"
        assert followup_intent["scope"] == "open"
        assert followup_intent["relation"] == "followup"
        assert followup_intent["tasks"] == ["discuss"]
        assert followup_intent["targets"] == [
            {"kind": "previous_turn", "identifier": None, "explicit": False}
        ]
        assert followup_intent["context_sources"] == ["current_view", "previous_turn"]
        assert followup_intent["requires_evidence"] is False
        assert followup_intent["resolved_by"] == "degraded_fallback"
        assert len(model.histories[-1]) == 1
        assert all("不应进入" not in item["question"] and "不应进入" not in item["answer"] for item in model.histories[-1])

        failed_only_id = uuid4()
        failed_only = app.state.services.answers.create(
            open_record.scope,
            "只有失败状态",
            conversation_id=failed_only_id,
            intent=IntentRouter().classify("这本书讲了什么？"),
        )
        failed_only.ledger.fail(ErrorCode.INTERNAL_ERROR, "failed")
        rejected_failed_conversation = client.post(
            f"/api/v1/books/{book_id}/questions",
            json={"question": "继续", "conversation_id": str(failed_only_id), "client_request_id": str(uuid4())},
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
        )
        assert rejected_failed_conversation.status_code == 404

        book_response = client.post(
            f"/api/v1/books/{book_id}/questions",
            json={
                "question": "这一章的核心论点是什么？",
                "current_chapter_id": chapter_id,
                "client_request_id": str(uuid4()),
            },
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
        )
        assert book_response.status_code == 202, book_response.text
        book_run = book_response.json()
        assert book_run["intent"]["route"] == "book_dialogue"
        events = client.get(f"/api/v1/answer-runs/{book_run['run_id']}/events").text
        assert "event: evidence" in events and "event: answer_delta" in events
        assert events.index("event: evidence") < events.index("event: answer_delta")

        followup = client.post(
            f"/api/v1/books/{book_id}/questions",
            json={
                "question": "为什么？",
                "current_chapter_id": chapter_id,
                "conversation_id": book_run["conversation_id"],
                "client_request_id": str(uuid4()),
            },
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
        )
        assert followup.status_code == 202, followup.text
        assert followup.json()["conversation_id"] == book_run["conversation_id"]
        assert followup.json()["intent"]["relation"] == "followup"
        assert model.calls == 4  # two open turns and two evidence-gated book turns

        clarify = client.post(
            f"/api/v1/books/{book_id}/questions",
            json={"question": "为什么？", "client_request_id": str(uuid4())},
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
        )
        assert clarify.status_code == 202, clarify.text
        assert clarify.json()["intent"]["route"] == "clarification"

        missing_conversation = client.post(
            f"/api/v1/books/{book_id}/questions",
            json={"question": "继续", "conversation_id": str(uuid4()), "client_request_id": str(uuid4())},
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
        )
        assert missing_conversation.status_code == 404

        other_book_id, other_chapter_id = _book(client, csrf)
        cross_book_conversation = client.post(
            f"/api/v1/books/{other_book_id}/questions",
            json={"question": "继续", "conversation_id": book_run["conversation_id"], "client_request_id": str(uuid4())},
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
        )
        assert cross_book_conversation.status_code == 404

        highlight_id = _highlight(client, csrf, book_id, chapter_id)
        app.state.services.answer_handler = None
        cross_book_highlight = client.post(
            f"/api/v1/books/{other_book_id}/questions",
            json={"question": "解释它", "highlight_id": highlight_id, "client_request_id": str(uuid4())},
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
        )
        assert cross_book_highlight.status_code == 404
        missing_highlight = client.post(
            f"/api/v1/books/{book_id}/questions",
            json={"question": "解释它", "highlight_id": str(uuid4()), "client_request_id": str(uuid4())},
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
        )
        assert missing_highlight.status_code == 404

        app.state.services.auth.add_account("conversation-other", "other-password")
        with TestClient(app) as other:
            other_csrf_response = other.post(
                "/api/v1/sessions",
                json={"identifier": "conversation-other", "password": "other-password"},
            )
            assert other_csrf_response.status_code == 200
            other_csrf = other.cookies.get(CSRF_COOKIE)
            assert other_csrf
            other_user_book_id, _ = _book(other, other_csrf)
            cross_user = other.post(
                f"/api/v1/books/{other_user_book_id}/questions",
                json={"question": "继续", "conversation_id": open_run["conversation_id"], "client_request_id": str(uuid4())},
                headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": other_csrf},
            )
            assert cross_user.status_code == 404

        invalid_chapter = client.post(
            f"/api/v1/books/{book_id}/questions",
            json={"question": "这本书讲什么？", "current_chapter_id": str(uuid4()), "client_request_id": str(uuid4())},
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
        )
        assert invalid_chapter.status_code == 404

        old_book = app.state.services.books.books[UUID(book_id)]
        old_version = app.state.services.books.versions[old_book.active_version_id]
        replacement_version_id = uuid4()
        app.state.services.books.versions[replacement_version_id] = old_version.model_copy(
            update={"book_version_id": replacement_version_id}
        )
        app.state.services.books.books[UUID(book_id)] = old_book.model_copy(
            update={"active_version_id": replacement_version_id}
        )
        cross_version = client.post(
            f"/api/v1/books/{book_id}/questions",
            json={"question": "继续", "conversation_id": open_run["conversation_id"], "client_request_id": str(uuid4())},
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
        )
        assert cross_version.status_code == 404

        history = client.get(f"/api/v1/books/{book_id}/answers").json()["items"]
        assert any(item["evidence"] == [] and item["intent"]["route"] == "open_dialogue" for item in history)


def test_open_dialogue_without_evidence_survives_restart(tmp_path: Path) -> None:
    first = create_stage05_app(tmp_path)
    first.state.services.answer_handler.model = ConversationModel()
    with TestClient(first) as client:
        csrf = _login(client)
        book_id, chapter_id = _book(client, csrf)
        response = client.post(
            f"/api/v1/books/{book_id}/questions",
            json={"question": "你好", "current_chapter_id": chapter_id, "client_request_id": str(uuid4())},
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
        )
        assert response.status_code == 202, response.text
        assert response.json()["intent"]["route"] == "open_dialogue"
        events = client.get(f"/api/v1/answer-runs/{response.json()['run_id']}/events").text
        assert "event: evidence" not in events and "event: completed" in events

    restarted = create_stage05_app(tmp_path)
    with TestClient(restarted) as client:
        _login(client)
        history = client.get(f"/api/v1/books/{book_id}/answers").json()["items"]
        assert len(history) == 1
        assert history[0]["status"] == "completed"
        assert history[0]["evidence"] == []
        assert history[0]["intent"]["route"] == "open_dialogue"
