"""Official source catalog and connector behaviour without a database or the network."""

import json
import re
from urllib.parse import urlsplit

import httpx
import pytest

from legal_core.config import Settings
from legal_core.connectors import (
    ConnectorError,
    FetcherHttp,
    canonical_document_url,
    classify_failure,
    discover,
    extract_listing_links,
    html_to_text,
    page_fingerprint,
)
from legal_core.models import SourceRegistry
from legal_core.monitor import MonitorError, OfficialHttpClient, RobotsDisallowed, SourcePolicyError
from legal_core.source_catalog import (
    BOE_LAW_PATTERN,
    BY_KEY,
    CATALOG_HOSTS,
    CONNECTOR_TYPES,
    DOCUMENT_KINDS,
    SOURCES,
    crawl_policy,
    integration_status,
)

# The approved source list, exactly as specified (entry number -> URL).
SPECIFIED = {
    1: "https://www.boe.gov.sa/",
    2: "https://www.uqn.gov.sa/",
    3: "https://ncar.gov.sa/",
    4: "https://laws.moj.gov.sa/",
    5: "https://www.bog.gov.sa/",
    6: "https://istitlaa.ncc.gov.sa/",
    7: "https://my.gov.sa/",
    8: "https://mc.gov.sa/ar/Regulations/pages/default.aspx",
    9: "https://www.hrsd.gov.sa/knowledge-centre/decisions-and-regulations/regulation-and-procedures",
    10: "https://beta.gosi.gov.sa/laws-regualtion",
    11: "https://misa.gov.sa/ar/activities/laws/",
    12: "https://www.zatca.gov.sa/ar/RulesRegulations/Pages/systems.aspx",
    13: "https://rulebook.sama.gov.sa/ar",
    14: "https://cma.gov.sa/RulesRegulations/Pages/default.aspx",
    15: "https://www.ia.gov.sa/ar/Regulations",
    16: "https://www.mof.gov.sa/docslibrary/RegulationsInstructions/Pages/default.aspx",
    17: "https://www.gac.gov.sa/",
    18: "https://rega.gov.sa/الأنظمة-والقرارات/الأنظمة-واللوائح-والأدلة/",
    19: "https://momah.gov.sa/ar/regulations",
    20: "https://www.moh.gov.sa/Ministry/Rules/Pages/default.aspx",
    21: "https://www.sfda.gov.sa/ar/regulations",
    22: "https://scfhs.org.sa/ar/ClassGuide",
    23: "https://www.tga.gov.sa/",
    24: "https://sdaia.gov.sa/en/SDAIA/about/Pages/RegulationsAndPolicies.aspx",
    25: "https://dgp.sdaia.gov.sa/wps/portal/pdp/knowledgecenter/",
    26: "https://nca.gov.sa/ar/regulatory-documents/",
    27: "https://dga.gov.sa/",
    28: "https://www.cst.gov.sa/",
    29: "https://saip.gov.sa/ar/resources/lows-and-regulations/systems-and-regulations",
    30: "https://sdaia.gov.sa/ar/SDAIA/about/Pages/ArchivesRegulationsAndPolicies.aspx",
    31: "https://laws.moj.gov.sa/ar/JudicialDecisionsList/0/",
    32: "https://www.bog.gov.sa/",
    33: "https://gstc.gov.sa/",
    34: "https://sjp.moj.gov.sa/",
    35: "https://www.spa.gov.sa/",
    36: "https://www.uqn.gov.sa/decisions/council-of-ministers-decisions",
    37: "https://my.gov.sa/saml.authn/agencies",
    38: "https://my.gov.sa/saml.authn/rules",
    39: "https://ncc.gov.sa/",
}


def _source(key: str, **overrides) -> SourceRegistry:
    entry = BY_KEY[key]
    values = dict(
        name=entry.name, authority=entry.authority, feed_url=entry.endpoint, source_key=entry.key,
        allowlisted_host=urlsplit(entry.endpoint).hostname, connector_type=entry.connector,
        connector_config={"link_patterns": list(entry.link_patterns)}, document_kind=entry.document_kind,
        crawl_policy=crawl_policy(entry), enabled=True,
    )  # fmt: skip
    values.update(overrides)
    return SourceRegistry(**values)


# --- catalog ------------------------------------------------------------------------------------


def test_every_entry_of_the_approved_list_is_registered_exactly_once():
    numbers = sorted(n for source in SOURCES for n in source.numbers)
    assert numbers == list(range(1, 40))
    by_number = {n: s for s in SOURCES for n in s.numbers}
    for number, url in SPECIFIED.items():
        assert by_number[number].url == url, number


