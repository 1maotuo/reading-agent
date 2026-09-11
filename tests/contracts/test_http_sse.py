from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from reading_agent.api import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    ApiServices,
    _AnswerRecord,
    create_app,
    export_openapi,
)
from reading_agent.contracts import (
    Block,
    Book,
    BookFormat,
    BookVersion,
    BookVersionStatus,
    Chapter,
    Chunk,
    ErrorCode,
    EvidenceBundle,
    EvidenceRef,
    JobRecord,
    JobStage,
    JobStatus,
    JobType,
    ReadingPosition,
    ReadingProgress,
    ScopeContext,
    SourceLocator,
    ToolName,
)
from reading_agent.domain import AnswerEventLedger, ContractViolation, issue_verified_evidence_token, sha256_text


UTC = timezone.utc


@pytest.fixture()
def fixture_app() -> tuple[TestClient, ApiServices, UUID, UUID, UUID, UUID]:
    services = ApiServices()
    user_id = services.auth.add_account("alice", "password", uuid4())
    other_user = services.auth.add_account("bob", "password", uuid4())
    now = datetime.now(UTC)
    book_id = uuid4()
    version_id = uuid4()
    chapter_id = uuid4()
    block_id = uuid4()
    book = Book(
        book_id=book_id,
        user_id=user_id,
        title="Contract Book",
        format=BookFormat.TXT,
        active_version_id=version_id,
        created_at=now,
        row_version=1,
    )
    version = BookVersion(
        book_version_id=version_id,
        book_id=book_id,
        user_id=user_id,
        file_sha256=sha256_text("book"),
        pipeline_version="p1",
        status=BookVersionStatus.READY,
        created_at=now,
        published_at=now,
    )
    chapter = Chapter(
        chapter_id=chapter_id,
        book_id=book_id,
        user_id=user_id,
        book_version_id=version_id,
        ordinal=0,
        title="One",
        source_locator={"kind": "synthetic", "value": "chapter-1"},
    )
    block = Block(
        block_id=block_id,
        chapter_id=chapter_id,
        book_id=book_id,
        user_id=user_id,
        book_version_id=version_id,
        ordinal=0,
        text="Hello contract world",
        text_sha256=sha256_text("Hello contract world"),
        source_locator={"kind": "synthetic", "value": "block-1"},
    )
    services.books.add_book(book)
    services.books.add_version(version)
    services.books.add_chapter(chapter)
    services.books.add_chapter(
        Chapter(
            chapter_id=uuid4(),
            book_id=book_id,
            user_id=user_id,
            book_version_id=version_id,
            ordinal=1,
            title="Two",
            source_locator={"kind": "synthetic", "value": "chapter-2"},
        )
    )
    services.books.add_block(block)
    token, session, csrf = services.auth.login("alice", "password")
    # Seed a progress row for C09.  The HTTP client receives non-Secure test
    # cookie jars; the application itself still sets Secure cookies.
    services.books.progress[(user_id, version_id, chapter_id)] = ReadingProgress(
        book_id=book_id,
        user_id=user_id,
        book_version_id=version_id,
        chapter_id=chapter_id,
        last_chunk_index=2,
        furthest_chunk_index=4,
        position=ReadingPosition(
            chapter_id=chapter_id,
            block_id=block_id,
            block_offset=3,
            updated_at=now,
            device_id="seed",
            row_version=1,
        ),
        updated_at=now,
        row_version=1,
    )
    # Keep a cross-user book to prove owner checks are 404/non-enumerating.
    other_book = Book(
        book_id=uuid4(),
        user_id=other_user,
        title="Other",
        format=BookFormat.TXT,
        created_at=now,
        row_version=1,
    )
    services.books.add_book(other_book)
    app = create_app(services)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    client.headers.update({"X-CSRF-Token": csrf})
    return client, services, user_id, book_id, version_id, chapter_id


def _multipart(client: TestClient, key: str, *, content: bytes = b"file", title: str = "Upload", format: str = "txt"):
    return client.post(
        "/api/v1/books",
        data={"title": title, "format": format},
        files={"file": ("book.txt", content, "text/plain")},
        headers={"Idempotency-Key": key},
    )


