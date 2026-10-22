"""Official source registry and connectors against the real database (network replaced by fakes)."""

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select, text

from legal_core.agent import RuleClassifier
from legal_core.cli import seed_sources
from legal_core.connectors import make_official_http, probe_source
from legal_core.db import get_sessionmaker
from legal_core.jobs import enqueue, run_job
from legal_core.models import (
    DocumentVersion,
    IngestionJob,
    JudicialDecision,
    LegalInstrument,
    SourceRegistry,
    SourceSyncLog,
    UpdateCandidate,
    utcnow,
)
from legal_core.monitor import OfficialHttpClient, sync_source, sync_sources
from legal_core.release import evaluate, unprocessed_candidates

pytestmark = pytest.mark.integration

LAW_A = "https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/08381293-6388-48e2-8ad2-a9a700f2aa94/1"
LAW_B = "https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/16b97fcb-4833-4f66-8531-a9a700f161b6/1"


def law_page(title: str, extra: str = "") -> bytes:
    return f"""<html><body><h1>{title}</h1>
<p>المادة الأولى:</p><p>يسمى هذا النظام {title}.</p>
<p>المادة الثانية:</p><p>تسري أحكام هذا النظام على المنشآت الخاضعة له. {extra}</p>
<p>المادة الثالثة:</p><p>يعمل بهذا النظام بعد تسعين يوماً من تاريخ نشره في الجريدة الرسمية.</p>
</body></html>""".encode()


def listing(*links: tuple[str, str]) -> bytes:
    return ("<html><body><ul>" + "".join(f'<li><a href="{u}">{t}</a></li>' for u, t in links) + "</ul></body></html>").encode()


def source(db, key: str) -> SourceRegistry:
    row = db.scalars(select(SourceRegistry).where(SourceRegistry.source_key == key)).one()
    row.enabled = True
    db.flush()
    return row


@pytest.fixture
def registry(db, settings):
    seed_sources(db, settings)
    db.commit()
    return db


def test_registry_holds_every_official_entry_and_seeding_is_idempotent(db, settings):
    db.add(SourceRegistry(name="HRSD - Labor Law", authority="old", feed_url="https://www.hrsd.gov.sa/",
                          allowlisted_host="www.hrsd.gov.sa", crawl_policy="manual"))  # fmt: skip
    db.commit()
    seed_sources(db, settings)
    seed_sources(db, settings)
    db.commit()
    rows = db.scalars(select(SourceRegistry)).all()
    assert len(rows) == 39  # the pre-catalog HRSD row was adopted, not duplicated
    assert sorted(n for r in rows for n in r.catalog_numbers) == list(range(1, 40))
    hrsd = next(r for r in rows if r.source_key == "hrsd")
    assert hrsd.connector_type == "page_watch" and hrsd.default_domain == "labor"
    assert not any(r.enabled for r in rows)  # tests never enable a real source
    assert {r.integration_status for r in rows} == {"integrated", "discovery_only", "not_integrated", "consolidated"}
    # The spec-named views read the same rows.
    assert db.execute(text("SELECT count(*) FROM official_sources")).scalar() == 39
    assert db.execute(text("SELECT count(*) FROM source_connectors WHERE integration_status = 'integrated'")).scalar() > 0


def test_ministry_listing_links_are_attributed_to_the_bureau_and_fetched_politely(registry, settings, monkeypatch):
    db = registry
    misa, boe = source(db, "misa"), source(db, "boe")
    monkeypatch.setattr(settings, "connector_max_acquisitions_per_source", 1)
    pages = {
        misa.feed_url: listing((LAW_A, "نظام الاستثمار التجريبي"), (LAW_B, "نظام التحكيم التجريبي")),
        LAW_A: law_page("نظام الاستثمار التجريبي"),
        LAW_B: law_page("نظام التحكيم التجريبي"),
    }
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return pages[url]

    first = sync_source(db, misa, settings, fetcher=fetch)
    assert first["new"] == 2 and first["acquired"] == 1 and first["deferred"] == 1
    candidates = db.scalars(select(UpdateCandidate).order_by(UpdateCandidate.discovered_at)).all()
    assert {c.source_id for c in candidates} == {boe.id}  # provenance: the authoritative publisher
    assert all(c.evidence.get("discovered_via") == "misa" for c in candidates)
    assert misa.health_status == "available" and misa.page_fingerprint
    # The deferred document is not judged before it is fetched.
    pending = {c.id for c in unprocessed_candidates(db, 50)}
    deferred = next(c for c in candidates if c.proposed_version_id is None)
    assert deferred.id not in pending

    # Next run: the backlog is fetched first; known links are not rediscovered.
    second = sync_source(db, misa, settings, fetcher=fetch)
    assert second["acquired"] == 1 and second["new"] == 0 and second["unchanged"] == 2
    assert db.scalar(select(func.count()).select_from(DocumentVersion)) == 2
    domains = set(db.scalars(select(LegalInstrument.domain)))
    assert domains == {"investment", "judicial"}  # inferred from the official titles