def test_catalog_rows_are_consistent():
    assert len({s.key for s in SOURCES}) == len(SOURCES)
    assert len({s.name for s in SOURCES}) == len(SOURCES)
    for source in SOURCES:
        assert source.connector in CONNECTOR_TYPES and source.document_kind in DOCUMENT_KINDS
        assert source.category in "ABCDEF" and source.access_notes
        endpoint = urlsplit(source.endpoint)
        assert endpoint.scheme == "https" and endpoint.hostname in source.hosts, source.key
        if source.consolidated_into:
            target = BY_KEY[source.consolidated_into]
            assert not target.consolidated_into and integration_status(source) == "consolidated"
        if source.connector == "html_listing":
            assert source.link_patterns, source.key
            for pattern in source.link_patterns:
                re.compile(pattern)


def test_sources_are_classified_by_legal_authority():
    assert BY_KEY["istitlaa"].document_kind == "draft"  # consultation drafts are never law
    assert BY_KEY["spa"].document_kind == "news" and BY_KEY["spa"].tier == "supporting"
    assert BY_KEY["moj_judgments"].document_kind == "judicial" and BY_KEY["bog"].document_kind == "judicial"
    assert BY_KEY["uqn_rss"].tier == "primary" and BY_KEY["boe"].tier == "primary"
    # robots.txt of the SAMA rulebook disallows every automated agent: no connector, never enabled.
    assert BY_KEY["sama"].connector == "none" and not BY_KEY["sama"].enabled
    assert crawl_policy(BY_KEY["sama"]) == "manual"


def test_catalog_hosts_extend_the_fetch_allowlist_but_nothing_else_does():
    settings = Settings(source_allowlist="www.uqn.gov.sa")
    assert CATALOG_HOSTS <= settings.allowlisted_hosts
    assert "evil.example" not in settings.allowlisted_hosts
    assert Settings(source_allowlist="www.uqn.gov.sa", include_catalog_hosts=False).allowlisted_hosts == {"www.uqn.gov.sa"}


# --- link extraction and identity -----------------------------------------------------------------

BOE_FOLDER = """<html><body><nav><a href="/BoeLaws/Laws/Search">بحث</a><a href="http://www.boe.gov.sa">الهيئة</a></nav>
<ul>
 <li><a href="/BoeLaws/Laws/LawDetails/08381293-6388-48e2-8ad2-a9a700f2aa94/1">نظام العمل</a></li>
 <li><a href="https://laws.boe.gov.sa/boelaws/laws/lawdetails/08381293-6388-48e2-8ad2-a9a700f2aa94/1">نظام العمل</a></li>
 <li><a href="/BoeLaws/Laws/LawDetails/16b97fcb-4833-4f66-8531-a9a700f161b6/1#section">النظام الأساسي للحكم</a></li>
</ul></body></html>""".encode()

IA_LISTING = """<html><body>
<div class="card"><h3>قواعد التأمين الشامل للمركبات</h3><a href="/Regulations/قواعد التامين الشامل للمركبات.pdf">تنزيل</a></div>
<div><a href="/Regulations/قواعد%20التقنية%20المالية%20التأمينية.pdf">تنزيل</a></div>
<a href="https://www.ia.gov.sa/ar/privacy-policy">سياسة الخصوصية</a>
</body></html>""".encode()


def test_listing_links_follow_only_document_rules_and_fold_duplicate_spellings():
    links = extract_listing_links(BOE_FOLDER, "https://laws.boe.gov.sa/BoeLaws/Laws/Folders/1", [BOE_LAW_PATTERN])
    assert links == [
        ("https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/08381293-6388-48e2-8ad2-a9a700f2aa94/1", "نظام العمل"),
        ("https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/16b97fcb-4833-4f66-8531-a9a700f161b6/1", "النظام الأساسي للحكم"),
    ]


def test_download_buttons_take_their_title_from_the_card_or_file_name():
    patterns = list(BY_KEY["ia"].link_patterns)
    links = dict(extract_listing_links(IA_LISTING, "https://www.ia.gov.sa/ar/Regulations", patterns))
    titles = sorted(links.values())
    assert titles == ["قواعد التأمين الشامل للمركبات", "قواعد التقنية المالية التأمينية"]
    assert all(url.startswith("https://www.ia.gov.sa/regulations/") or "/Regulations/" in url for url in links)


def test_canonical_urls_drop_tracking_and_fold_boe_spellings():
    assert canonical_document_url("https://WWW.UQN.gov.sa/decisions-and-regulations/1?utm_source=rss#x") == (
        "https://www.uqn.gov.sa/decisions-and-regulations/1"
    )
    assert canonical_document_url("https://laws.boe.gov.sa/boelaws/laws/lawdetails/16B97FCB-4833-4f66-8531-a9a700f161b6/1") == (
        "https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/16b97fcb-4833-4f66-8531-a9a700f161b6/1"
    )


def test_page_fingerprint_ignores_markup_and_scripts_but_not_visible_text():
    base = b"<html><body><h1>\xd8\xa7\xd9\x84\xd8\xa3\xd9\x86\xd8\xb8\xd9\x85\xd8\xa9</h1><script>var t=1</script></body></html>"
    churn = base.replace(b"var t=1", b"var t=2").replace(b"<h1>", b'<h1 class="x">')
    changed = base.replace(b"</h1>", b" 2026</h1>")
    assert page_fingerprint(base) == page_fingerprint(churn) != page_fingerprint(changed)


