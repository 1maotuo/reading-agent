from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from reading_agent.api import CSRF_COOKIE
from reading_agent.stage05 import DEMO_IDENTIFIER, DEMO_PASSWORD, create_stage05_app


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "experiments" / "f03_01_document_parsing" / "fixtures"


class DeterministicReaderModel:
    model_name = "deterministic-reader-test"
    available = True

    def generate(self, *, question, evidence):
        refs = list(evidence)
        return f"这是基于原文的测试解释：{question} [E1]", {"input_tokens": len(refs), "output_tokens": 8}


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/sessions",
        json={"identifier": DEMO_IDENTIFIER, "password": DEMO_PASSWORD},
    )
    assert response.status_code == 200, response.text
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf
    return csrf


def _upload(client: TestClient, csrf: str, path: Path, format_name: str):
    response = client.post(
        "/api/v1/books",
        data={"title": f"测试-{format_name}", "format": format_name},
        files={"file": (path.name, path.read_bytes(), "application/octet-stream")},
        headers={"Idempotency-Key": f"upload-{format_name}-{uuid4()}", "X-CSRF-Token": csrf},
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_s05_01_to_03_login_four_real_formats_and_reading(tmp_path: Path) -> None:
    app = create_stage05_app(tmp_path)
    app.state.services.answer_handler.model = DeterministicReaderModel()
    with TestClient(app) as client:
        csrf = _login(client)
        for name, suffix in (("pdf", "pdf"), ("epub", "epub"), ("txt", "txt"), ("markdown", "md")):
            created = _upload(client, csrf, FIXTURES / f"thinking_clearly.{suffix}", name)
            job = client.get(f"/api/v1/jobs/{created['job_id']}")
            assert job.status_code == 200
            assert job.json()["status"] == "succeeded"
            book = client.get(f"/api/v1/books/{created['book_id']}")
            assert book.status_code == 200
            assert book.json()["active_version_id"]
            chapters = client.get(f"/api/v1/books/{created['book_id']}/chapters").json()["items"]
            assert chapters
            blocks = client.get(
                f"/api/v1/books/{created['book_id']}/chapters/{chapters[0]['chapter_id']}/blocks"
            ).json()["items"]
            assert blocks
            assert any("thinking" in item["text"].casefold() for item in blocks)
        listed = client.get("/api/v1/books")
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 4


def test_s05_04_to_08_progress_highlight_evidence_sse_and_history(tmp_path: Path) -> None:
    app = create_stage05_app(tmp_path)
    app.state.services.answer_handler.model = DeterministicReaderModel()
    with TestClient(app) as client:
        csrf = _login(client)
        created = _upload(client, csrf, FIXTURES / "thinking_clearly.md", "markdown")
        book_id = created["book_id"]
        chapter = client.get(f"/api/v1/books/{book_id}/chapters").json()["items"][0]
        chapter_id = chapter["chapter_id"]
        block = client.get(f"/api/v1/books/{book_id}/chapters/{chapter_id}/blocks").json()["items"][0]

        progress = client.get(f"/api/v1/books/{book_id}/progress", params={"chapter_id": chapter_id})
        assert progress.status_code == 200
        position = progress.json()
        update = {
            "chapter_id": chapter_id,
            "last_chunk_index": block["ordinal"],
            "furthest_chunk_index": block["ordinal"],
            "position": {
                "chapter_id": chapter_id,
                "block_id": block["block_id"],
                "block_offset": 1,
                "updated_at": position["updated_at"],
                "device_id": "browser-test",
                "row_version": position["row_version"],
            },
            "device_id": "browser-test",
        }
        saved = client.put(
            f"/api/v1/books/{book_id}/progress",
            json=update,
            headers={"If-Match": str(position["row_version"]), "X-CSRF-Token": csrf},
        )
        assert saved.status_code == 200, saved.text
        stale = client.put(
            f"/api/v1/books/{book_id}/progress",
            json=update,
            headers={"If-Match": str(position["row_version"]), "X-CSRF-Token": csrf},
        )
        assert stale.status_code == 409

        quote = block["text"][: min(24, len(block["text"]))]
        highlight = client.post(
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
            headers={"Idempotency-Key": f"highlight-{uuid4()}", "X-CSRF-Token": csrf},
        )
        assert highlight.status_code == 201, highlight.text

        question = client.post(
            f"/api/v1/books/{book_id}/questions",
            json={
                "question": "请用简单的话解释这段内容",
                "highlight_id": highlight.json()["highlight_id"],
                "client_request_id": str(uuid4()),
            },
            headers={"Idempotency-Key": f"question-{uuid4()}", "X-CSRF-Token": csrf},
        )
        assert question.status_code == 202, question.text
        run_id = question.json()["run_id"]
        events = client.get(f"/api/v1/answer-runs/{run_id}/events")
        assert events.status_code == 200
        event_names = [line.removeprefix("event: ") for line in events.text.splitlines() if line.startswith("event: ")]
        assert event_names[0] == "accepted"
        assert "evidence" in event_names
        assert "answer_delta" in event_names
        assert event_names[-1] == "completed"
        assert event_names.index("evidence") < event_names.index("answer_delta")

        evidence = client.get(f"/api/v1/answer-runs/{run_id}/evidence")
        assert evidence.status_code == 200
        assert evidence.json()["refs"][0]["block_ids"] == [block["block_id"]]
        history = client.get(f"/api/v1/books/{book_id}/answers")
        assert history.status_code == 200
        assert history.json()["items"][0]["answer"].endswith("[E1]")
        trace_id = question.json()["trace_id"]
        trace = client.get(f"/api/v1/traces/{trace_id}")
        assert trace.status_code == 200
        assert trace.json()["tool_call_count"] == 1
        assert trace.json()["model_name"] == "deterministic-reader-test"


def test_s05_07_cross_user_evidence_and_bad_anchor_fail_closed(tmp_path: Path) -> None:
    app = create_stage05_app(tmp_path)
    app.state.services.answer_handler.model = DeterministicReaderModel()
    with TestClient(app) as owner:
        csrf = _login(owner)
        created = _upload(owner, csrf, FIXTURES / "thinking_clearly.txt", "txt")
        book_id = created["book_id"]
        chapter = owner.get(f"/api/v1/books/{book_id}/chapters").json()["items"][0]
        block = owner.get(
            f"/api/v1/books/{book_id}/chapters/{chapter['chapter_id']}/blocks"
        ).json()["items"][0]
        bad = owner.post(
            f"/api/v1/books/{book_id}/highlights",
            json={
                "chapter_id": chapter["chapter_id"],
                "start": {"block_id": block["block_id"], "offset": 0},
                "end": {"block_id": block["block_id"], "offset": 4},
                "exact_quote": "伪造",
                "prefix": "",
                "suffix": "",
                "text_sha256": hashlib.sha256("伪造".encode("utf-8")).hexdigest(),
            },
            headers={"Idempotency-Key": f"bad-{uuid4()}", "X-CSRF-Token": csrf},
        )
        assert bad.status_code == 422

        other_id = app.state.services.auth.add_account("other", "password")
        assert other_id
        with TestClient(app) as other:
            login = other.post("/api/v1/sessions", json={"identifier": "other", "password": "password"})
            assert login.status_code == 200
            assert other.get(f"/api/v1/books/{book_id}").status_code == 404


def test_s05_07_reading_scope_blocks_unadvanced_chunk_until_progress_moves(tmp_path: Path) -> None:
    app = create_stage05_app(tmp_path)
    app.state.services.answer_handler.model = DeterministicReaderModel()
    with TestClient(app) as client:
        csrf = _login(client)
        created = _upload(client, csrf, FIXTURES / "thinking_clearly.md", "markdown")
        book_id = created["book_id"]
        chapter = client.get(f"/api/v1/books/{book_id}/chapters").json()["items"][0]
        chapter_id = chapter["chapter_id"]
        blocks = client.get(f"/api/v1/books/{book_id}/chapters/{chapter_id}/blocks").json()["items"]
        first = blocks[0]
        first_chunk_index = app.state.services.books.block_chunk_indexes[UUID(first["block_id"])]
        later = next(
            item
            for item in blocks[1:]
            if app.state.services.books.block_chunk_indexes[UUID(item["block_id"])] > first_chunk_index
        )

        progress = client.get(f"/api/v1/books/{book_id}/progress", params={"chapter_id": chapter_id})
        assert progress.status_code == 200
        position = progress.json()
        assert position["furthest_chunk_index"] == first["ordinal"]

        question_payload = {
            "question": f"请解释这段原文：{later['text']}",
            "client_request_id": str(uuid4()),
        }
        blocked = client.post(
            f"/api/v1/books/{book_id}/questions",
            json=question_payload,
            headers={"Idempotency-Key": f"scope-blocked-{uuid4()}", "X-CSRF-Token": csrf},
        )
        assert blocked.status_code == 202, blocked.text
        blocked_evidence = client.get(f"/api/v1/answer-runs/{blocked.json()['run_id']}/evidence")
        assert blocked_evidence.status_code == 200
        assert all(later["block_id"] not in ref["block_ids"] for ref in blocked_evidence.json()["refs"])

        update = {
            "chapter_id": chapter_id,
            "last_chunk_index": later["ordinal"],
            "furthest_chunk_index": later["ordinal"],
            "position": {
                "chapter_id": chapter_id,
                "block_id": later["block_id"],
                "block_offset": 0,
                "updated_at": position["updated_at"],
                "device_id": "scope-test",
                "row_version": position["row_version"],
            },
            "device_id": "scope-test",
        }
        advanced = client.put(
            f"/api/v1/books/{book_id}/progress",
            json=update,
            headers={"If-Match": str(position["row_version"]), "X-CSRF-Token": csrf},
        )
        assert advanced.status_code == 200, advanced.text

        allowed = client.post(
            f"/api/v1/books/{book_id}/questions",
            json={**question_payload, "client_request_id": str(uuid4())},
            headers={"Idempotency-Key": f"scope-allowed-{uuid4()}", "X-CSRF-Token": csrf},
        )
        assert allowed.status_code == 202, allowed.text
        allowed_evidence = client.get(f"/api/v1/answer-runs/{allowed.json()['run_id']}/evidence")
        assert allowed_evidence.status_code == 200
        assert any(later["block_id"] in ref["block_ids"] for ref in allowed_evidence.json()["refs"])
