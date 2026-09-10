from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from reading_agent.api import CSRF_COOKIE
from reading_agent.stage05 import DEMO_IDENTIFIER, DEMO_PASSWORD, create_stage05_app


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "experiments" / "f03_01_document_parsing" / "fixtures" / "thinking_clearly.md"


class DeterministicReaderModel:
    model_name = "deterministic-postgres-minio-test"
    available = True

    def generate(self, *, question, evidence):
        return f"基于本地 PostgreSQL/MinIO 预览的解释：{question} [E1]", {"input_tokens": 3, "output_tokens": 8}


def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    dsn = "postgresql://postgres@127.0.0.1:15432/reading_agent_stage05"
    monkeypatch.setenv("READING_AGENT_STAGE05_POSTGRES_DSN", dsn)
    monkeypatch.setenv("READING_AGENT_STAGE05_MINIO_ENDPOINT", "127.0.0.1:19000")
    monkeypatch.setenv("READING_AGENT_STAGE05_MINIO_ACCESS_KEY", "reading_agent_local")
    monkeypatch.setenv("READING_AGENT_STAGE05_MINIO_SECRET_KEY", "stage05-local-minio-secret")
    monkeypatch.setenv("READING_AGENT_STAGE05_MINIO_BUCKET", "reading-agent-stage05")


def test_postgres_minio_restart_scope_and_row_version(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    try:
        _configured(monkeypatch)
        first = create_stage05_app(tmp_path)
    except Exception as exc:
        pytest.skip(f"local PostgreSQL/MinIO preview unavailable: {exc}")
    first.state.services.answer_handler.model = DeterministicReaderModel()
    with TestClient(first) as client:
        login = client.post("/api/v1/sessions", json={"identifier": DEMO_IDENTIFIER, "password": DEMO_PASSWORD})
        assert login.status_code == 200, login.text
        csrf = client.cookies.get(CSRF_COOKIE)
        assert csrf
        created = client.post(
            "/api/v1/books",
            data={"title": f"Postgres MinIO 集成 {uuid4()}", "format": "markdown"},
            files={"file": (FIXTURE.name, FIXTURE.read_bytes(), "text/markdown")},
            headers={"Idempotency-Key": f"integration-{uuid4()}", "X-CSRF-Token": csrf},
        )
        assert created.status_code == 202, created.text
        book_id = created.json()["book_id"]
        assert first.state.services.object_store is not None
        object_uri = first.state.services.books.source_paths[UUID(book_id)]
        object_key = object_uri.split("/", 3)[-1]
        assert first.state.services.object_store.exists(object_key)

        chapter = client.get(f"/api/v1/books/{book_id}/chapters").json()["items"][0]
        chapter_id = chapter["chapter_id"]
        block = client.get(f"/api/v1/books/{book_id}/chapters/{chapter_id}/blocks").json()["items"][0]
        progress = client.get(f"/api/v1/books/{book_id}/progress", params={"chapter_id": chapter_id}).json()
        payload = {
            "chapter_id": chapter_id,
            "last_chunk_index": block["ordinal"],
            "furthest_chunk_index": block["ordinal"],
            "position": {
                "chapter_id": chapter_id,
                "block_id": block["block_id"],
                "block_offset": 1,
                "updated_at": progress["updated_at"],
                "device_id": "integration-device-a",
                "row_version": progress["row_version"],
            },
            "device_id": "integration-device-a",
        }
        updated = client.put(
            f"/api/v1/books/{book_id}/progress",
            json=payload,
            headers={"If-Match": str(progress["row_version"]), "X-CSRF-Token": csrf},
        )
        assert updated.status_code == 200, updated.text
        stale = client.put(
            f"/api/v1/books/{book_id}/progress",
            json=payload,
            headers={"If-Match": str(progress["row_version"]), "X-CSRF-Token": csrf},
        )
        assert stale.status_code == 409, stale.text

    restarted = create_stage05_app(tmp_path)
    with TestClient(restarted) as client:
        login = client.post("/api/v1/sessions", json={"identifier": DEMO_IDENTIFIER, "password": DEMO_PASSWORD})
        assert login.status_code == 200, login.text
        assert any(item["book_id"] == book_id for item in client.get("/api/v1/books").json()["items"])
        status = client.get("/api/v1/dev/status")
        assert status.json()["database"] == "postgresql-preview"
        assert status.json()["object_storage"] == "minio-private"
        assert status.json()["conflict_policy"] == "row_version"
