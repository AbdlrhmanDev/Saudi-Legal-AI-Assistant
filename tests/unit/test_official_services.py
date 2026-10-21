import legal_core.official_services as official_services
from legal_core.official_services import OfficialService, suggest_official_services


def _service(service_id: str, title: str, description: str = "خدمة إلكترونية رسمية") -> OfficialService:
    return OfficialService(
        id=service_id,
        title_ar=title,
        description_ar=description,
        guidance_ar=f"بحسب المصدر الرسمي، تتيح الخدمة {title}.",
        provider_ar="وزارة العدل — ناجز",
        official_url=f"https://najiz.sa/applications/{service_id}",
        official_info_url=f"https://www.moj.gov.sa/ar/eServices/Pages/ServiceDetailsNew.aspx?itemId={service_id}",
        last_verified_at="2026-10-21",
    )


def test_labor_claim_question_searches_live_directory(monkeypatch):
    monkeypatch.setattr(
        official_services,
        "_download_catalog",
        lambda _timeout: (_service("101", "صحيفة الدعوى", "رفع دعوى عمالية أمام المحاكم"),),
    )

    services = suggest_official_services("أبغى أرفع صحيفة دعوى عمالية إلكترونيًا", live=True)

    assert [service["title_ar"] for service in services] == ["صحيفة الدعوى"]
    assert services[0]["official_url"] == "https://najiz.sa/applications/101"
    assert services[0]["legal_evidence"] is False


def test_colloquial_marriage_request_routes_to_live_directory(monkeypatch):
    monkeypatch.setattr(
        official_services,
        "_download_catalog",
        lambda _timeout: (_service("300", "إنشاء عقد زواج", "تقديم عقد زواج وتوثيقه"),),
    )

    results = suggest_official_services("أبغى إنشاء عقد زواج", live=True)

    assert [item["title_ar"] for item in results] == ["إنشاء عقد زواج"]


def test_single_distinctive_service_word_can_match(monkeypatch):
    monkeypatch.setattr(
        official_services,
        "_download_catalog",
        lambda _timeout: (_service("400", "إصدار وكالة", "إنشاء وكالة إلكترونية"),),
    )

    results = suggest_official_services("أبي وكالة", live=True)

    assert [item["title_ar"] for item in results] == ["إصدار وكالة"]


def test_generic_data_word_does_not_match_real_estate_service(monkeypatch):
    monkeypatch.setattr(
        official_services,
        "_download_catalog",
        lambda _timeout: (_service("401", "البيانات العقارية", "الاستعلام عن العقارات"),),
    )

    assert suggest_official_services("أبي خدمة نقل البيانات الشخصية", live=True) == []


def test_each_service_search_reads_the_source_again_and_never_uses_a_cache(monkeypatch):
    calls = 0

    def download(_timeout):
        nonlocal calls
        calls += 1
        return (_service(str(calls), "توثيق وصية", f"الإصدار الرسمي {calls}"),)

    monkeypatch.setattr(official_services, "_download_catalog", download)

    first = suggest_official_services("أبغى خدمة توثيق وصية", live=True)
    second = suggest_official_services("أبغى خدمة توثيق وصية", live=True)

    assert calls == 2
    assert first[0]["id"] == "1"
    assert second[0]["id"] == "2"


def test_ordinary_legal_question_does_not_call_service_directory(monkeypatch):
    monkeypatch.setattr(
        official_services,
        "_download_catalog",
        lambda _timeout: (_ for _ in ()).throw(AssertionError("directory should not be fetched")),
    )

    assert suggest_official_services("كم مدة فترة التجربة في نظام العمل؟", live=True) == []


def test_disabled_live_search_does_not_fetch(monkeypatch):
    monkeypatch.setattr(
        official_services,
        "_download_catalog",
        lambda _timeout: (_ for _ in ()).throw(AssertionError("directory should not be fetched")),
    )

    assert suggest_official_services("أبغى خدمة توثيق وصية") == []


def test_service_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(
        official_services,
        "_download_catalog",
        lambda _timeout: (
            _service("1", "تقديم طلب تنفيذ"),
            _service("2", "التحقق من طلب تنفيذ"),
        ),
    )

    assert len(suggest_official_services("أبغى تقديم طلب تنفيذ", live=True, limit=1)) == 1
    assert suggest_official_services("أبغى تقديم طلب تنفيذ", live=True, limit=0) == []


def test_download_directory_accepts_sharepoint_verbose_json(monkeypatch):
    class Response:
        content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "d": {
                    "results": [
                        {
                            "Id": 300,
                            "Title": "إنشاء عقد زواج",
                            "Description": "خدمة إلكترونية لتقديم عقد زواج وتوثيقه.",
                            "ServiceURL": "https://najiz.sa/applications/social-cases/marriage-contract",
                            "Steps": "<ol><li>تسجيل الدخول</li></ol>",
                            "Requirements": "<ul><li>النفاذ الوطني</li></ul>",
                        }
                    ]
                }
            }

    class Client:
        def __init__(self, **kwargs):
            assert "cached" not in kwargs["headers"]["User-Agent"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url, *, params):
            assert params["$format"] == "json"
            return Response()

    monkeypatch.setattr(official_services.httpx, "Client", Client)

    services = official_services._download_catalog(5)

    assert [service.title_ar for service in services] == ["إنشاء عقد زواج"]


def test_remote_directory_item_is_cleaned_and_untrusted_action_hosts_are_rejected():
    item = {
        "Id": 901,
        "Title": "<b>إصدار وكالة</b>",
        "Description": "<p>خدمة رسمية لإصدار وكالة.</p>",
        "ServiceURL": "https://lookalike.example/steal",
        "Steps": "<ol><li>تسجيل الدخول</li><li>إدخال البيانات</li></ol>",
        "Requirements": "<ul><li>النفاذ الوطني</li></ul>",
    }

    service = official_services._service_from_item(item, "2026-10-20")

    assert service is not None
    assert service.title_ar == "إصدار وكالة"
    assert service.official_url == service.official_info_url
    assert "تسجيل الدخول؛ إدخال البيانات" in service.guidance_ar


def test_live_directory_failure_returns_no_stale_results(monkeypatch):
    monkeypatch.setattr(
        official_services,
        "_download_catalog",
        lambda _timeout: (_ for _ in ()).throw(TimeoutError("source unavailable")),
    )

    assert suggest_official_services("أبغى خدمة إصدار وكالة", live=True) == []
