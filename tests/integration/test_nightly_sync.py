"""PRD v2 acceptance tests: autonomous nightly synchronization (section 16, T01-T10, FR-01..FR-13).

Every scenario runs the real pipeline against PostgreSQL + pgvector with clearly labelled fictional
fixtures (fixtures/nightly, fixtures/feeds/nightly). No test performs a manual approval: a valid
change must reach answers with zero clicks, and an invalid one must never reach them.
"""

import json
import logging
import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select, update

from legal_core import generations
from legal_core.agent import AgentToolbox, OpenAIClassifier, ToolBoundaryError
from legal_core.db import get_sessionmaker
from legal_core.freshness import freshness_report
from legal_core.models import (
    AmendmentRelation,
    ArticleChunk,
    DocumentVersion,
    IndexGeneration,
    LegalInstrument,
    QuarantineEvent,
    ReviewEvent,
    SourceRegistry,
    SyncRun,
    UpdateCandidate,
)
from legal_core.review import ReviewError, rollback_generation
from legal_core.sync import ensure_run, execute_run, run_due_syncs, sync_lock
from tests._env import TODAY

pytestmark = pytest.mark.integration

RIYADH = ZoneInfo("Asia/Riyadh")
FIXTURE_SOURCE = "TEST FIXTURE - simulated official feed"
LEAVE = "ما مدة الإجازة السنوية للعامل في نظام العمل؟"
PROBATION = "كم مدة فترة التجربة في نظام العمل؟"
CONSUMER = "ما مدة فسخ العقد للمستهلك في نظام التجارة الإلكترونية؟"
CAPITAL = "ما الحد الأدنى لرأس مال الشركة ذات المسؤولية المحدودة في نظام الشركات؟"


class CountingEmbedder:
    """Wraps the offline embedder and records every text sent to the embedding model."""

    def __init__(self, inner) -> None:
        self.inner, self.model_name, self.is_semantic = inner, inner.model_name, inner.is_semantic
        self.inputs: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.inputs.extend(texts)
        return self.inner.embed(texts)


def midnight(day: date, minute: int = 1) -> datetime:
    return datetime.combine(day, time(0, minute), RIYADH)


def use_feed(db, feed: str) -> None:
    source = db.scalars(select(SourceRegistry).where(SourceRegistry.name == FIXTURE_SOURCE)).one()
    source.feed_url = feed
    db.commit()


def run_once(db, settings, embedder, *, now=None, fetcher=None, classifier=None) -> SyncRun:
    """One logical run through the same pipeline the scheduler uses."""
    run = ensure_run(db, TODAY, "manual", settings)
    db.commit()
    execute_run(get_sessionmaker(), run.id, settings, embedder, now=now or midnight(TODAY), fetcher=fetcher,
                classifier=classifier)  # fmt: skip
    db.expire_all()
    return db.get(SyncRun, run.id)


def active(db) -> IndexGeneration:
    return db.scalars(select(IndexGeneration).where(IndexGeneration.is_active.is_(True))).one()


def candidate(db, external_id: str) -> UpdateCandidate:
    return db.scalars(select(UpdateCandidate).where(UpdateCandidate.external_id == external_id)).one()


def quotes(result: dict) -> str:
    return " ".join(c["quote"] for c in result["citations"])


def human_events(db) -> int:
    return db.scalar(select(func.count(ReviewEvent.id)).where(ReviewEvent.actor_id.is_not(None))) or 0


# --- T01 / T07: discovery, zero-click publication, exclusion -----------------------------


