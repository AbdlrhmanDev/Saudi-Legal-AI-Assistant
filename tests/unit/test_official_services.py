import legal_core.official_services as official_services
from legal_core.official_services import OfficialService, suggest_official_services


def test_labor_claim_question_suggests_official_statement_of_claim():
    services = suggest_official_services("أبغى أرفع دعوى عمالية، وين أقدمها إلكترونيًا؟")

    assert [service["id"] for service in services] == ["moj-statement-of-claim"]
    assert services[0]["official_url"] == "https://najiz.sa/applications/courts/lawsuits-requests"
    assert services[0]["legal_evidence"] is False
    assert services[0]["official_info_url"].startswith("https://www.moj.gov.sa/")
    assert "المحاكم" in services[0]["guidance_ar"]
    assert "النفاذ الوطني" in services[0]["guidance_ar"]


def test_enforcement_question_suggests_najiz_service():
    services = suggest_official_services("كيف أقدم طلب تنفيذ سند تنفيذي في ناجز؟")

    assert services[0]["id"] == "najiz-enforcement-request"
    assert "najiz.sa" in services[0]["official_url"]


def test_unrelated_legal_question_does_not_show_a_generic_service():
    assert suggest_official_services("كم مدة فترة التجربة في نظام العمل؟") == []


def test_personal_data_transfer_never_matches_real_estate_services(monkeypatch):
    remote = (
        OfficialService(
            id="property-data",
            title_ar="البيانات العقارية",
            description_ar="الاستعلام عن العقارات والصفقات.",
            guidance_ar="خدمة عقارية.",
            provider_ar="وزارة العدل",
            official_url="https://najiz.sa/property",
            official_info_url="https://www.moj.gov.sa/ar/eServices/Pages/ServiceDetailsNew.aspx?itemId=1",
            last_verified_at="2026-10-20",
            match_phrases=(),
        ),
    )
    monkeypatch.setattr(official_services, "_download_catalog", lambda _timeout: remote)
    official_services.clear_official_service_cache()

    assert suggest_official_services("هل يجوز نقل البيانات الشخصية خارج المملكة؟", live=True) == []
    assert suggest_official_services("أبي خدمة نقل البيانات الشخصية", live=True) == []
    official_services.clear_official_service_cache()


def test_enforcement_inquiry_is_distinct_from_submitting_a_request():
    services = suggest_official_services("كيف أتحقق من طلب تنفيذ برقم الطلب؟")

    assert services[0]["id"] == "najiz-enforcement-inquiry"


def test_heirs_inventory_query_matches_the_official_inquiry_service():
    services = suggest_official_services("ابغى استعلام عن وثيقة حصر الورثة")

    assert [service["id"] for service in services] == ["najiz-heirs-inventory-inquiry"]
    assert services[0]["official_url"] == "https://najiz.sa/applications/heirs-inventory/olddeed"
    assert "رقمنة" in services[0]["guidance_ar"]


def test_service_limit_is_enforced():
    assert suggest_official_services("أريد رفع دعوى مستعجلة وتقديم طلب تنفيذ", limit=1)
    assert suggest_official_services("أريد رفع دعوى مستعجلة وتقديم طلب تنفيذ", limit=0) == []


def test_live_catalog_search_finds_a_service_not_in_the_fallback_directory(monkeypatch):
    remote = OfficialService(
        id="moj-service-900",
        title_ar="توثيق وصية",
        description_ar="خدمة إلكترونية لتوثيق الوصية لدى وزارة العدل.",
        guidance_ar="بحسب كتالوج وزارة العدل الرسمي، تتيح الخدمة توثيق الوصية.",
        provider_ar="وزارة العدل — ناجز",
        official_url="https://najiz.sa/applications/social-cases/will",
        official_info_url="https://www.moj.gov.sa/ar/eServices/Pages/ServiceDetailsNew.aspx?itemId=900",
        last_verified_at="2026-10-20",
        match_phrases=(),
    )
    monkeypatch.setattr(official_services, "_download_catalog", lambda _timeout: (remote,))
    official_services.clear_official_service_cache()

    results = suggest_official_services("أبغى خدمة توثيق وصية", live=True)

    assert [item["id"] for item in results] == ["moj-service-900"]
    assert results[0]["legal_evidence"] is False
    official_services.clear_official_service_cache()


def test_remote_catalog_item_is_cleaned_and_untrusted_action_hosts_are_rejected():
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


def test_ordinary_legal_question_does_not_trigger_remote_catalog(monkeypatch):
    monkeypatch.setattr(
        official_services,
        "_download_catalog",
        lambda _timeout: (_ for _ in ()).throw(AssertionError("catalog should not be fetched")),
    )
    official_services.clear_official_service_cache()

    assert suggest_official_services("كم مدة فترة التجربة في نظام العمل؟", live=True) == []