def test_the_same_page_from_two_sources_is_processed_once(registry, settings):
    db = registry
    rss, council = source(db, "uqn_rss"), source(db, "uqn_council")
    decision = "https://www.uqn.gov.sa/decisions-and-regulations/4002092"
    feed = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>قرار مجلس الوزراء رقم 1</title><link>{decision}</link><description>قرار</description></item>
</channel></rss>""".encode()
    pages = {rss.feed_url: feed, council.feed_url: listing((decision, "قرار مجلس الوزراء رقم 1")),
             decision: law_page("قرار مجلس الوزراء رقم 1")}  # fmt: skip
    sync_source(db, rss, settings, fetcher=pages.__getitem__)
    counts = sync_source(db, council, settings, fetcher=pages.__getitem__)
    assert counts["duplicates_across_sources"] == 1 and counts["new"] == 0
    assert db.scalar(select(func.count()).select_from(UpdateCandidate)) == 1


def test_watched_pages_report_changes_but_never_create_legal_candidates(registry, settings):
    db = registry
    istitlaa = source(db, "istitlaa")
    page = {"v": b"<html><body><h2>\xd9\x85\xd8\xb4\xd8\xb1\xd9\x88\xd8\xb9 1</h2></body></html>"}

    def fetch(_url):
        return page["v"]

    assert sync_sources(db, settings, source_id=istitlaa.id, force=True, fetcher=fetch)[0]["page_changed"] == 0
    page["v"] = page["v"].replace(b"1", b"2")
    totals, errors = sync_sources(db, settings, source_id=istitlaa.id, force=True, fetcher=fetch)
    assert totals["page_changed"] == 1 and not errors
    assert db.scalar(select(func.count()).select_from(UpdateCandidate)) == 0
    logs = db.scalars(select(SourceSyncLog).where(SourceSyncLog.source_id == istitlaa.id)
                      .order_by(SourceSyncLog.started_at)).all()  # fmt: skip
    assert [log.status for log in logs] == ["no_change", "changed"] and logs[-1].connector_type == "page_watch"


def test_drafts_and_news_are_excluded_by_the_release_gate_whatever_their_title(registry, settings):
    db = registry
    istitlaa = source(db, "istitlaa")
    candidate = UpdateCandidate(source_id=istitlaa.id, origin="feed", external_id="draft-1", title="نظام الإيجار الجديد",
                                canonical_url="https://istitlaa.scbc.gov.sa/ar/x", changed_hash="h", classification="draft",
                                state="candidate")  # fmt: skip
    db.add(candidate)
    db.flush()
    proposal = RuleClassifier().propose(db, candidate, "")
    assert proposal.classification == "draft"  # not reclassified as a new law from its title
    decision = evaluate(db, candidate, proposal, settings)
    assert decision.outcome == "exclude" and decision.reason_code == "NOT_A_LEGAL_CHANGE"


def _judgment_api(details: dict):
    listing_payload = {
        "statusCode": 200, "success": True,
        "model": {"judgementsCollection": [
            {"id": "J1", "judgementNumber": "4530838530", "judgementDate": "1445-09-04", "city": "الرياض",
             "courtName": "المحكمة العمالية", "courtType": 1, "caseNumber": "77", "isAppeal": False},
            {"id": "J2", "judgementNumber": "4630444976", "judgementDate": "1446-05-16", "city": "جدة",
             "courtName": "محكمة الاستئناف", "courtType": 2, "caseNumber": None, "isAppeal": False},
        ], "totalCount": 2},
    }  # fmt: skip

    def fake(url: str, body=None) -> bytes:
        if url.endswith("/judgements-list"):
            return json.dumps(listing_payload, ensure_ascii=False).encode()
        key = url.split("id=", 1)[1].split("&", 1)[0]
        return json.dumps({"success": True, "model": details[key]}, ensure_ascii=False).encode()

    return fake


def test_judgments_are_stored_apart_from_legislation_and_never_duplicated(registry, settings):
    db = registry
    moj = source(db, "moj_judgments")
    details = {
        "J1": {"title": "القضية رقم 1", "judgmentNumber": "4530838530", "judgmentHijriiDate": "1445-09-04",
               "judgmentDate": "2024-03-14T00:00:00",
               "judgmentTextofRulling": "<p>الوقائع: طالب العامل بأجره المتأخر.</p><p>الحكم: إلزام صاحب العمل بالدفع.</p>"},
        "J2": {"title": "القضية رقم 2", "judgmentTextofRulling": "<p>تأييد الحكم الابتدائي.</p>"},
    }  # fmt: skip
    fake = _judgment_api(details)
    counts = sync_source(db, moj, settings, fetcher=fake)
    assert counts["judicial_new"] == 2 and counts["new"] == 0
    records = {r.external_id: r for r in db.scalars(select(JudicialDecision))}
    first = records["J1"]
    assert first.court_level == "first_instance" and records["J2"].court_level == "appeal"
    assert first.judgment_date.isoformat() == "2024-03-14" and first.finality_status == "unknown"
    assert first.source_url == "https://laws.moj.gov.sa/ar/JudicialDecisionsList/0/J1"
    assert "إلزام صاحب العمل" in first.text and first.snapshot_id is not None
    # Judgments never become legislation.
    assert db.scalar(select(func.count()).select_from(LegalInstrument)) == 0
    assert db.scalar(select(func.count()).select_from(UpdateCandidate)) == 0
    again = sync_source(db, moj, settings, fetcher=fake)
    assert again["judicial_new"] == 0 and again["unchanged"] == 2
    assert db.scalar(select(func.count()).select_from(JudicialDecision)) == 2


def test_a_blocked_or_failing_source_is_logged_and_reported_honestly(registry, settings):
    db = registry
    my_gov = source(db, "my_gov")

    def refused(url: str) -> bytes:
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("forbidden", request=request, response=httpx.Response(403, request=request))

    totals, errors = sync_sources(db, settings, source_id=my_gov.id, force=True, fetcher=refused)
    assert totals["sources_failed"] == 1 and errors
    assert my_gov.health_status == "blocked" and my_gov.last_http_status == 403 and my_gov.last_success_at is None
    log = db.scalars(select(SourceSyncLog).where(SourceSyncLog.source_id == my_gov.id)).one()
    assert log.status == "failed" and log.error.startswith("blocked") and log.http_status == 403


def test_known_laws_are_reread_and_an_amended_text_becomes_a_new_candidate(registry, settings):
    db = registry
    boe = source(db, "boe")
    pages = {boe.feed_url: listing((LAW_A, "نظام التحكيم التجريبي")), LAW_A: law_page("نظام التحكيم التجريبي")}
    sync_source(db, boe, settings, fetcher=pages.__getitem__)
    original = db.scalars(select(UpdateCandidate)).one()
    assert original.proposed_version_id is not None
    original.state = "approved"
    original.discovered_at = utcnow() - timedelta(days=settings.connector_refresh_days + 1)
    db.flush()

    pages[LAW_A] = law_page("نظام التحكيم التجريبي", extra="ويجوز الاتفاق كتابة على التحكيم.")  # amended
    counts = sync_source(db, boe, settings, fetcher=pages.__getitem__)
    assert counts["refreshed"] == 1 and counts["changed"] == 1 and counts["acquired"] == 1
    amended = db.scalars(select(UpdateCandidate).where(UpdateCandidate.id != original.id)).one()
    assert amended.evidence["refresh_of"] == str(original.id) and amended.proposed_version_id
    assert db.scalar(select(func.count()).select_from(DocumentVersion)) == 2  # history kept

    # An unchanged re-read only records the check.
    original.evidence = {}
    original.discovered_at = utcnow() - timedelta(days=settings.connector_refresh_days + 1)
    amended.state = "approved"
    db.flush()
    counts = sync_source(db, boe, settings, fetcher=pages.__getitem__)
    assert counts["changed"] == 0


def test_probe_job_records_every_source_without_creating_candidates(registry, settings, embedder):
    db = registry

    def fetch(url: str, body=None) -> bytes:
        if "judgements-list" in url:
            return b'{"success": true, "model": {"judgementsCollection": [], "totalCount": 0}}'
        return b"<html><body>page</body></html>"

    job = enqueue(db, "source_probe", {"source_id": None})
    db.commit()
    run_job(get_sessionmaker(), job.id, settings, embedder, fetch)
    db.expire_all()
    job = db.get(IngestionJob, job.id)
    assert job.status == "succeeded" and len(job.result["results"]) == 39
    rows = db.scalars(select(SourceRegistry)).all()
    assert all(r.last_probe_at is not None for r in rows if not r.consolidated_into)
    # Listings with no matching links and an empty judgments API are "degraded", not "connected".
    assert db.scalars(select(SourceRegistry).where(SourceRegistry.source_key == "boe")).one().health_status == "degraded"
    assert db.scalar(select(func.count()).select_from(UpdateCandidate)) == 0


def test_probe_reports_robots_disallow_for_the_sama_rulebook(registry, settings):
    db = registry
    sama = db.scalars(select(SourceRegistry).where(SourceRegistry.source_key == "sama")).one()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: GoogleBot\nAllow: /\n\nUser-agent: *\nDisallow: /\n")
        raise AssertionError("must not fetch a disallowed page")



    client = OfficialHttpClient(settings, resolve=lambda _h: None, transport=httpx.MockTransport(handler))
    result = probe_source(sama, client, settings)
    assert result["status"] == "robots_disallowed" and sama.health_status == "robots_disallowed"
    assert make_official_http(settings) is not None


def test_implementing_regulations_are_linked_to_their_parent_law(db):
    from legal_core.models import DocumentRelationship
    from legal_core.relationships import link_instruments

    law = LegalInstrument(official_title_ar="نظام العمل", title_normalized="نظام العمل")
    regulation = LegalInstrument(official_title_ar="اللائحة التنفيذية لنظام العمل", title_normalized="اللايحه التنفيذيه لنظام العمل")
    unrelated = LegalInstrument(official_title_ar="اللائحة التنفيذية لنظام غير موجود", title_normalized="x")
    db.add_all([law, regulation, unrelated])
    db.flush()
    assert link_instruments(db) == 1
    assert link_instruments(db) == 0  # idempotent
    link = db.scalars(select(DocumentRelationship)).one()
    assert (link.from_instrument_id, link.to_instrument_id, link.relation_type) == (
        regulation.id, law.id, "implementing_regulation_of"
    )


def boe_page(status: str) -> bytes:
    articles = "".join(
        f'<div class="article_item"><h3>المادة {name}</h3><div>{text}</div></div>'
        for name, text in (
            ("الأولى", "يسمى هذا النظام نظام المحفوظات التجريبي."),
            ("الثانية", "تحفظ الوثائق الرسمية لدى المركز وفق أحكام هذا النظام."),
        )
    )
    return f"""<html><body><div><span>الاسم</span><span>نظام المحفوظات التجريبي</span></div>
