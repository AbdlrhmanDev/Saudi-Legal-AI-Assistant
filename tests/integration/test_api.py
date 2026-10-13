"""HTTP contract: envelope, validation, server-side authorization, and the admin review flow."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.deps import rate_limiter
from api.main import app
from api.security import hash_password
from legal_core.cli import create_admin
from legal_core.db import get_sessionmaker
from legal_core.jobs import claim_next_job, run_job
from legal_core.models import IngestionJob, User, WorkerHeartbeat

pytestmark = pytest.mark.integration

PROBATION = "كم مدة فترة التجربة في نظام العمل؟"
NEW_LAW = """نظام الطائرات المسيرة التجريبي (بيانات اختبار غير رسمية)

المادة الأولى:
يحظر تشغيل الطائرات المسيرة فوق المنشآت الحيوية دون تصريح مسبق من الجهة المختصة.

المادة الثانية:
تسجل كل طائرة مسيرة يزيد وزنها على مئتين وخمسين غراماً قبل استخدامها.
"""


@pytest.fixture
def client(corpus, settings):
    create_admin(corpus, settings)
    corpus.add(User(email="reader@example.test", role="user", password_hash=hash_password("reader-password-123")))
    corpus.commit()
    rate_limiter.reset()
    with TestClient(app) as test_client:
        yield test_client


def _login(client, email, password) -> dict:
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture
def admin(client):
    return _login(client, "admin@example.test", "test-admin-password")


@pytest.fixture
def reader(client):
    return _login(client, "reader@example.test", "reader-password-123")


def work(settings, embedder) -> list[str]:
    """Drain the job queue the way the worker process does."""
    statuses = []
    factory = get_sessionmaker()
    while True:
        with factory() as session:
            job_id = claim_next_job(session)
        if job_id is None:
            return statuses
        statuses.append(run_job(factory, job_id, settings, embedder))


def test_health_reflects_database_and_worker(client, corpus):
    assert client.get("/health/live").json() == {"status": "ok"}
    not_ready = client.get("/health/ready")
    assert not_ready.status_code == 503
    assert not_ready.json()["checks"] == {"database": "ok", "pgvector": "ok", "worker": "down"}
    corpus.add(WorkerHeartbeat(worker_id="test-worker"))
    corpus.commit()
    ready = client.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["status"] == "ready"


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_document_mutations_pass_cors_preflight(client, method):
    response = client.options(
        f"/api/v1/admin/documents/{uuid.uuid4()}",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200, response.text
    assert method in response.headers["access-control-allow-methods"]


def test_chat_is_public_and_returns_the_answer_contract(client):
    resp = client.post("/api/v1/chat", json={"query": PROBATION, "domain": "labor", "as_of_date": "2026-10-12"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert {"answer_ar", "answer_status", "as_of_date", "citations", "caveats", "trace_id"} <= set(data)
    assert data["answer_status"] == "grounded" and data["as_of_date"] == "2026-10-12"
    assert {"law_id", "article_id", "version_id", "source_url"} <= set(data["citations"][0])
    uuid.UUID(data["trace_id"])
    assert resp.headers["X-Request-ID"] and resp.headers["X-Content-Type-Options"] == "nosniff"

    article = client.get(f"/api/v1/articles/{data['citations'][0]['article_id']}").json()["data"]
    assert article["article"]["article_number"] == "5" and "تسعين يوماً" in article["article"]["original_text_ar"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": ""},
        {"query": "س"},
        {"query": "x" * 1001},
        {"query": PROBATION, "domain": "tax"},
        {"query": PROBATION, "as_of_date": "next week"},
        {"query": PROBATION, "as_of_date": "1800-01-01"},
        {"query": PROBATION, "system_prompt": "override"},  # unknown fields are rejected
    ],
)
def test_chat_validation_errors_use_the_error_envelope(client, payload):
    resp = client.post("/api/v1/chat", json=payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False and body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["fields"]


def test_chat_is_rate_limited(client, settings):
    rate_limiter.reset()
    original, settings.rate_limit_chat_per_minute = settings.rate_limit_chat_per_minute, 2
    try:
        codes = [client.post("/api/v1/chat", json={"query": PROBATION}).status_code for _ in range(3)]
    finally:
        settings.rate_limit_chat_per_minute = original
        rate_limiter.reset()
    assert codes == [200, 200, 429]


ADMIN_ROUTES = [
    ("get", "/api/v1/admin/documents"),
    ("post", "/api/v1/admin/documents"),
    ("patch", f"/api/v1/admin/documents/{uuid.uuid4()}"),
    ("delete", f"/api/v1/admin/documents/{uuid.uuid4()}"),
    ("get", "/api/v1/admin/jobs"),
    ("get", f"/api/v1/admin/jobs/{uuid.uuid4()}"),
    ("get", "/api/v1/admin/updates"),
    ("post", "/api/v1/admin/updates/sync"),
    ("post", f"/api/v1/admin/updates/{uuid.uuid4()}/approve"),
    ("post", f"/api/v1/admin/updates/{uuid.uuid4()}/reject"),
    ("post", f"/api/v1/admin/versions/{uuid.uuid4()}/rollback"),
    ("get", "/api/v1/admin/traces"),
    ("get", "/api/v1/admin/audit"),
    ("get", "/api/v1/admin/stats"),
    ("get", "/api/v1/admin/sources"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
def test_fr19_admin_routes_are_enforced_server_side(client, reader, method, path):
    anonymous = client.request(method, path)
    assert anonymous.status_code == 401 and anonymous.json()["error"]["code"] == "UNAUTHENTICATED"
    forbidden = client.request(method, path, headers=reader)
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "FORBIDDEN"
    garbage = client.request(method, path, headers={"Authorization": "Bearer not.a.token"})
    assert garbage.status_code == 401 and garbage.json()["error"]["code"] == "INVALID_TOKEN"


def test_login_rejects_bad_credentials_without_detail(client):
    for email, password in (("admin@example.test", "wrong"), ("nobody@example.test", "test-admin-password")):
        resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert resp.status_code == 401 and resp.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert client.get("/api/v1/auth/me").status_code == 401


def test_catalog_lists_only_reviewed_material(client, admin):
    listing = client.get("/api/v1/laws", params={"limit": 2}).json()["data"]
    assert listing["pagination"] == {"page": 1, "limit": 2, "total": 5, "totalPages": 3}
    assert len(listing["items"]) == 2

    found = client.get("/api/v1/laws", params={"q": "الشركات"}).json()["data"]["items"]
    assert [i["domain"] for i in found] == ["commercial"]
    typo = client.get("/api/v1/laws", params={"q": "حمايه البيانات الشخصيه"}).json()["data"]["items"]
    assert typo and typo[0]["domain"] == "data_protection"
    assert client.get("/api/v1/laws", params={"domain": "labor"}).json()["data"]["pagination"]["total"] == 2

    law_id = found[0]["id"]
    versions = client.get(f"/api/v1/laws/{law_id}/versions", params={"as_of_date": "2023-06-01"}).json()["data"]["versions"]
    assert [v["legal_status"] for v in versions] == ["scheduled"]  # status is relative to the as-of date
    assert "review_status" not in versions[0]
    articles = client.get(f"/api/v1/versions/{versions[0]['id']}/articles").json()["data"]["articles"]
    assert len(articles) == 9
    assert client.get(f"/api/v1/laws/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/v1/laws/not-a-uuid").status_code == 422
    coverage = client.get("/api/v1/coverage").json()["data"]
    assert {d["domain"] for d in coverage["domains"]} == {"labor", "commercial", "data_protection"}


def test_upload_review_approve_flow_over_http(client, admin, corpus, settings, embedder):
    question = {"query": "ما حكم تشغيل الطائرات المسيرة فوق المنشآت الحيوية؟", "as_of_date": "2026-10-12"}
    assert client.post("/api/v1/chat", json=question).json()["data"]["answer_status"] == "insufficient_evidence"

    # FR-01: upload -> job id.
    upload = client.post(
        "/api/v1/admin/documents",
        headers=admin,
        files={"file": ("../../etc/drone law.txt", NEW_LAW.encode(), "text/plain")},
        data={"domain": "other", "source_url": "https://laws.boe.gov.sa/test-only", "published_at": "2026-09-01"},
    )
    assert upload.status_code == 201, upload.text
    job_id = upload.json()["data"]["job_id"]
    assert client.get(f"/api/v1/admin/jobs/{job_id}", headers=admin).json()["data"]["status"] == "queued"
    assert work(settings, embedder) == ["succeeded"]

    job = client.get(f"/api/v1/admin/jobs/{job_id}", headers=admin).json()["data"]
    assert job["status"] == "succeeded" and job["counts"]["articles"] == 2
    assert job["result"]["metadata_gaps"] == ["issuing_authority", "effective_from"]
    stored = corpus.get(IngestionJob, uuid.UUID(job_id)).payload
    assert stored["filename"] == "drone law.txt" and "/" not in stored["storage_path"]  # no path traversal

    # The parsed version waits in the review queue and is invisible to the public.
    candidate_id = job["result"]["candidate_id"]
    queue = client.get("/api/v1/admin/updates", headers=admin, params={"state": "needs_review"}).json()["data"]
    assert [c["id"] for c in queue["items"]] == [candidate_id]
    detail = client.get(f"/api/v1/admin/updates/{candidate_id}", headers=admin).json()["data"]
    assert detail["candidate"]["classification"] == "new_law" and detail["proposed_version"]["review_status"] == "needs_review"
    assert client.post("/api/v1/chat", json=question).json()["data"]["answer_status"] == "insufficient_evidence"
    assert client.get("/api/v1/laws").json()["data"]["pagination"]["total"] == 5

    # Approval without a confirmed effective date is refused.
    refused = client.post(
        f"/api/v1/admin/updates/{candidate_id}/approve", headers=admin, json={"effective_date_evidence": "not stated"}
    )
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "EFFECTIVE_DATE_REQUIRED"

    approved = client.post(
        f"/api/v1/admin/updates/{candidate_id}/approve",
        headers=admin,
        json={"effective_from": "2026-10-01", "effective_date_evidence": "Article 2 of the test issuing decision."},
    )
    assert approved.status_code == 200, approved.text
    version = approved.json()["data"]["version"]
    assert version["legal_status"] == "active" and version["index_active"] is True

    answer = client.post("/api/v1/chat", json=question).json()["data"]
    assert answer["answer_status"] == "grounded" and answer["citations"][0]["version_id"] == version["id"]
    assert answer["citations"][0]["source_url"] == "https://laws.boe.gov.sa/test-only"
    assert client.get("/api/v1/laws").json()["data"]["pagination"]["total"] == 6

    # Trace and audit trail are inspectable by the admin.
    trace = client.get(f"/api/v1/admin/traces/{answer['trace_id']}", headers=admin).json()["data"]
    assert trace["cited_article_ids"] == [answer["citations"][0]["article_id"]] and trace["query_text"] is None
    audit = client.get("/api/v1/admin/audit", headers=admin, params={"decision": "approved"}).json()["data"]["items"]
    assert audit[0]["version_id"] == version["id"] and audit[0]["actor_id"]

    # Approving twice is a conflict; a duplicate upload is detected.
    again = client.post(
        f"/api/v1/admin/updates/{candidate_id}/approve",
        headers=admin,
        json={"effective_from": "2026-10-01", "effective_date_evidence": "same evidence"},
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "INVALID_STATE"
    immutable_patch = client.patch(
        f"/api/v1/admin/documents/{version['id']}", headers=admin, json={"title_ar": "عنوان لا يُحفظ"}
    )
    assert immutable_patch.status_code == 409 and immutable_patch.json()["error"]["code"] == "IMMUTABLE_VERSION"
    immutable_delete = client.delete(f"/api/v1/admin/documents/{version['id']}", headers=admin)
    assert immutable_delete.status_code == 409 and immutable_delete.json()["error"]["code"] == "IMMUTABLE_VERSION"
    client.post("/api/v1/admin/documents", headers=admin, files={"file": ("drone.txt", NEW_LAW.encode(), "text/plain")})
    assert work(settings, embedder) == ["duplicate"]

    # FR-17 over HTTP.
    rolled = client.post(f"/api/v1/admin/versions/{version['id']}/rollback", headers=admin, json={"reason": "demo rollback"})
    assert rolled.status_code == 200 and rolled.json()["data"]["decision"] == "rolled_back"
    assert client.post("/api/v1/chat", json=question).json()["data"]["answer_status"] == "insufficient_evidence"
    document = client.get(f"/api/v1/admin/documents/{version['id']}", headers=admin).json()["data"]
    assert document["version"]["review_status"] == "rolled_back" and len(document["articles"]) == 2
    assert [e["decision"] for e in document["review_events"]] == ["approved", "rolled_back"]


def test_unapproved_document_can_be_viewed_edited_and_deleted(client, admin, settings, embedder):
    upload = client.post(
        "/api/v1/admin/documents",
        headers=admin,
        files={"file": ("draft.txt", NEW_LAW.encode(), "text/plain")},
        data={"domain": "other", "source_url": "https://laws.boe.gov.sa/draft-test"},
    )
    assert upload.status_code == 201, upload.text
    assert work(settings, embedder) == ["succeeded"]
    job = client.get(f"/api/v1/admin/jobs/{upload.json()['data']['job_id']}", headers=admin).json()["data"]
    version_id = job["result"]["version_id"]

    detail = client.get(f"/api/v1/admin/documents/{version_id}", headers=admin)
    assert detail.status_code == 200
    assert len(detail.json()["data"]["articles"]) == 2

    patched = client.patch(
        f"/api/v1/admin/documents/{version_id}",
        headers=admin,
        json={
            "title_ar": "نظام الطائرات المسيرة التجريبي المعدل",
            "issuing_authority": "جهة اختبار رسمية",
            "published_at": "2026-09-02",
            "effective_from": "2026-10-01",
        },
    )
    assert patched.status_code == 200, patched.text
    edited = patched.json()["data"]
    assert edited["instrument_title_ar"] == "نظام الطائرات المسيرة التجريبي المعدل"
    assert edited["issuing_authority"] == "جهة اختبار رسمية"
    assert edited["effective_from"] == "2026-10-01"
    assert edited["metadata_gaps"] == []

    deleted = client.delete(f"/api/v1/admin/documents/{version_id}", headers=admin)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["id"] == version_id
    assert client.get(f"/api/v1/admin/documents/{version_id}", headers=admin).status_code == 404


@pytest.mark.parametrize(
    ("files", "data", "code"),
    [
        ({"file": ("law.pdf", b"PK\x03\x04 zip bytes", "application/pdf")}, {}, "INVALID_DOCUMENT"),
        ({"file": ("law.exe", b"MZ\x90\x00", "application/octet-stream")}, {}, "INVALID_DOCUMENT"),
        ({"file": ("law.txt", b"", "text/plain")}, {}, "VALIDATION_ERROR"),
        ({"file": ("law.txt", "نص".encode(), "text/plain")}, {"source_url": "javascript:alert(1)"}, "INVALID_DOCUMENT"),
        ({"file": ("law.txt", "نص".encode(), "text/plain")}, {"domain": "tax"}, "VALIDATION_ERROR"),
        (None, {"register_url": "https://evil.example/law.html"}, "SOURCE_NOT_ALLOWLISTED"),
        (None, {"register_url": "http://laws.boe.gov.sa/law"}, "SOURCE_NOT_ALLOWLISTED"),
        (None, {}, "VALIDATION_ERROR"),
    ],
)
def test_fr01_upload_validation(client, admin, corpus, files, data, code):
    resp = client.post("/api/v1/admin/documents", headers=admin, files=files, data=data)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == code
    assert corpus.scalars(select(IngestionJob)).first() is None  # nothing was queued


def test_oversized_upload_is_rejected(client, admin, settings):
    original, settings.max_upload_bytes = settings.max_upload_bytes, 64
    try:
        resp = client.post("/api/v1/admin/documents", headers=admin, files={"file": ("big.txt", b"a" * 65, "text/plain")})
    finally:
        settings.max_upload_bytes = original
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_feed_sync_and_reviewer_overrides_over_http(client, admin, settings, embedder):
    queued = client.post("/api/v1/admin/updates/sync", headers=admin)
    assert queued.status_code == 201
    assert work(settings, embedder) == ["succeeded"]
    run = client.get(f"/api/v1/admin/jobs/{queued.json()['data']['job_id']}", headers=admin).json()["data"]
    assert run["counts"]["new"] == 3 and run["finished_at"]

    updates = client.get("/api/v1/admin/updates", headers=admin).json()["data"]["items"]
    by_class = {u["classification"]: u for u in updates if u["origin"] == "feed"}
    assert set(by_class) == {"amendment", "decision", "irrelevant"}
    assert by_class["amendment"]["affected_article_numbers"] == ["5"]
    detail = client.get(f"/api/v1/admin/updates/{by_class['amendment']['id']}", headers=admin).json()["data"]
    assert detail["candidate"]["diff"]["changes"][0]["before"] and detail["candidate"]["canonical_url"]

    # FR-14: the reviewer can override the automatic classification.
    notice_id = by_class["decision"]["id"]
    patched = client.patch(
        f"/api/v1/admin/updates/{notice_id}", headers=admin, json={"classification": "irrelevant", "reason": "administrative only"}
    ).json()["data"]
    assert patched["classification"] == "irrelevant" and patched["classification_source"] == "reviewer"

    # A candidate with no attached official text cannot be approved.
    no_doc = client.post(
        f"/api/v1/admin/updates/{by_class['irrelevant']['id']}/approve",
        headers=admin,
        json={"effective_from": "2026-11-01", "effective_date_evidence": "none available"},
    )
    assert no_doc.status_code == 409

    rejected = client.post(f"/api/v1/admin/updates/{notice_id}/reject", headers=admin, json={"reason": "not legislation"})
    assert rejected.status_code == 200 and rejected.json()["data"]["decision"] == "rejected"
    assert client.post(f"/api/v1/admin/updates/{notice_id}/reject", headers=admin, json={"reason": "again"}).status_code == 409
    assert client.post(f"/api/v1/admin/updates/{notice_id}/reject", headers=admin, json={}).status_code == 422

    stats = client.get("/api/v1/admin/stats", headers=admin).json()["data"]
    assert stats["updates_by_state"]["rejected"] == 1 and stats["jobs_by_status"] == {"succeeded": 1}
    assert stats["last_successful_source_check"] and stats["worker"]["alive"] is False
    sources = client.get("/api/v1/admin/sources", headers=admin).json()["data"]["items"]
    assert len(sources) == 6 and [s["name"] for s in sources if s["enabled"]] == ["TEST FIXTURE - simulated official feed"]

    # Switching sources on and off: a feed source can be enabled, a manual-upload source cannot.
    by_policy = {s["crawl_policy"]: s for s in sources}
    feed, manual = by_policy["feed_only"], by_policy["manual"]
    on = client.patch(f"/api/v1/admin/sources/{feed['id']}", headers=admin, json={"enabled": True})
    assert on.status_code == 200 and on.json()["data"]["enabled"] is True
    off = client.patch(f"/api/v1/admin/sources/{feed['id']}", headers=admin, json={"enabled": False})
    assert off.json()["data"]["enabled"] is False
    refused = client.patch(f"/api/v1/admin/sources/{manual['id']}", headers=admin, json={"enabled": True})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "SOURCE_MANUAL_ONLY"


def test_errors_never_leak_internals(client, admin):
    missing = client.get(f"/api/v1/admin/jobs/{uuid.uuid4()}", headers=admin)
    assert missing.status_code == 404 and missing.json() == {
        "success": False,
        "error": {"code": "NOT_FOUND", "message": "Job not found.", "details": {}},
    }
    unknown = client.get("/api/v1/does-not-exist")
    assert unknown.status_code == 404 and unknown.json()["success"] is False
    assert "Traceback" not in unknown.text and Path(__file__).name not in unknown.text


def test_openapi_documents_the_contract(client):
    spec = client.get("/openapi.json").json()
    for path in (
        "/api/v1/chat",
        "/api/v1/laws",
        "/api/v1/laws/{law_id}/versions",
        "/api/v1/admin/documents",
        "/api/v1/admin/jobs/{job_id}",
        "/api/v1/admin/updates",
        "/api/v1/admin/updates/sync",
        "/api/v1/admin/updates/{candidate_id}/approve",
        "/api/v1/admin/updates/{candidate_id}/reject",
        "/api/v1/admin/versions/{version_id}/rollback",
        "/health/live",
        "/health/ready",
    ):
        assert path in spec["paths"], path
    assert "ChatAnswer" in spec["components"]["schemas"]


# --- self-service accounts -------------------------------------------------------


def test_register_creates_a_read_only_account(client):
    body = {"name": "سارة", "email": "Sara@Example.test", "password": "longpassword1", "accept_terms": True}
    created = client.post("/api/v1/auth/register", json=body)
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["user"]["role"] == "user" and data["user"]["email"] == "sara@example.test" and data["user"]["name"] == "سارة"
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
    assert client.get("/api/v1/admin/stats", headers=headers).status_code == 403  # never an admin

    assert client.post("/api/v1/auth/register", json=body).json()["error"]["code"] == "EMAIL_TAKEN"
    for bad in (
        {**body, "email": "new@example.test", "password": "short1"},
        {**body, "email": "new@example.test", "password": "nodigitshere"},
        {**body, "email": "new@example.test", "accept_terms": False},
        {**body, "email": "not-an-email"},
        {**body, "email": "new@example.test", "role": "admin"},  # cannot choose a role
    ):
        assert client.post("/api/v1/auth/register", json=bad).status_code == 422, bad


def test_password_reset_flow(client, reader):
    # Same answer whether or not the address exists.
    unknown = client.post("/api/v1/auth/forgot", json={"email": "nobody@example.test"}).json()
    known = client.post("/api/v1/auth/forgot", json={"email": "reader@example.test"}).json()
    assert unknown["message"] == known["message"] and "dev_reset_token" not in unknown["data"]
    first = known["data"]["dev_reset_token"]  # returned outside production only
    second = client.post("/api/v1/auth/forgot", json={"email": "reader@example.test"}).json()["data"]["dev_reset_token"]

    stale = client.post("/api/v1/auth/reset", json={"token": first, "password": "brandnewpass1"})
    assert stale.status_code == 422 and stale.json()["error"]["code"] == "INVALID_RESET_TOKEN"  # superseded
    assert client.post("/api/v1/auth/reset", json={"token": second, "password": "weak"}).status_code == 422
    assert client.post("/api/v1/auth/reset", json={"token": second, "password": "brandnewpass1"}).status_code == 200
    assert client.post("/api/v1/auth/reset", json={"token": second, "password": "anotherpass12"}).status_code == 422  # single use

    old_login = client.post("/api/v1/auth/login", json={"email": "reader@example.test", "password": "reader-password-123"})
    assert old_login.status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": "reader@example.test", "password": "brandnewpass1"}).status_code == 200


def test_reset_token_is_not_returned_in_production(client, settings):
    original, settings.environment = settings.environment, "production"
    try:
        data = client.post("/api/v1/auth/forgot", json={"email": "reader@example.test"}).json()["data"]
    finally:
        settings.environment = original
    assert data == {"sent": True}