def test_html_to_text_keeps_paragraphs():
    assert html_to_text("<p>الوقائع: نزاع</p>\r\n<p>الأسباب: ثابت</p>") == "الوقائع: نزاع\nالأسباب: ثابت"


# --- connectors with fake transport -----------------------------------------------------------------


def test_listing_discovery_attributes_bureau_of_experts_pages_to_the_bureau():
    misa = _source("misa")
    page = b'<a href="https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/08381293-6388-48e2-8ad2-a9a700f2aa94/1">x law</a>'
    discovery = discover(misa, FetcherHttp(lambda _url: page), Settings())
    assert [i.owner_key for i in discovery.items] == ["boe"]
    assert discovery.fingerprint


def test_empty_listing_is_reported_not_hidden():
    discovery = discover(_source("momah"), FetcherHttp(lambda _url: b"<html><body>JS app</body></html>"), Settings())
    assert discovery.items == [] and "no server-rendered links" in discovery.note


def test_moj_judgments_api_discovery_parses_the_official_payload():
    payload = {
        "statusCode": 200, "success": True,
        "model": {"judgementsCollection": [{"id": "abc", "judgementNumber": "4530838530", "judgementDate": "1445-09-04",
                                            "city": "الرياض", "courtName": "المحكمة التجارية", "courtType": 1,
                                            "caseNumber": "1", "isAppeal": False}], "totalCount": 54196},
    }  # fmt: skip
    seen = {}

    def fake(url, body=None):
        seen["url"], seen["body"] = url, body
        return json.dumps(payload, ensure_ascii=False).encode()

    discovery = discover(_source("moj_judgments"), FetcherHttp(fake), Settings(connector_max_judgments_per_run=5))
    assert seen["url"].endswith("/Judgements/judgements-list") and seen["body"] == {"pageNumber": 1, "pageSize": 5}
    assert [(r.external_id, r.court_name, r.hijri_date) for r in discovery.judicial] == [
        ("abc", "المحكمة التجارية", "1445-09-04")
    ]
    assert discovery.note == "54196 judgments published"


def test_a_source_without_a_permitted_access_path_is_never_harvested():
    with pytest.raises(ConnectorError):
        discover(_source("sama", connector_type="none"), FetcherHttp(lambda _url: b""), Settings())


def _client(handler) -> OfficialHttpClient:
    return OfficialHttpClient(
        Settings(fetch_min_interval_seconds=0), resolve=lambda _host: None, transport=httpx.MockTransport(handler)
    )


def test_robots_txt_disallow_is_enforced_and_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        raise AssertionError("a disallowed page must not be requested")

    client = _client(handler)
    with pytest.raises(RobotsDisallowed) as caught:
        client.get("https://rulebook.sama.gov.sa/ar")
    assert classify_failure(caught.value, None)[0] == "robots_disallowed"


def test_json_api_access_is_checked_and_an_html_rejection_counts_as_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.method == "POST":
            assert request.headers["languageCode"] == "ar" and json.loads(request.content) == {"pageNumber": 1}
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, html="<html>Request Rejected</html>")

    client = _client(handler)
    assert json.loads(client.post_json("https://laws-gateway.moj.gov.sa/apis/x", {"pageNumber": 1})) == {"success": True}
    with pytest.raises(SourcePolicyError) as caught:
        client.get_json("https://laws-gateway.moj.gov.sa/apis/y")
    assert classify_failure(caught.value, client.last_status)[0] == "blocked"
    with pytest.raises(SourcePolicyError):
        client.post_json("https://evil.example/api", {})


@pytest.mark.parametrize(
    ("status", "health"), [(403, "blocked"), (401, "blocked"), (429, "blocked"), (503, "unavailable"), (404, "unavailable")]
)
def test_http_failures_map_to_honest_health_states(status, health):
    request = httpx.Request("GET", "https://my.gov.sa/ar")
    error = httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))
    wrapped = MonitorError("source unavailable after retries")
    wrapped.__cause__ = error
    assert classify_failure(wrapped, None)[0] == health


def test_timeouts_and_connection_failures_are_unavailable():
    assert classify_failure(httpx.ConnectTimeout("t"), None)[0] == "unavailable"
    assert classify_failure(httpx.ConnectError("refused"), None)[0] == "unavailable"


def test_each_source_has_its_own_fetch_budget_under_a_global_ceiling():
    from legal_core.monitor import acquisition_budget

    boe = _source("boe", connector_config={"max_items": BY_KEY["boe"].max_items})
    gazette = _source("uqn_rss", connector_config={"max_items": BY_KEY["uqn_rss"].max_items})
    assert acquisition_budget(boe, Settings()) == 10 and acquisition_budget(gazette, Settings()) == 50
    assert acquisition_budget(gazette, Settings(connector_max_acquisitions_per_source=5)) == 5
    assert acquisition_budget(_source("ia", connector_config={}), Settings()) == 50