def test_c02_http_scope_is_server_derived_and_client_identity_is_untrusted(fixture_app) -> None:
    client, services, user_id, book_id, version_id, chapter_id = fixture_app
    forged = {"X-Experiment-User": str(uuid4()), "X-Experiment-Book": str(uuid4()), "X-Experiment-Version": str(uuid4())}
    own = client.get(f"/api/v1/books/{book_id}", headers=forged)
    assert own.status_code == 200
    assert own.json()["book_id"] == str(book_id)
    forged_upload = {
        "title": "Upload",
        "format": "txt",
        "file_sha256": sha256_text("file"),
        "byte_size": 4,
        "user_id": str(uuid4()),
        "book_id": str(uuid4()),
        "book_version_id": str(uuid4()),
    }
    assert client.post("/api/v1/books", json=forged_upload, headers={"Idempotency-Key": "forged"}).status_code == 422
    forged_question = {
        "question": "What?",
        "client_request_id": str(uuid4()),
        "user_id": str(uuid4()),
        "book_id": str(uuid4()),
        "book_version_id": str(version_id),
        "chapter_id": str(chapter_id),
    }
    assert client.post(
        f"/api/v1/books/{book_id}/questions",
        json=forged_question,
        headers={"Idempotency-Key": "forged-question"},
    ).status_code == 422
    other_id = next(book_id for book_id, book in services.books.books.items() if book.user_id != user_id)
    hidden = client.get(f"/api/v1/books/{other_id}")
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "not_found"


