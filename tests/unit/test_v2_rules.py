"""Deterministic v2 rules: effective-date evidence, classification of drafts/repeals, and the
source-fetching boundary (SSRF, redirects, MIME, size, robots.txt). No database needed."""

from datetime import date

import httpx
import pytest

from legal_core.config import Settings
from legal_core.legal_dates import extract_effective_dates, resolve_effective_date
from legal_core.monitor import (
    MonitorError,
    SourcePolicyError,
    assert_public_host,
    check_official_url,
    classify,
    make_http_fetcher,
)

PUBLISHED = date(2026, 10, 5)


def _values(text: str, published=PUBLISHED) -> list[tuple[date, str]]:
    return [(e.value, e.kind) for e in extract_effective_dates(text, origin="notice", published_at=published).evidence]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ويعمل به اعتباراً من 2026-11-01.", [(date(2026, 11, 1), "explicit")]),
        ("يسري هذا النظام اعتباراً من 01/12/2026م", [(date(2026, 12, 1), "explicit")]),
        ("يعمل بهذا النظام من تاريخ نشره في الجريدة الرسمية", [(PUBLISHED, "from_publication")]),
        ("ويُعمل بهذا القرار بعد تسعين يوماً من تاريخ نشره", [(date(2027, 1, 3), "publication_offset")]),
        ("ويعمل به بعد 30 يوما من تاريخ نشره", [(date(2026, 11, 4), "publication_offset")]),
        ("تسري أحكام هذا النظام على كل عقد عمل", []),  # scope clause, not a commencement date
        ("يعمل العامل ثماني ساعات اعتباراً من الساعة 2026", []),
    ],
)
def test_effective_dates_are_read_only_from_commencement_clauses(text, expected):
    assert _values(text) == expected


def test_publication_date_is_never_assumed():
    found = extract_effective_dates("يعمل بهذا النظام من تاريخ نشره", origin="document", published_at=None)
    assert found.evidence == [] and found.needs_publication_date
    resolved = resolve_effective_date(
        notice_text="", document_text="يعمل بهذا النظام من تاريخ نشره", published_at=None, consolidated_amendment=False
    )
    assert resolved.value is None and resolved.reason_code == "PUBLICATION_DATE_MISSING"


def test_conflicting_hijri_and_missing_dates_are_reasons_not_guesses():
    conflict = resolve_effective_date(
        notice_text="ويعمل به اعتباراً من 2026-10-01", document_text="ويعمل بهذا التعديل اعتباراً من 2026-12-01",
        published_at=PUBLISHED, consolidated_amendment=True,
    )  # fmt: skip
    assert conflict.value is None and conflict.reason_code == "CONFLICTING_EFFECTIVE_DATES"
    hijri = resolve_effective_date(
        notice_text="ويعمل به اعتباراً من 1448/05/10هـ", document_text="", published_at=PUBLISHED,
        consolidated_amendment=False,
    )  # fmt: skip
    assert hijri.value is None and hijri.reason_code == "HIJRI_DATE_UNVERIFIED"
    missing = resolve_effective_date(notice_text="قرار", document_text="نص", published_at=PUBLISHED,
                                     consolidated_amendment=False)  # fmt: skip
    assert missing.reason_code == "EFFECTIVE_DATE_MISSING"


def test_a_consolidated_texts_own_commencement_clause_is_not_the_amendment_date():
    resolved = resolve_effective_date(
        notice_text="قرار بتعديل المادة الخامسة، ويعمل به اعتباراً من 2026-11-01",
        document_text="المادة العاشرة: يعمل بهذا النظام من تاريخ نشره.",
        published_at=PUBLISHED,
        consolidated_amendment=True,
    )
    assert resolved.value == date(2026, 11, 1) and resolved.reason_code is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("مشروع تعديل نظام العمل لطلب المرئيات", "draft"),
        ("مسودة اللائحة التنفيذية لنظام التجارة الإلكترونية", "draft"),
        ("إلغاء نظام الشركات", "repeal"),
        ("تعديل المادة الخامسة وإلغاء المادة السابعة من نظام العمل", "amendment"),
        ("نظام التجارة الإلكترونية", "new_law"),
    ],
)
def test_drafts_and_repeals_are_classified_before_generic_rules(title, expected):
    assert classify(title) == expected


# --- fetching boundary ---------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(fetch_min_interval_seconds=0, fetch_max_attempts=1)


def test_private_and_metadata_addresses_are_refused():
    with pytest.raises(SourcePolicyError):
        assert_public_host("localhost")


@pytest.mark.parametrize(
    "url",
    [
        "http://laws.boe.gov.sa/x",  # not https
        "https://laws.boe.gov.sa:8443/x",  # explicit port
        "https://user:pw@laws.boe.gov.sa/x",  # credentials
        "https://evil.example/x",  # not allowlisted
        "https://169.254.169.254/latest/meta-data/",
    ],
)
def test_only_plain_https_urls_on_allowlisted_hosts_are_accepted(url, settings):
    with pytest.raises(SourcePolicyError):
        check_official_url(url, settings)


def _fetcher(settings, routes: dict[str, httpx.Response]):
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    return make_http_fetcher(settings, resolve=lambda host: None, transport=httpx.MockTransport(handler))


def test_redirects_are_revalidated_on_every_hop(settings):
    fetch = _fetcher(
        settings,
        {
            "https://laws.boe.gov.sa/a": httpx.Response(302, headers={"location": "https://laws.boe.gov.sa/b"}),
            "https://laws.boe.gov.sa/b": httpx.Response(200, text="ok", headers={"content-type": "text/html"}),
            "https://laws.boe.gov.sa/out": httpx.Response(302, headers={"location": "https://evil.example/x"}),
        },
    )
    assert fetch("https://laws.boe.gov.sa/a") == b"ok"
    with pytest.raises(SourcePolicyError):
        fetch("https://laws.boe.gov.sa/out")


def test_unsupported_types_oversized_bodies_and_robots_disallow_are_refused(settings):
    settings.max_upload_bytes = 10
    big = b"x" * (6 * 1024 * 1024)
    fetch = _fetcher(
        settings,
        {
            "https://laws.boe.gov.sa/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /private\n"),
            "https://laws.boe.gov.sa/image": httpx.Response(200, content=b"x", headers={"content-type": "image/png"}),
            "https://laws.boe.gov.sa/big": httpx.Response(200, content=big, headers={"content-type": "text/html"}),
            "https://laws.boe.gov.sa/private/doc": httpx.Response(200, text="secret", headers={"content-type": "text/html"}),
        },
    )
    with pytest.raises(SourcePolicyError):
        fetch("https://laws.boe.gov.sa/image")
    with pytest.raises(MonitorError):
        fetch("https://laws.boe.gov.sa/big")
    with pytest.raises(SourcePolicyError, match="robots"):
        fetch("https://laws.boe.gov.sa/private/doc")
