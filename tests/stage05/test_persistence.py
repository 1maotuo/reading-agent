from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from reading_agent.api import ApiServices, CSRF_COOKIE, DEFAULT_USER_ID
from reading_agent.contracts import Book, BookFormat
from reading_agent.domain import utc_now
from reading_agent.persistence import SQLitePreviewStateStore, StateStoreError
from reading_agent.stage05 import DEMO_IDENTIFIER, DEMO_PASSWORD, create_stage05_app


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "experiments" / "f03_01_document_parsing" / "fixtures" / "thinking_clearly.md"


class DeterministicReaderModel:
    model_name = "deterministic-persistence-test"
    available = True

    def generate(self, *, question, evidence):
        return f"重启后仍可恢复的解释：{question} [E1]", {"input_tokens": 3, "output_tokens": 8}


def _login(client: TestClient, identifier: str = DEMO_IDENTIFIER, password: str = DEMO_PASSWORD) -> str:
    response = client.post("/api/v1/sessions", json={"identifier": identifier, "password": password})
    assert response.status_code == 200, response.text
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf
    return csrf


def test_preview_state_survives_restart_and_keeps_owner_scope(tmp_path: Path) -> None:
    first = create_stage05_app(tmp_path)
    first.state.services.answer_handler.model = DeterministicReaderModel()
    with TestClient(first) as client:
        csrf = _login(client)
        created = client.post(
            "/api/v1/books",
            data={"title": "持久化测试书", "format": "markdown"},
            files={"file": (FIXTURE.name, FIXTURE.read_bytes(), "text/markdown")},
            headers={"Idempotency-Key": f"upload-{uuid4()}", "X-CSRF-Token": csrf},
        )
        assert created.status_code == 202, created.text
        book_id = created.json()["book_id"]
        chapter = client.get(f"/api/v1/books/{book_id}/chapters").json()["items"][0]
        chapter_id = chapter["chapter_id"]
        blocks = client.get(f"/api/v1/books/{book_id}/chapters/{chapter_id}/blocks").json()["items"]
        block = blocks[0]

        current = client.get(f"/api/v1/books/{book_id}/progress", params={"chapter_id": chapter_id}).json()
        saved = client.put(
            f"/api/v1/books/{book_id}/progress",
            json={
                "chapter_id": chapter_id,
                "last_chunk_index": block["ordinal"],
                "furthest_chunk_index": block["ordinal"],
                "position": {
                    "chapter_id": chapter_id,
                    "block_id": block["block_id"],
                    "block_offset": 2,
                    "updated_at": current["updated_at"],
                    "device_id": "first-process",
                    "row_version": current["row_version"],
                },
                "device_id": "first-process",
            },
            headers={"If-Match": str(current["row_version"]), "X-CSRF-Token": csrf},
        )
        assert saved.status_code == 200, saved.text

        quote = block["text"][: min(20, len(block["text"]))]
        highlighted = client.post(
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
        assert highlighted.status_code == 201, highlighted.text
        highlight_id = highlighted.json()["highlight_id"]

        asked = client.post(
            f"/api/v1/books/{book_id}/questions",
            json={
                "question": "请解释这段话",
                "highlight_id": highlight_id,
                "client_request_id": str(uuid4()),
            },
            headers={"Idempotency-Key": f"question-{uuid4()}", "X-CSRF-Token": csrf},
        )
        assert asked.status_code == 202, asked.text
        run_id = asked.json()["run_id"]
        assert client.get(f"/api/v1/answer-runs/{run_id}/events").text.rstrip().endswith("}")

    with sqlite3.connect(tmp_path / "var" / "dev-data" / "preview-state.sqlite3") as connection:
        payload = connection.execute(
            "SELECT payload_json FROM preview_state WHERE singleton_id=1"
        ).fetchone()[0]
    durable = json.loads(payload)
    assert "session_id" not in payload
    assert "events" not in durable["answers"][0]

    # A new application object represents a real server-process restart.
    second = create_stage05_app(tmp_path)
    with TestClient(second) as owner:
        _login(owner)
        books = owner.get("/api/v1/books").json()["items"]
        assert [item["book_id"] for item in books] == [book_id]
        restored_blocks = owner.get(
            f"/api/v1/books/{book_id}/chapters/{chapter_id}/blocks"
        ).json()["items"]
        assert restored_blocks[0]["text"] == block["text"]
        progress = owner.get(f"/api/v1/books/{book_id}/progress", params={"chapter_id": chapter_id})
        assert progress.status_code == 200
        assert progress.json()["position"]["device_id"] == "first-process"
        highlights = owner.get(f"/api/v1/books/{book_id}/highlights").json()["items"]
        assert highlights[0]["highlight_id"] == highlight_id
        history = owner.get(f"/api/v1/books/{book_id}/answers").json()["items"]
        assert history[0]["run_id"] == run_id
        assert history[0]["evidence"][0]["block_ids"] == [block["block_id"]]

        second.state.services.auth.add_account("other", "other-password")
        with TestClient(second) as other:
            _login(other, "other", "other-password")
            assert other.get(f"/api/v1/books/{book_id}").status_code == 404


def test_corrupt_preview_snapshot_fails_closed_without_overwrite(tmp_path: Path) -> None:
    app = create_stage05_app(tmp_path)
    with TestClient(app) as client:
        _login(client)
    database = tmp_path / "var" / "dev-data" / "preview-state.sqlite3"
    with sqlite3.connect(database) as connection:
        original = connection.execute(
            "SELECT payload_json, payload_sha256 FROM preview_state WHERE singleton_id=1"
        ).fetchone()
        assert original is not None
        connection.execute(
            "UPDATE preview_state SET payload_json = payload_json || 'corrupt' WHERE singleton_id=1"
        )
        connection.commit()

    with pytest.raises(StateStoreError, match="checksum mismatch"):
        create_stage05_app(tmp_path)

    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT payload_json, payload_sha256 FROM preview_state WHERE singleton_id=1"
        ).fetchone()
    assert after is not None and after[1] == original[1] and after[0].endswith("corrupt")


def test_concurrent_saves_cannot_let_an_old_snapshot_win(tmp_path: Path) -> None:
    services = ApiServices()
    store = SQLitePreviewStateStore(tmp_path / "state.sqlite3")
    first = Book(
        book_id=uuid4(),
        user_id=DEFAULT_USER_ID,
        title="先保存的书",
        format=BookFormat.MARKDOWN,
        created_at=utc_now(),
        row_version=1,
    )
    second = Book(
        book_id=uuid4(),
        user_id=DEFAULT_USER_ID,
        title="后保存的书",
        format=BookFormat.TXT,
        created_at=utc_now(),
        row_version=1,
    )
    services.books.add_book(first)

    captured = threading.Event()
    release = threading.Event()
    original_snapshot = store._snapshot

    def delayed_snapshot(current_services):
        snapshot = original_snapshot(current_services)
        if threading.current_thread().name == "old-save":
            captured.set()
            assert release.wait(3)
        return snapshot

    store._snapshot = delayed_snapshot  # type: ignore[method-assign]
    old = threading.Thread(target=store.save, args=(services,), name="old-save")
    old.start()
    assert captured.wait(3)
    services.books.add_book(second)
    new = threading.Thread(target=store.save, args=(services,), name="new-save")
    new.start()
    release.set()
    old.join(3)
    new.join(3)
    assert not old.is_alive() and not new.is_alive()

    restored = ApiServices()
    assert store.load(restored)
    assert set(restored.books.books) == {first.book_id, second.book_id}