<div>تاريخ الإصدار</div><div>1409/05/16 هـ  الموافق : 25/12/1988 مـ</div>
<div>تاريخ النشر</div><div>1409/05/27  هـ الموافق : 05/01/1989 مـ</div>
<div>الحالة</div><div>{status}</div>
<div id="divLawText"><h3>نظام المحفوظات التجريبي</h3>{articles}</div></body></html>""".encode()


@pytest.mark.parametrize(("status", "outcome"), [("ساري", "publish"), ("ملغي", "quarantine")])
def test_bureau_status_verifies_a_consolidated_law_only_from_its_retrieval_date(registry, settings, status, outcome):
    from legal_core.parser import parse_boe_metadata

    db = registry
    boe = source(db, "boe")
    pages = {boe.feed_url: listing((LAW_A, "نظام المحفوظات التجريبي")), LAW_A: boe_page(status)}
    sync_source(db, boe, settings, fetcher=pages.__getitem__)
    candidate = db.scalars(select(UpdateCandidate)).one()
    meta = parse_boe_metadata(pages[LAW_A])
    assert meta.status == status and meta.published_at.isoformat() == "1989-01-05"

    decision = evaluate(db, candidate, RuleClassifier().propose(db, candidate, ""), settings)
    assert decision.outcome == outcome
    if outcome == "publish":
        # Not back-dated: the text is verified in force from the day its official page was read.
        assert decision.effective.value == utcnow().astimezone(ZoneInfo("Asia/Riyadh")).date()
        assert decision.effective.evidence[0].kind == "publisher_status"
        assert candidate.proposed_version.published_at.isoformat() == "1989-01-05"
    else:
        assert decision.reason_code == "STATUS_NOT_IN_FORCE"


def test_weak_page_headings_never_merge_different_documents(registry, settings):
    from legal_core.ingestion import DocumentMetadata, ingest_document

    db = registry
    ids = []
    for number, title in ((1, "اعتماد دليل نقل الثروة الحيوانية"), (2, "الموافقة على دليل تراخيص الاستيراد")):
        candidate = UpdateCandidate(origin="feed", external_id=f"g{number}", title=title, changed_hash=str(number),
                                    classification="regulation", state="retrieved")  # fmt: skip
        db.add(candidate)
        db.flush()
        content = f"دليل\n\nالمادة الأولى:\nنص الوثيقة رقم {number}.".encode()
        result = ingest_document(db, content=content, filename="x.txt", mime_type="text/plain",
                                 meta=DocumentMetadata(candidate_id=candidate.id), settings=settings)  # fmt: skip
        ids.append(result.instrument_id)
    assert ids[0] != ids[1]
    titles = sorted(db.scalars(select(LegalInstrument.official_title_ar)))
    assert titles == ["دليل تراخيص الاستيراد", "دليل نقل الثروة الحيوانية"]
