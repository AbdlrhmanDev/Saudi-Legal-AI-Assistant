"""PRD v2 HTTP surface (section 14) and the multi-instance scaling safeguards."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import Request

from api.deps import RateLimiter, client_address, rate_limiter
from api.main import app
from api.security import hash_password
from legal_core.answer import answer_cache, answer_question
from legal_core.cli import create_admin
from legal_core.llm import ExtractiveAnswerModel
from legal_core.models import QueryTrace, SourceRegistry, User
from tests._env import TODAY
from tests.integration.test_api import _login, work
from tests.integration.test_nightly_sync import FIXTURE_SOURCE, LEAVE, run_once, use_feed

pytestmark = pytest.mark.integration


@pytest.fixture
def client(corpus, settings):
    create_admin(corpus, settings)
    corpus.add(User(email="reader@example.test", role="user", password_hash=hash_password("reader-password-123")))
    corpus.commit()
    rate_limiter.reset()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin(client):
    return _login(client, "admin@example.test", "test-admin-password")


@pytest.fixture
def reader(client):
    return _login(client, "reader@example.test", "reader-password-123")


def test_probes_do_not_take_the_api_down_when_the_worker_is_down(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "checks": {"database": "ok", "pgvector": "ok"}, "worker": "down"}


def test_public_freshness_updates_and_chat_contract(client, corpus, settings, embedder):
    before = client.get("/api/v1/freshness").json()["data"]
    assert before["corpus_verified_at"] is None and before["source_health"] == "degraded" and before["warning_ar"]

    use_feed(corpus, "feeds/nightly/day2_amendment.xml")
    run_once(corpus, settings, embedder)

    fresh = client.get("/api/v1/freshness").json()["data"]
    assert fresh["corpus_verified_at"] and fresh["source_health"] == "healthy" and fresh["warning_ar"] is None
    assert fresh["timezone"] == "Asia/Riyadh" and fresh["corpus_verified_at"].endswith("+03:00")
    assert fresh["latest_included_publication"] == "2026-10-09"
    assert {"labor", "commercial"} <= set(fresh["covered_domains"]) and fresh["active_generation"]["number"] >= 1
    assert fresh["sources"][0]["name"] == FIXTURE_SOURCE and fresh["next_scheduled_sync"].endswith("T00:00:00+03:00")

    updates = client.get("/api/v1/updates").json()["data"]["items"]
    kinds = {(u["change_type"], u["affected_article_number"]) for u in updates}
    assert ("new_instrument", None) in kinds and {("changed", "4"), ("changed", "6")} <= kinds
    assert all(u["is_test_data"] for u in updates)

    body = client.post("/api/v1/chat", json={"query": LEAVE, "as_of_date": TODAY.isoformat()}).json()["data"]
    assert body["corpus_verified_at"] == fresh["corpus_verified_at"] and body["source_health"] == "healthy"
    assert body["coverage_notice"] == "Selected Saudi legal sources; not exhaustive"
    citation = body["citations"][0]
    assert citation["official_url"] and citation["instrument_id"] == citation["law_id"]


def test_operator_endpoints_trigger_inspect_quarantine_and_roll_back(client, admin, reader, corpus, settings, embedder):
    for path in ("/api/v1/admin/sync-runs", "/api/v1/admin/quarantine", "/api/v1/admin/index/generations"):
        assert client.get(path, headers=reader).status_code == 403
    assert client.post("/api/v1/admin/sync-runs/trigger", headers=reader).status_code == 403

    use_feed(corpus, "feeds/nightly/ambiguous.xml")
    queued = client.post("/api/v1/admin/sync-runs/trigger", headers=admin)
    assert queued.status_code == 201 and queued.json()["data"]["run"]["trigger"] == "manual"
    assert work(settings, embedder) == ["succeeded"]

    runs = client.get("/api/v1/admin/sync-runs", headers=admin).json()["data"]["items"]
    assert runs[0]["status"] == "succeeded" and runs[0]["stage_counts"]["validate"]["quarantine"] == 3
    detail = client.get(f"/api/v1/admin/sync-runs/{runs[0]['id']}", headers=admin).json()["data"]
    assert len(detail["quarantine"]) == 3 and detail["generations"][0]["trigger"] == "nightly_sync"
    assert {e["decision"] for e in detail["audit_events"]} == {"auto_published", "auto_quarantined"}
    assert all(e["actor_id"] is None for e in detail["audit_events"])  # zero human actions

    quarantine = client.get("/api/v1/admin/quarantine", headers=admin).json()["data"]["items"]
    assert {q["reason_code"] for q in quarantine} == {
        "NO_OFFICIAL_TEXT", "CONFLICTING_EFFECTIVE_DATES", "AMBIGUOUS_AMENDMENT_MAPPING"
    }  # fmt: skip

    generations = client.get("/api/v1/admin/index/generations", headers=admin).json()["data"]["items"]
    active = next(g for g in generations if g["is_active"])
    detail = client.get(f"/api/v1/admin/index/generations/{active['id']}", headers=admin).json()["data"]
    assert detail["health"]["ok"] and detail["manifest"]

    rolled = client.post("/api/v1/admin/index/rollback", headers=admin, json={"reason": "Operator drill (test)."})
    assert rolled.status_code == 200 and rolled.json()["data"]["trigger"] == "rollback"
    stats = client.get("/api/v1/admin/stats", headers=admin).json()["data"]["sync"]
    assert stats["open_quarantine"] == 3 and stats["active_generation"]["number"] == rolled.json()["data"]["number"]


def test_quarantined_candidate_can_be_corrected_through_the_escape_hatch(client, admin, corpus, settings, embedder):
    use_feed(corpus, "feeds/nightly/ambiguous.xml")
    run_once(corpus, settings, embedder)
    item = next(
        q for q in client.get("/api/v1/admin/quarantine", headers=admin).json()["data"]["items"]
        if q["reason_code"] == "CONFLICTING_EFFECTIVE_DATES"
    )  # fmt: skip
    approved = client.post(
        f"/api/v1/admin/updates/{item['candidate_id']}/approve",
        headers=admin,
        json={"effective_from": "2026-12-01", "effective_date_evidence": "Operator verified the gazette (test)."},
    )
    assert approved.status_code == 200, approved.text
    remaining = client.get("/api/v1/admin/quarantine", headers=admin).json()["data"]["items"]
    assert item["id"] not in {q["id"] for q in remaining}


# --- scaling safeguards -----------------------------------------------------------------


def test_rate_limit_is_shared_by_every_api_process(corpus, settings):
    assert settings.rate_limit_backend == "database"
    first, second = RateLimiter(), RateLimiter()  # two processes / replicas
    first.reset()
    first.check("chat:198.51.100.7", 3)
    second.check("chat:198.51.100.7", 3)
    first.check("chat:198.51.100.7", 3)
    with pytest.raises(Exception) as excinfo:
        second.check("chat:198.51.100.7", 3)
    assert getattr(excinfo.value, "code", None) == "RATE_LIMITED"
    second.check("chat:198.51.100.8", 3)  # other clients are unaffected


def _request(peer: str, forwarded: str | None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_client_address_behind_a_proxy_cannot_be_spoofed(settings, monkeypatch):
    # Behind Railway/nginx every request arrives from the proxy: without this, all users would share
    # one rate-limit bucket.
    monkeypatch.setattr(settings, "trusted_proxy_count", 0)
    assert client_address(_request("10.0.0.2", "203.0.113.9")) == "10.0.0.2"
    monkeypatch.setattr(settings, "trusted_proxy_count", 1)
    assert client_address(_request("10.0.0.2", "203.0.113.9")) == "203.0.113.9"
    assert client_address(_request("10.0.0.2", "6.6.6.6, 203.0.113.9")) == "203.0.113.9"  # forged entry ignored
    assert client_address(_request("10.0.0.2", None)) == "10.0.0.2"


def test_answer_cache_hits_and_is_invalidated_by_a_new_generation(corpus, settings, embedder, llm, monkeypatch):
    monkeypatch.setattr(settings, "answer_cache_size", 100)
    answer_cache.clear()

    def ask() -> dict:
        return answer_question(corpus, query=LEAVE, domain=None, as_of_date=TODAY, settings=settings,
                               embedder=embedder, llm=llm)  # fmt: skip

    first, second = ask(), ask()
    assert second["answer_ar"] == first["answer_ar"] and second["trace_id"] != first["trace_id"]
    traces = corpus.scalars(select(QueryTrace).order_by(QueryTrace.query_time)).all()
    assert "cache_hit" not in traces[0].timings_ms and traces[1].timings_ms["cache_hit"] == 1
    assert traces[1].cited_article_ids == traces[0].cited_article_ids  # the audit trail stays complete

    use_feed(corpus, "feeds/nightly/day2_amendment.xml")
    run_once(corpus, settings, embedder)
    third = ask()
    assert "خمسة وعشرين" in third["answer_ar"] and third["answer_ar"] != first["answer_ar"]
    answer_cache.clear()


def test_database_connection_is_released_while_the_model_drafts(corpus, settings, embedder):
    observed: list[bool] = []

    class Probe(ExtractiveAnswerModel):
        def draft(self, *args, **kwargs):
            observed.append(corpus.in_transaction())
            return super().draft(*args, **kwargs)

    answer_question(corpus, query=LEAVE, domain=None, as_of_date=TODAY, settings=settings, embedder=embedder,
                    llm=Probe())  # fmt: skip
    assert observed == [False]


def test_sources_listing_reports_per_source_freshness(client, admin, corpus, settings, embedder):
    use_feed(corpus, "feeds/nightly/day1_new_law.xml")
    run_once(corpus, settings, embedder)
    source = corpus.scalars(select(SourceRegistry).where(SourceRegistry.name == FIXTURE_SOURCE)).one()
    assert source.last_success_at is not None and source.last_error is None
    report = client.get("/api/v1/admin/freshness", headers=admin).json()["data"]
    assert report["sources"][0]["status"] == "healthy" and report["last_run"]["logical_date"] == date(2026, 10, 12).isoformat()