def test_c04_sessions_expiry_logout_csrf_and_non_enumerating_login(fixture_app) -> None:
    client, services, *_ = fixture_app
    unknown = client.post("/api/v1/sessions", json={"identifier": "nobody", "password": "bad"})
    wrong_password = client.post("/api/v1/sessions", json={"identifier": "alice", "password": "bad"})
    assert unknown.status_code == wrong_password.status_code == 401
    assert unknown.json()["error"]["code"] == wrong_password.json()["error"]["code"] == "unauthenticated"
    login = client.post("/api/v1/sessions", json={"identifier": "alice", "password": "password"})
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "Secure" in login.headers["set-cookie"]
    token, session, csrf = services.auth.login("alice", "password")
    client.cookies.set(SESSION_COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    assert client.get("/api/v1/session").status_code == 200
    services.auth.sessions[token].view = session.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    assert client.get("/api/v1/session").status_code == 401
    token, session, csrf = services.auth.login("alice", "password")
    client.cookies.set(SESSION_COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    assert client.delete("/api/v1/session", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.delete("/api/v1/session", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.get("/api/v1/session").status_code == 401


def test_c04_unicode_password_is_authenticated_and_wrong_password_is_normal_401(fixture_app) -> None:
    client, services, *_ = fixture_app
    services.auth.add_account("unicode", "阅读密码", uuid4())

    login = client.post("/api/v1/sessions", json={"identifier": "unicode", "password": "阅读密码"})
    assert login.status_code == 200, login.text

    wrong_password = client.post("/api/v1/sessions", json={"identifier": "unicode", "password": "错误密码"})
    assert wrong_password.status_code == 401, wrong_password.text
    assert wrong_password.json()["error"]["code"] == "unauthenticated"


def test_c05_upload_idempotency_format_and_size_contract(fixture_app) -> None:
    client, services, *_ = fixture_app
    multipart = _multipart(client, "multipart", content=b"file", title="Multipart")
    assert multipart.status_code == 202
    first = _multipart(client, "same", content=b"file", title="Upload")
    second = _multipart(client, "same", content=b"file", title="Upload")
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    first_job = services.jobs.get(UUID(first.json()["job_id"]))
    assert first_job.input_sha256 == sha256_text("file")
    conflict = _multipart(client, "same", content=b"other", title="Upload")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    forged_json = client.post(
        "/api/v1/books",
        json={"title": "Fake", "format": "txt", "file_sha256": sha256_text("file"), "byte_size": 4},
        headers={"Idempotency-Key": "json"},
    )
    assert forged_json.status_code == 422
    too_big = client.post(
        "/api/v1/books",
        data={"title": "Big", "format": "txt"},
        files={"file": ("big.txt", b"x" * 50_000_001, "text/plain")},
        headers={"Idempotency-Key": "big"},
    )
    assert too_big.status_code == 413
    unsupported = client.post(
        "/api/v1/books",
        data={"title": "Bad", "format": "exe"},
        files={"file": ("bad.exe", b"file", "application/octet-stream")},
        headers={"Idempotency-Key": "format"},
    )
    assert unsupported.status_code == 415


def test_c08_ready_read_and_owner_limited_chapter_blocks(fixture_app) -> None:
    client, _, _, book_id, _, chapter_id = fixture_app
    chapters = client.get(f"/api/v1/books/{book_id}/chapters?limit=1")
    assert chapters.status_code == 200
    assert chapters.json()["items"][0]["chapter_id"] == str(chapter_id)
    next_cursor = chapters.json()["next_cursor"]
    assert next_cursor and not next_cursor.isdigit()
    assert client.get(f"/api/v1/books/{book_id}/chapters?cursor=not-a-server-cursor").status_code == 422
    assert client.get(f"/api/v1/books/{book_id}/chapters?cursor={next_cursor}&limit=1").status_code == 200
    blocks = client.get(f"/api/v1/books/{book_id}/chapters/{chapter_id}/blocks")
    assert blocks.status_code == 200
    assert blocks.json()["items"][0]["text"] == "Hello contract world"
    assert client.get(f"/api/v1/books/{book_id}/chapters/{uuid4()}/blocks").status_code == 404


def test_c09_http_progress_if_match_and_furthest_monotonic(fixture_app) -> None:
    client, _, _, book_id, _, chapter_id = fixture_app
    payload = {
        "chapter_id": str(chapter_id),
        "last_chunk_index": 0,
        "furthest_chunk_index": 1,
        "position": {
            "chapter_id": str(chapter_id),
            "block_id": str(next(iter(client.app.state.services.books.blocks))),
            "block_offset": 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "device_id": "http",
            "row_version": 1,
        },
        "device_id": "http",
    }
    updated = client.put(f"/api/v1/books/{book_id}/progress", json=payload, headers={"If-Match": "1"})
    assert updated.status_code == 200
    assert updated.json()["last_chunk_index"] == 0
    assert updated.json()["furthest_chunk_index"] == 4
    conflict = client.put(f"/api/v1/books/{book_id}/progress", json=payload, headers={"If-Match": "1"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "version_conflict"


def test_c16_delete_requires_if_match_and_records_tombstone_revoke_cancel_purge(fixture_app) -> None:
    client, services, _, book_id, _, _ = fixture_app
    missing = client.delete(f"/api/v1/books/{book_id}")
    assert missing.status_code == 409
    wrong = client.delete(f"/api/v1/books/{book_id}", headers={"If-Match": "99"})
    assert wrong.status_code == 409
    deleted = client.delete(f"/api/v1/books/{book_id}", headers={"If-Match": "1"})
    assert deleted.status_code == 202
    job_id = UUID(deleted.json()["job_id"])
    assert services.books.deletion_steps[book_id] == ["tombstone", "revoke", "cancel", "purge"]
    assert services.books.tombstones[book_id].book_id == book_id
    assert services.jobs.get(job_id).type is JobType.DELETE_BOOK
    assert client.get(f"/api/v1/books/{book_id}").status_code == 404


def test_c17_mutations_use_idempotency_nested_highlight_path_and_pagination(fixture_app) -> None:
    client, services, user_id, book_id, version_id, chapter_id = fixture_app
    now = datetime.now(UTC)
    retryable = JobRecord(
        job_id=uuid4(),
        user_id=user_id,
        book_id=book_id,
        type=JobType.IMPORT_BOOK,
        idempotency_key="retry-contract",
        input_sha256=sha256_text("retry"),
        pipeline_version="p1",
        created_at=now,
        updated_at=now,
    )
    services.jobs.add(retryable)
    services.jobs.claim(retryable.job_id, "contract-worker")
    services.jobs.fail(retryable.job_id, "contract-worker", retryable=True)
    missing_retry_key = client.post(f"/api/v1/jobs/{retryable.job_id}/retry", json={})
    assert missing_retry_key.status_code == 422
    retried = client.post(
        f"/api/v1/jobs/{retryable.job_id}/retry",
        json={},
        headers={"Idempotency-Key": "retry-command"},
    )
    assert retried.status_code == 202
    assert retried.json()["status"] == "queued"
    assert client.post(
        f"/api/v1/jobs/{retryable.job_id}/retry",
        json={},
        headers={"Idempotency-Key": "retry-command"},
    ).status_code == 202

    cancellable = JobRecord(
        job_id=uuid4(),
        user_id=user_id,
        book_id=book_id,
        type=JobType.IMPORT_BOOK,
        idempotency_key="cancel-contract",
        input_sha256=sha256_text("cancel"),
        pipeline_version="p1",
        created_at=now,
        updated_at=now,
    )
    services.jobs.add(cancellable)
    assert client.post(f"/api/v1/jobs/{cancellable.job_id}/cancel").status_code == 422
    cancelled = client.post(
        f"/api/v1/jobs/{cancellable.job_id}/cancel",
        headers={"Idempotency-Key": "cancel-command"},
    )
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "cancelled"

    block_id = next(iter(services.books.blocks))
    highlight_payload = {
        "chapter_id": str(chapter_id),
        "start": {"block_id": str(block_id), "offset": 6},
        "end": {"block_id": str(block_id), "offset": 14},
        "exact_quote": "contract",
        "prefix": "Hello ",
        "suffix": " world",
        "text_sha256": sha256_text("contract"),
    }
    no_highlight_key = client.post(f"/api/v1/books/{book_id}/highlights", json=highlight_payload)
    assert no_highlight_key.status_code == 422
    created = client.post(
        f"/api/v1/books/{book_id}/highlights",
        json=highlight_payload,
        headers={"Idempotency-Key": "highlight-command"},
    )
    assert created.status_code == 201
    highlight_id = UUID(created.json()["highlight_id"])
    page = client.get(f"/api/v1/books/{book_id}/highlights?limit=1")
    assert page.status_code == 200
    assert page.json()["items"][0]["highlight_id"] == str(highlight_id)
    assert client.delete(f"/api/v1/books/{book_id}/highlights/{highlight_id}").status_code == 204
    assert client.delete(f"/api/v1/books/{book_id}/highlights/{highlight_id}").status_code == 404

    no_question_key = client.post(
        f"/api/v1/books/{book_id}/questions",
        json={"question": "question", "client_request_id": str(uuid4())},
    )
    assert no_question_key.status_code == 422
    question_body = {"question": "question", "client_request_id": str(uuid4())}
    first = client.post(
        f"/api/v1/books/{book_id}/questions",
        json=question_body,
        headers={"Idempotency-Key": "question-command"},
    )
    second = client.post(
        f"/api/v1/books/{book_id}/questions",
        json=question_body,
        headers={"Idempotency-Key": "question-command"},
    )
    assert first.status_code == second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]


def test_c13_evidence_is_required_before_answer_delta_or_completed(fixture_app) -> None:
    client, services, _, book_id, _, _ = fixture_app
    response = client.post(
        f"/api/v1/books/{book_id}/questions",
        json={"question": "What is the point?", "client_request_id": str(uuid4())},
        headers={"Idempotency-Key": "question-c13"},
    )
    assert response.status_code == 202
    run_id = UUID(response.json()["run_id"])
    record = services.answers.records[run_id]
    assert [event.type.value for event in record.ledger.events] == ["accepted"]
    assert client.get(f"/api/v1/answer-runs/{run_id}/events").text.count("answer_delta") == 0
    with pytest.raises(ContractViolation) as exc:
        record.ledger.answer_delta("must not pass")
    assert exc.value.code is ErrorCode.EVIDENCE_REQUIRED


def test_c14_sse_seq_order_terminal_and_reconnect_replay_without_tool_retry(fixture_app) -> None:
    client, services, _, book_id, version_id, chapter_id = fixture_app
    session = services.auth.authenticate(next(iter(services.auth.sessions)))
    scope = ScopeContext(
        session_id=session.session_id,
        user_id=session.user_id,
        book_id=book_id,
        book_version_id=version_id,
        chapter_id=chapter_id,
        furthest_chunk_index=4,
        request_id=uuid4(),
        trace_id=uuid4(),
    )
    ledger = AnswerEventLedger(run_id=uuid4(), trace_id=scope.trace_id, request_id=scope.request_id, scope=scope)
    ledger.accepted()
    call_id = uuid4()
    ledger.start_tool(call_id, ToolName.SEARCH_BOOK)
    ledger.finish_tool(call_id, ToolName.SEARCH_BOOK, "succeeded", result_ref="evidence-store-ref")
    evidence = EvidenceRef(
        evidence_id=uuid4(),
        user_id=scope.user_id,
        book_id=scope.book_id,
        book_version_id=scope.book_version_id,
        chapter_id=scope.chapter_id,
        chunk_id=uuid4(),
        chunk_index=2,
        block_ids=[uuid4()],
        quote="replay evidence",
        content_sha256=sha256_text("replay evidence"),
        source_locator=SourceLocator(kind="synthetic", value="http-oracle"),
    )
    ledger.evidence(
        issue_verified_evidence_token(
            run_id=ledger.run_id,
            scope=scope,
            bundle=EvidenceBundle(refs=[evidence]),
            oracle=lambda _scope, _id: evidence,
        )
    )
    ledger.answer_delta("answer")
    ledger.complete(answer_id=uuid4(), conversation_id=uuid4(), evidence_ids=[evidence.evidence_id])
    services.answers.records[ledger.run_id] = _AnswerRecord(
        scope=scope,
        question="q",
        ledger=ledger,
        created_at=datetime.now(UTC),
    )
    all_events = client.get(f"/api/v1/answer-runs/{ledger.run_id}/events")
    assert all_events.status_code == 200
    lines = [line for line in all_events.text.splitlines() if line.startswith("id:")]
    seqs = [int(line.split(":", 1)[1]) for line in lines]
    assert seqs == list(range(1, len(seqs) + 1))
    assert all_events.text.count("event: completed") == 1
    resumed = client.get(
        f"/api/v1/answer-runs/{ledger.run_id}/events",
        headers={"Last-Event-ID": "3"},
    )
    assert resumed.status_code == 200
    assert "event: tool_started" not in resumed.text
    assert "event: evidence" in resumed.text
    assert resumed.text.count("event: completed") == 1
    with pytest.raises(ContractViolation):
        ledger.complete(answer_id=uuid4(), conversation_id=uuid4(), evidence_ids=[evidence.evidence_id])


def test_c15_http_errors_do_not_echo_sensitive_input(fixture_app) -> None:
    client, *_ = fixture_app
    secret = "password-do-not-echo"
    response = client.post(
        "/api/v1/sessions",
        json={"identifier": "nobody", "password": secret},
    )
    assert response.status_code == 401
    assert secret not in response.text
    assert "stack" not in response.text


def test_c17_openapi_contains_public_contracts_but_not_internal_scope(fixture_app) -> None:
    client, *_ = fixture_app
    schema = export_openapi(client.app)
    serialized = json.dumps(schema, sort_keys=True)
    assert "ScopeContext" not in serialized
    assert "/api/v1/books/{book_id}/questions" in schema["paths"]
    assert "/api/v1/answer-runs/{run_id}/events" in schema["paths"]
    assert "QuestionCreate" in schema["components"]["schemas"]
    assert "SSEEnvelope" not in schema["components"]["schemas"] or "scope" not in serialized.lower()