def test_t01_new_instrument_is_discovered_validated_indexed_and_answered_with_zero_clicks(
    ask, corpus, settings, embedder, caplog
):
    use_feed(corpus, "feeds/nightly/day1_new_law.xml")
    before = active(corpus).number
    with caplog.at_level(logging.WARNING, logger="alerts"):
        run = run_once(corpus, settings, embedder)

    assert run.status == "succeeded"
    assert run.stage_counts["validate"]["publish"] == 1 and run.stage_counts["activate"]["generation"] == before + 1
    item = candidate(corpus, "nightly-2001")
    version = item.proposed_version
    assert item.state == "approved" and version.review_status == "approved" and version.index_active
    assert version.effective_from == date(2026, 10, 8)  # "يعمل بهذا النظام من تاريخ نشره" + feed pubDate
    assert version.snapshot_id is not None and version.validation_evidence["method"] == "release_gate"
    assert version.validation_evidence["gate"]["effective_date"]["evidence"][0]["kind"] == "from_publication"
    relation = corpus.scalars(select(AmendmentRelation).where(AmendmentRelation.amending_version_id == version.id)).one()
    assert relation.change_type == "new_instrument" and relation.sync_run_id == run.id
    generation = active(corpus)
    assert generation.trigger == "nightly_sync" and generation.health["ok"] and generation.sync_run_id == run.id
    update_alert = next(
        r for r in caplog.records if r.getMessage() == "alert" and r.alert_kind == "legal_updates"
    )
    assert update_alert.alert_details["published"] == 1
    assert update_alert.alert_details["index_generation"] == generation.number

    result = ask(CONSUMER)
    assert result["answer_status"] in ("grounded", "partial")
    assert any(c["version_id"] == str(version.id) and c["official_url"] for c in result["citations"])
    assert "سبعة أيام" in quotes(result)
    assert human_events(corpus) == 0  # FR-19: no manual action anywhere


def test_t07_drafts_and_press_releases_never_enter_the_authoritative_corpus(corpus, settings, embedder):
    use_feed(corpus, "feeds/nightly/day1_new_law.xml")
    run_once(corpus, settings, embedder)
    draft, press = candidate(corpus, "nightly-2002"), candidate(corpus, "nightly-2003")
    assert (draft.classification, draft.state) == ("draft", "excluded")
    assert (press.classification, press.state) == ("irrelevant", "excluded")
    assert draft.proposed_version_id is None  # a draft is never even fetched
    excluded = corpus.scalars(select(ReviewEvent.reason).where(ReviewEvent.candidate_id == draft.id)).one()
    assert excluded.startswith("NOT_A_LEGAL_CHANGE")


# --- T02 / T03 / T04 / T05: idempotence, incremental embeddings, temporal answers ---------


def test_t02_identical_second_run_is_a_noop(corpus, settings, embedder):
    use_feed(corpus, "feeds/nightly/day1_new_law.xml")
    run_once(corpus, settings, embedder)
    snapshot = lambda: (  # noqa: E731
        corpus.scalar(select(func.count(DocumentVersion.id))),
        corpus.scalar(select(func.count(ArticleChunk.id))),
        corpus.scalar(select(func.count(IndexGeneration.id))),
        corpus.scalar(select(func.count(UpdateCandidate.id))),
    )
    before = snapshot()
    counting = CountingEmbedder(embedder)
    second = run_once(corpus, settings, counting)
    assert second.status == "no_op"
    assert snapshot() == before and counting.inputs == []


def test_t03_t04_t05_amendment_reembeds_only_changed_articles_and_keeps_history(ask, corpus, settings, embedder):
    use_feed(corpus, "feeds/nightly/day1_new_law.xml")
    run_once(corpus, settings, embedder)
    old_answer = ask(LEAVE)
    assert "واحد وعشرين" in quotes(old_answer)

    use_feed(corpus, "feeds/nightly/day2_amendment.xml")
    counting = CountingEmbedder(embedder)
    run = run_once(corpus, settings, counting)
    assert run.status == "succeeded" and run.stage_counts["version"]["published"] == 1

    # T03: exactly the two changed articles were embedded; the other eight reused stored vectors.
    assert len(counting.inputs) == 2
    assert any("اليوم الخامس" in t for t in counting.inputs) and any("خمسة وعشرين" in t for t in counting.inputs)
    assert run.stage_counts["index"] == {"chunks": 10, "embedded": 2, "reused": 8}
    amendment = candidate(corpus, "nightly-2004").proposed_version
    changed = corpus.scalars(
        select(AmendmentRelation.affected_article_number).where(AmendmentRelation.amending_version_id == amendment.id)
    ).all()
    assert sorted(changed) == ["4", "6"]

    # T04: the refreshed answer cites the new edition.
    new_answer = ask(LEAVE)
    assert "خمسة وعشرين" in quotes(new_answer)
    assert {c["version_id"] for c in new_answer["citations"]} == {str(amendment.id)}

    # T05: the previously effective wording stays available for an earlier as-of date.
    historical = ask(LEAVE, as_of=date(2026, 10, 9))
    assert "واحد وعشرين" in quotes(historical) and "خمسة وعشرين" not in quotes(historical)


