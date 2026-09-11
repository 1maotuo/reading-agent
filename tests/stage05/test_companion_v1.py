from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from reading_agent.api import CSRF_COOKIE
from reading_agent.companion import BookType, CompanionProfiles
from reading_agent.stage05 import DEMO_IDENTIFIER, DEMO_PASSWORD, create_stage05_app


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "experiments" / "f03_01_document_parsing" / "fixtures" / "thinking_clearly.md"


def _login(client: TestClient) -> str:
    response = client.post("/api/v1/sessions", json={"identifier": DEMO_IDENTIFIER, "password": DEMO_PASSWORD})
    assert response.status_code == 200
    return client.cookies[CSRF_COOKIE]


def test_deterministic_book_classification_and_safe_context() -> None:
    profiles = CompanionProfiles()
    kind, confidence = profiles.classify("宇宙物理入门", "科学实验解释宇宙与物理规律")
    assert kind is BookType.SCIENCE
    assert confidence >= 0.55
    context = profiles.system_context(user_id=uuid4(), book_id=uuid4())
    assert "不能改变证据、安全、范围或工具规则" in context


def test_companion_settings_round_trip_and_restart(tmp_path: Path, monkeypatch) -> None:
    # Unit tests must never consume a developer's real provider credential or
    # make book-type assertions depend on a live model response.
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    app = create_stage05_app(tmp_path)
    with TestClient(app) as client:
        csrf = _login(client)
        created = client.post(
            "/api/v1/books",
            data={"title": "科学思考方法", "format": "markdown"},
            files={"file": (FIXTURE.name, FIXTURE.read_bytes(), "text/markdown")},
            headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf},
        )
        assert created.status_code == 202, created.text
        book_id = created.json()["book_id"]
        initial = client.get(f"/api/v1/books/{book_id}/companion")
        assert initial.status_code == 200
        assert initial.json()["detected_book_type"] in {"science", "practical"}
        assert initial.json()["user_skill"]["long_term_memory_enabled"] is True
        profile = client.get(f"/api/v1/books/{book_id}/book-memory")
        assert profile.status_code == 200
        assert profile.json()["unconfirmed_episode_count"] == 0
        cleared = client.delete(
            f"/api/v1/books/{book_id}/book-memory",
            headers={"X-CSRF-Token": csrf},
        )
        assert cleared.status_code == 200
        assert cleared.json() == {"deleted": 0}

        updated = client.put(
            f"/api/v1/books/{book_id}/companion",
            json={
                "role": "friend",
                "tone": "gentle",
                "depth": "deep",
                "custom_instructions": "先讲结论，再举例",
                "book_type": "philosophy",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["effective_book_type"] == "philosophy"
        assert updated.json()["user_skill"]["role"] == "friend"

    restarted = create_stage05_app(tmp_path)
    with TestClient(restarted) as client:
        _login(client)
        restored = client.get(f"/api/v1/books/{book_id}/companion")
        assert restored.status_code == 200
        assert restored.json()["effective_book_type"] == "philosophy"
        assert restored.json()["user_skill"]["custom_instructions"] == "先讲结论，再举例"