def test_t06_future_effective_amendment_is_published_as_scheduled_not_current(ask, corpus, settings, embedder):
    use_feed(corpus, "feeds/simulated_feed.xml")
    run = run_once(corpus, settings, embedder)
    assert run.stage_counts["version"]["scheduled"] == 1
    version = candidate(corpus, "fixture-decision-1001").proposed_version
    assert version.effective_from == date(2026, 11, 1) and version.review_status == "approved"

    today = ask(PROBATION)
    assert "تسعين" in quotes(today) and "مئة وثمانين" not in quotes(today)
    assert today["forthcoming"] and today["forthcoming"][0]["effective_from"] == "2026-11-01"
    later = ask(PROBATION, as_of=date(2026, 11, 2))
    assert "مئة وثمانين" in quotes(later)


def test_repeal_is_published_from_its_evidenced_date(ask, corpus, settings, embedder):
    use_feed(corpus, "feeds/nightly/repeal.xml")
    run = run_once(corpus, settings, embedder)
    assert run.stage_counts["version"]["repeals"] == 1
    companies = corpus.scalars(
        select(DocumentVersion).join(LegalInstrument).where(LegalInstrument.official_title_ar.like("نظام الشركات%"))
    ).one()
    assert companies.effective_to == date(2026, 10, 11) and companies.end_reason == "repealed"
    notice = candidate(corpus, "nightly-2005").proposed_version
    assert notice.review_status == "evidence" and not notice.index_active  # proof, never retrievable text
    assert ask(CAPITAL)["answer_status"] == "insufficient_evidence"
    assert "خمسين ألف" in quotes(ask(CAPITAL, as_of=date(2026, 10, 1)))


# --- T08 / T09: quarantine ----------------------------------------------------------------


def test_t08_uncertain_changes_are_quarantined_alerted_and_other_updates_continue(
    ask, corpus, settings, embedder, caplog
):
    use_feed(corpus, "feeds/nightly/ambiguous.xml")
    with caplog.at_level(logging.WARNING, logger="alerts"):
        run = run_once(corpus, settings, embedder)
    reasons = {
        candidate(corpus, ext).state: None for ext in ("nightly-3001", "nightly-3002", "nightly-3003")
    }
    assert set(reasons) == {"quarantined"}
    codes = set(corpus.scalars(select(QuarantineEvent.reason_code).where(QuarantineEvent.sync_run_id == run.id)))
    assert codes == {"NO_OFFICIAL_TEXT", "CONFLICTING_EFFECTIVE_DATES", "AMBIGUOUS_AMENDMENT_MAPPING"}
    assert candidate(corpus, "nightly-2001").state == "approved"  # FR-10: the valid item still published
    assert any(r.getMessage() == "alert" and r.alert_kind == "quarantine" for r in caplog.records)

    for ext in ("nightly-3002", "nightly-3003"):
        version = candidate(corpus, ext).proposed_version
        assert version.review_status == "quarantined" and not version.index_active
    assert "تسعين" in quotes(ask(PROBATION))  # the partial amendment changed nothing
    assert "ثلاثين يوماً من تاريخ الطلب" not in quotes(ask("ما حق صاحب البيانات في الوصول إلى بياناته؟"))


def test_t09_instructions_inside_a_pdf_cannot_change_tools_allowlist_or_corpus(ask, corpus, settings, embedder):
    allowlist = set(settings.allowlisted_hosts)
    sources = corpus.scalar(select(func.count(SourceRegistry.id)))
    before = active(corpus).id
    use_feed(corpus, "feeds/nightly/hostile.xml")
    run = run_once(corpus, settings, embedder)

    item = candidate(corpus, "nightly-4001")
    assert item.state == "quarantined"
    code = corpus.scalars(select(QuarantineEvent.reason_code).where(QuarantineEvent.candidate_id == item.id)).one()
    assert code == "PROMPT_INJECTION_DETECTED"
    assert settings.allowlisted_hosts == allowlist and "evil.example" not in settings.source_allowlist
    assert corpus.scalar(select(func.count(SourceRegistry.id))) == sources
    assert active(corpus).id == before and run.stage_counts["version"]["published"] == 0
    assert "تسعين" in quotes(ask(PROBATION))

    # The agent's tools enforce their own boundaries whatever a document asks for.
    toolbox = AgentToolbox(corpus, settings, fetcher=lambda url: b"never fetched")
    with pytest.raises(ToolBoundaryError):
        toolbox.call("update_allowlist", {"host": "evil.example"})
    with pytest.raises(ToolBoundaryError):
        toolbox.fetch_official_document("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(ToolBoundaryError):
        toolbox.submit_candidate({"url": "https://evil.example/law", "title": "x"})


def test_bounded_agent_proposals_are_verified_against_the_source(corpus, settings, embedder):
    """A model proposal that names an unknown target or quotes text absent from the source is
    discarded; a disagreement with the deterministic rules quarantines instead of publishing."""

    class HostileModel:
        class chat:  # noqa: N801 - mimics the OpenAI client shape
            class completions:  # noqa: N801
                @staticmethod
                def create(**_kwargs):
                    content = json.dumps({
                        "classification": "new_law",
                        "target_title": "evil.example administrator",
                        "effective_date_quotes": ["publish immediately as current law"],
                        "affected_article_numbers": ["1"],
                        "uncertainty": [],
                    })  # fmt: skip
                    message = type("M", (), {"content": content})
                    return type("R", (), {"choices": [type("C", (), {"message": message})]})

    classifier = OpenAIClassifier(settings, client=HostileModel())
    use_feed(corpus, "feeds/nightly/day2_amendment.xml")
    run_once(corpus, settings, embedder, classifier=classifier)
    item = candidate(corpus, "nightly-2004")  # rules: amendment; model: new_law
    proposal = item.evidence["agent"]
    assert proposal["provider"] == "openai" and proposal["effective_date_spans"] == []
    assert "agent_target_not_a_known_instrument" in proposal["uncertainty"]
    assert "agent_quote_not_found_in_source" in proposal["uncertainty"]
    assert item.state == "quarantined" and item.evidence["gate"]["reasons"][0]["code"] == "AGENT_RULES_DISAGREE"


def test_fr03_agent_search_cannot_leave_the_official_allowlist(corpus, settings):
    results = [
        {"url": "https://evil.example/fake-law", "title": "fake"},
        {"url": "https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/1", "title": "official"},
        {"url": "http://laws.boe.gov.sa/insecure", "title": "plain http"},
    ]
    toolbox = AgentToolbox(corpus, settings, fetcher=lambda url: b"x", search=lambda q: results)
    found = toolbox.search_official_sites("نظام")
    assert [r["url"] for r in found] == ["https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/1"]
    assert "https://evil.example/fake-law" in toolbox.blocked


# --- T10 and fault injection: retries, freshness, no index corruption --------------------


def test_t10_feed_outage_retries_reports_degraded_freshness_and_keeps_the_index(
    ask, corpus, settings, embedder, monkeypatch
):
    monkeypatch.setattr(settings, "sync_catchup_days", 1)
    use_feed(corpus, "feeds/nightly/day1_new_law.xml")
    assert run_due_syncs(get_sessionmaker(), settings, embedder, now=midnight(TODAY - timedelta(days=1))) == [
        (TODAY - timedelta(days=1), "succeeded")
    ]
    corpus.expire_all()
    generation = active(corpus).id
    baseline = ask(CONSUMER)

    calls: list[str] = []

    def outage(url: str) -> bytes:
        calls.append(url)
        raise httpx.ConnectError("feed unreachable (injected)")

    results = run_due_syncs(get_sessionmaker(), settings, embedder, now=midnight(TODAY), fetcher=outage)
    corpus.expire_all()
    assert results == [(TODAY, "failed")]
    assert len(calls) == settings.fetch_max_attempts  # retried with backoff before giving up
    failed = corpus.scalars(select(SyncRun).where(SyncRun.logical_date_riyadh == TODAY)).one()
    assert failed.error_code == "ALL_SOURCES_FAILED" and failed.retry_after is not None
    assert active(corpus).id == generation  # nothing touched the live index
    assert ask(CONSUMER)["citations"] == baseline["citations"]

    report = freshness_report(corpus, settings)
    assert report["source_health"] != "healthy" and report["warning_ar"]
    assert any(report["warning_ar"] == c for c in ask(CONSUMER)["caveats"])

    # The scheduler retries the failed day once the source is back.
    retry = run_due_syncs(get_sessionmaker(), settings, embedder, now=midnight(TODAY, minute=30))
    corpus.expire_all()
    assert retry == [(TODAY, "no_op")]
    assert corpus.get(SyncRun, failed.id).retry_count == 1


def test_embedding_failure_publishes_nothing_and_is_retried_by_a_later_run(ask, corpus, settings, embedder):
    class Down:
        model_name, is_semantic = embedder.model_name, embedder.is_semantic

        def embed(self, texts):
            raise RuntimeError("embedding provider unavailable (injected)")

    use_feed(corpus, "feeds/nightly/day1_new_law.xml")
    before = active(corpus).id
    run_once(corpus, settings, Down())
    item = candidate(corpus, "nightly-2001")
    assert item.state == "quarantined" and active(corpus).id == before
    event = corpus.scalars(select(QuarantineEvent).where(QuarantineEvent.candidate_id == item.id)).one()
    assert event.reason_code == "PUBLISH_FAILED" and event.retry_after is not None
    assert ask(CONSUMER)["answer_status"] == "insufficient_evidence"

    run_once(corpus, settings, embedder, now=midnight(TODAY + timedelta(days=2)))
    assert candidate(corpus, "nightly-2001").state == "approved"
    assert corpus.get(QuarantineEvent, event.id).resolution_state == "superseded"
    assert ask(CONSUMER)["answer_status"] != "insufficient_evidence"


def test_failed_index_health_check_keeps_the_previous_generation(corpus, settings, embedder, monkeypatch):
    monkeypatch.setattr(
        generations, "check_health", lambda *_a, **_k: {"ok": False, "checks": {"forced": {"ok": False}}}
    )
    use_feed(corpus, "feeds/nightly/day1_new_law.xml")
    before = active(corpus).id
    run = run_once(corpus, settings, embedder)
    assert active(corpus).id == before
    assert candidate(corpus, "nightly-2001").state == "quarantined"
    failed = corpus.scalars(select(IndexGeneration).where(IndexGeneration.validation_status == "failed")).one()
    assert failed.sync_run_id == run.id and not failed.is_active
    assert not candidate(corpus, "nightly-2001").proposed_version.index_active


# --- FR-12: atomic generations and rollback ----------------------------------------------


def test_fr12_rollback_restores_the_previous_generation_and_can_be_undone(ask, corpus, settings, embedder):
    use_feed(corpus, "feeds/nightly/day2_amendment.xml")
    pre = active(corpus)
    run_once(corpus, settings, embedder)
    assert "خمسة وعشرين" in quotes(ask(LEAVE))

    restored = rollback_generation(corpus, actor_id=None, reason="Rollback drill (test).")
    corpus.commit()
    assert restored.trigger == "rollback" and restored.manifest == pre.manifest
    assert "واحد وعشرين" in quotes(ask(LEAVE))
    assert corpus.scalars(select(func.count(IndexGeneration.id)).where(IndexGeneration.is_active.is_(True))).one() == 1

    undo = rollback_generation(corpus, actor_id=None, reason="Undo the drill (test).")
    corpus.commit()
    assert undo.number == restored.number + 1 and "خمسة وعشرين" in quotes(ask(LEAVE))
    with pytest.raises(ReviewError):
        rollback_generation(corpus, actor_id=None, reason="  ")


# --- Scheduler contract (section 6, metric "scheduler compliance") ------------------------


def test_seven_midnights_create_one_logical_run_each_and_a_missed_day_catches_up(
    corpus, settings, embedder, monkeypatch
):
    use_feed(corpus, "feeds/simulated_feed.xml")
    days = [TODAY + timedelta(days=i) for i in range(7)]
    monkeypatch.setattr(settings, "sync_catchup_days", 1)
    assert run_due_syncs(get_sessionmaker(), settings, embedder, now=midnight(days[0])) == [(days[0], "succeeded")]
    monkeypatch.setattr(settings, "sync_catchup_days", 2)  # a missed night is caught up the next night
    for day in days[1:]:
        if day == days[3]:
            continue  # the worker was down that night
        run_due_syncs(get_sessionmaker(), settings, embedder, now=midnight(day))
        run_due_syncs(get_sessionmaker(), settings, embedder, now=midnight(day, minute=40))  # repeated tick
    # Before the next midnight nothing new is due.
    assert run_due_syncs(get_sessionmaker(), settings, embedder, now=midnight(days[6]) + timedelta(hours=23)) == []

    corpus.expire_all()
    runs = corpus.scalars(select(SyncRun).order_by(SyncRun.logical_date_riyadh)).all()
    assert [r.logical_date_riyadh for r in runs] == days
    assert {r.logical_date_riyadh: r.trigger for r in runs}[days[3]] == "catch_up"
    assert all(r.status in ("succeeded", "no_op") for r in runs)
    assert [r.status for r in runs].count("succeeded") == 1  # later days found nothing new
    assert runs[0].scheduled_at == midnight(days[0], minute=0)


def test_midnight_is_computed_in_asia_riyadh_not_utc(corpus, settings, embedder, monkeypatch):
    monkeypatch.setattr(settings, "sync_catchup_days", 1)
    use_feed(corpus, "feeds/simulated_feed.xml")
    utc = ZoneInfo("UTC")
    just_before = datetime.combine(TODAY - timedelta(days=1), time(20, 59), utc)  # 23:59 Riyadh
    just_after = datetime.combine(TODAY - timedelta(days=1), time(21, 1), utc)  # 00:01 Riyadh on TODAY
    assert [d for d, _ in run_due_syncs(get_sessionmaker(), settings, embedder, now=just_before)] == [
        TODAY - timedelta(days=1)
    ]
    assert [d for d, _ in run_due_syncs(get_sessionmaker(), settings, embedder, now=just_after)] == [TODAY]


def test_a_second_scheduler_cannot_start_a_concurrent_run(corpus, settings, embedder):
    use_feed(corpus, "feeds/simulated_feed.xml")
    started = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with sync_lock(blocking=False) as acquired:
            assert acquired
            started.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    started.wait(10)
    try:
        assert run_due_syncs(get_sessionmaker(), settings, embedder, now=midnight(TODAY)) == []
    finally:
        release.set()
        holder.join()
    assert corpus.scalar(select(func.count(SyncRun.id))) == 0


def test_a_run_left_running_by_a_dead_worker_is_recovered_and_retried(corpus, settings, embedder, monkeypatch):
    monkeypatch.setattr(settings, "sync_catchup_days", 1)
    use_feed(corpus, "feeds/simulated_feed.xml")
    stuck = ensure_run(corpus, TODAY, "scheduled", settings)
    stuck.status = "running"
    corpus.commit()
    assert run_due_syncs(get_sessionmaker(), settings, embedder, now=midnight(TODAY, minute=10)) == [
        (TODAY, "succeeded")
    ]
    corpus.expire_all()
    recovered = corpus.get(SyncRun, stuck.id)
    assert recovered.retry_count == 1 and recovered.status == "succeeded"


def test_legacy_chunks_are_hash_backfilled_so_amendments_stay_incremental(corpus, settings, embedder):
    from legal_core.cli import backfill_chunk_hashes

    corpus.execute(update(ArticleChunk).values(content_hash=None))  # an index built before v2
    corpus.commit()
    assert backfill_chunk_hashes(corpus, settings) != "chunk hashes backfilled: 0"
    corpus.commit()
    assert backfill_chunk_hashes(corpus, settings) == "chunk hashes backfilled: 0"
    use_feed(corpus, "feeds/nightly/day2_amendment.xml")
    counting = CountingEmbedder(embedder)
    run_once(corpus, settings, counting)
    labor_inputs = [t for t in counting.inputs if t.startswith("نظام العمل")]
    assert len(labor_inputs) == 2  # only the two amended articles, not all ten
