"""Search the official Ministry of Justice service catalog without mixing it into legal RAG.

The Ministry publishes its Najiz/e-service catalog through a public SharePoint REST list.  A
short-lived in-process snapshot lets normal queries search the whole catalog without relying on
Google or making the legal answer depend on a remote page.  The small reviewed directory below is
an outage fallback for common demonstrations.  Service cards are never legal evidence.
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from legal_core.normalize import lexical_tokens, normalize_ar

log = logging.getLogger(__name__)

_OFFICIAL_HOSTS = frozenset({"moj.gov.sa", "www.moj.gov.sa", "najiz.sa", "www.najiz.sa", "new.najiz.sa"})
_CATALOG_URL = "https://www.moj.gov.sa/ar/_api/web/lists/getbytitle('eservicesListNew')/items"
_CATALOG_INFO_URL = "https://www.moj.gov.sa/ar/eServices/Pages/ServiceDetailsNew.aspx"
_CATALOG_MAX_BYTES = 4 * 1024 * 1024
_CATALOG_MAX_ITEMS = 500
_SERVICE_INTENT = frozenset(
    {
        "خدمه", "ناجز", "استعلام", "استعلم", "تحقق", "اتحقق", "اصدار", "استخراج", "توثيق", "رقمنه",
        "طلب", "تقديم", "اقدم", "ارفع", "احجز", "وين", "ابغي", "ابي", "تسجيل", "تحديث", "اضافه",
        "متابعه", "انشاء", "اسوي", "سوي", "الكتروني", "الكترونيه",
    }
)
_catalog_lock = threading.Lock()
_catalog_cache: tuple[float, tuple["OfficialService", ...]] | None = None


@dataclass(frozen=True)
class OfficialService:
    id: str
    title_ar: str
    description_ar: str
    guidance_ar: str
    provider_ar: str
    official_url: str
    official_info_url: str
    last_verified_at: str
    match_phrases: tuple[str, ...]

    def public_dict(self) -> dict[str, str | bool]:
        return {
            "id": self.id,
            "title_ar": self.title_ar,
            "description_ar": self.description_ar,
            "guidance_ar": self.guidance_ar,
            "provider_ar": self.provider_ar,
            "official_url": self.official_url,
            "official_info_url": self.official_info_url,
            "last_verified_at": self.last_verified_at,
            "legal_evidence": False,
        }


_SERVICES = (
    OfficialService(
        id="moj-statement-of-claim",
        title_ar="صحيفة الدعوى",
        description_ar="تقديم دعوى إلكترونيًا أمام المحاكم عبر خدمات وزارة العدل ومنصة ناجز.",
        guidance_ar=(
            "بحسب صفحة وزارة العدل الرسمية، تتيح خدمة «صحيفة دعوى» رفع دعوى أمام المحاكم العامة "
            "والجزائية والحقوقية والتجارية والأحوال الشخصية والعمالية. للتقديم: سجّل الدخول إلى ناجز "
            "عبر النفاذ الوطني، ثم اختر الخدمات الإلكترونية، وباقة القضاء، وخدمة صحيفة دعوى، ثم تقديم "
            "طلب جديد. أدخل تصنيف الدعوى وبياناتها وأطرافها، وأرفق المستندات المطلوبة ثم قدّم الطلب. "
            "تشمل المتطلبات هوية مقبولة، والعنوان الوطني، وبيانات المدعي والمدعى عليه، وقد تُطلب مستندات داعمة."
        ),
        provider_ar="وزارة العدل",
        official_url="https://najiz.sa/applications/courts/lawsuits-requests",
        official_info_url=(
            "https://www.moj.gov.sa/ar/eServices/Pages/"
            "928b8f02-19a7-482b-ba0e-29a151d9a900.aspx"
        ),
        last_verified_at="2026-10-19",
        match_phrases=(
            "صحيفه دعوي",
            "رفع دعوي",
            "ارفع دعوي",
            "تقديم دعوي",
            "دعوي عماليه",
            "اقامه دعوي",
            "ارفع قضيه",
        ),
    ),
    OfficialService(
        id="najiz-enforcement-request",
        title_ar="تقديم طلب تنفيذ",
        description_ar="تقديم السند التنفيذي ومتابعة طلبات التنفيذ إلكترونيًا عبر منصة ناجز.",
        guidance_ar=(
            "بحسب صفحة وزارة العدل الرسمية، تتيح خدمة «تقديم طلب تنفيذ» تقديم السند التنفيذي للطلبات "
            "المالية أو التنفيذ المباشر أو الأحوال الشخصية. سجّل الدخول إلى ناجز عبر النفاذ الوطني، "
            "ثم اختر الخدمات الإلكترونية، وباقة التنفيذ، وتقديم طلب تنفيذ، وحدد نوع الطلب وأدخل بيانات "
            "الأطراف ومضمون الطلب وأرفق نسخة السند. من المتطلبات ملف شخصي في ناجز وآيبان فعال والمستندات "
            "المطلوبة بحسب نوع السند."
        ),
        provider_ar="وزارة العدل — ناجز",
        official_url="https://najiz.sa/applications/iExecution#/Request/",
        official_info_url=(
            "https://www.moj.gov.sa/ar/eServices/Pages/"
            "1c719954-3463-4754-85e1-1b319b937b86.aspx"
        ),
        last_verified_at="2026-10-19",
        match_phrases=(
            "طلب تنفيذ",
            "تنفيذ حكم",
            "تنفيذ سند",
            "سند تنفيذي",
            "محكمه التنفيذ",
        ),
    ),
    OfficialService(
        id="moj-urgent-lawsuits",
        title_ar="الدعاوى المستعجلة",
        description_ar="صفحة وزارة العدل الرسمية الخاصة بخدمة الدعاوى المستعجلة.",
        guidance_ar=(
            "هذه خدمة «الدعاوى المستعجلة» المنشورة في بوابة وزارة العدل. راجع صفحة التفاصيل الرسمية "
            "لمعرفة نطاق الخدمة ومتطلباتها الحالية، ثم انتقل إلى الإجراء الإلكتروني من الرابط الرسمي."
        ),
        provider_ar="وزارة العدل",
        official_url=(
            "https://www.moj.gov.sa/ar/eservices/pages/"
            "d73f20f6-8693-4381-9a5a-1f7120815918.aspx"
        ),
        official_info_url=(
            "https://www.moj.gov.sa/ar/eservices/pages/"
            "d73f20f6-8693-4381-9a5a-1f7120815918.aspx"
        ),
        last_verified_at="2026-10-19",
        match_phrases=("دعوي مستعجله", "الدعاوي المستعجله", "قضيه مستعجله"),
    ),
    OfficialService(
        id="najiz-enforcement-inquiry",
        title_ar="التحقق من طلب التنفيذ",
        description_ar="التحقق من بيانات طلب التنفيذ باستخدام رقم الطلب ورمز التحقق.",
        guidance_ar=(
            "بحسب صفحة وزارة العدل الرسمية، تتيح خدمة «التحقق من طلب التنفيذ» الاستعلام عن بيانات الطلب. "
            "أدخل رقم طلب التنفيذ ورمز التحقق في صفحة ناجز الرسمية لعرض النتيجة."
        ),
        provider_ar="وزارة العدل — ناجز",
        official_url="https://najiz.sa/applications/iexecution/Inquiry",
        official_info_url=(
            "https://www.moj.gov.sa/ar/eServices/Pages/"
            "8cc249d5-9f9c-464f-a772-25192e8fdf3b.aspx"
        ),
        last_verified_at="2026-10-19",
        match_phrases=(
            "التحقق من طلب تنفيذ",
            "استعلام عن طلب تنفيذ",
            "اتحقق من طلب تنفيذ",
            "اشيك علي التنفيذ",
            "متابعه طلب التنفيذ",
        ),
    ),
    OfficialService(
        id="najiz-heirs-inventory-inquiry",
        title_ar="الاستعلام عن وثيقة حصر الورثة",
        description_ar="الاستعلام عن صكوك حصر الورثة المرقمنة والتحقق من مطابقة الصك إلكترونيًا.",
        guidance_ar=(
            "بحسب صفحة وزارة العدل الرسمية، تتيح خدمة «الاستعلام عن وثيقة حصر الورثة» الاستعلام عن "
            "صكوك حصر الورثة المرقمنة. سجّل الدخول إلى ناجز عبر النفاذ الوطني، ثم اختر الخدمات الإلكترونية، "
            "وباقة خدمات التحقق، وخدمة الاستعلام عن وثيقة حصر الورثة، ثم أدخل بيانات مقدم الطلب والصك "
            "والمتوفى، وأرفق صورة الصك من الأمام والخلف واضغط «تحقق». يلزم أولًا تقديم طلب رقمنة لصك "
            "حصر الورثة الورقي، مع إرفاق صور واضحة للصك."
        ),
        provider_ar="وزارة العدل — ناجز",
        official_url="https://najiz.sa/applications/heirs-inventory/olddeed",
        official_info_url=(
            "https://www.moj.gov.sa/ar/eServices/Pages/"
            "56c74df6-f63d-478e-be46-761633cebb76.aspx"
        ),
        last_verified_at="2026-10-19",
        match_phrases=(
            "استعلام عن وثيقه حصر الورثه",
            "الاستعلام عن وثيقه حصر الورثه",
            "استعلام حصر الورثه",
            "استعلم عن حصر الورثه",
            "التحقق من صك حصر الورثه",
            "استعلام عن صك حصر الورثه",
        ),
    ),
)


def _validate_directory() -> None:
    for service in _SERVICES:
        for url in (service.official_url, service.official_info_url):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in _OFFICIAL_HOSTS:
                raise ValueError(f"Unapproved service URL for {service.id}")


_validate_directory()


def _allowed_url(url: object) -> str | None:
    if not isinstance(url, str) or len(url) > 2000:
        return None
    parsed = urlsplit(url.strip())
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        return None
    return url.strip() if parsed.hostname in _OFFICIAL_HOSTS else None


def _html_items(value: object) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    soup = BeautifulSoup(value, "html.parser")
    items = [node.get_text(" ", strip=True) for node in soup.select("li")]
    if not items:
        items = [soup.get_text(" ", strip=True)]
    return [item for item in items if item]


def _plain_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def _service_from_item(item: object, verified_on: str) -> OfficialService | None:
    if not isinstance(item, dict):
        return None
    item_id = item.get("Id") or item.get("ID")
    title = _plain_text(item.get("Title"))
    description = _plain_text(item.get("Description"))
    if not isinstance(item_id, int) or not title or not description:
        return None
    info_url = f"{_CATALOG_INFO_URL}?itemId={item_id}"
    action_url = _allowed_url(item.get("ServiceURL")) or info_url
    steps = _html_items(item.get("Steps"))[:8]
    requirements = _html_items(item.get("Requirements"))[:6]
    parts = [f"بحسب كتالوج وزارة العدل الرسمي، {description.rstrip('.، ')}."]
    if steps:
        parts.append("خطوات الخدمة: " + "؛ ".join(steps) + ".")
    if requirements:
        parts.append("المتطلبات: " + "؛ ".join(requirements) + ".")
    guidance = " ".join(parts)[:1800].strip()
    return OfficialService(
        id=f"moj-service-{item_id}",
        title_ar=title,
        description_ar=description[:600],
        guidance_ar=guidance,
        provider_ar="وزارة العدل — ناجز",
        official_url=action_url,
        official_info_url=info_url,
        last_verified_at=verified_on,
        match_phrases=(),
    )


def _download_catalog(timeout_seconds: float) -> tuple[OfficialService, ...]:
    params = {
        "$select": "Id,Title,Description,ServiceURL,Steps,Requirements,Modified,Category",
        "$top": str(_CATALOG_MAX_ITEMS),
        # The Ministry's SharePoint currently ignores the nometadata Accept variant unless the
        # response format is explicit, and otherwise returns Atom XML.
        "$format": "json",
    }
    headers = {
        "Accept": "application/json;odata=verbose",
        "User-Agent": "SaudiLegalAI-MVP/0.1 (official service catalog; cached)",
    }
    with httpx.Client(timeout=timeout_seconds, follow_redirects=False, headers=headers) as client:
        response = client.get(_CATALOG_URL, params=params)
        response.raise_for_status()
    if len(response.content) > _CATALOG_MAX_BYTES:
        raise ValueError("official service catalog exceeded the response size limit")
    payload = response.json()
    raw_items = payload.get("value") if isinstance(payload, dict) else None
    if raw_items is None and isinstance(payload, dict):
        verbose = payload.get("d")
        raw_items = verbose.get("results") if isinstance(verbose, dict) else None
    if not isinstance(raw_items, list) or len(raw_items) > _CATALOG_MAX_ITEMS:
        raise ValueError("official service catalog returned an invalid item list")
    verified_on = datetime.now(ZoneInfo("Asia/Riyadh")).date().isoformat()
    services = tuple(service for item in raw_items if (service := _service_from_item(item, verified_on)))
    if not services:
        raise ValueError("official service catalog returned no usable services")
    return services


def clear_official_service_cache() -> None:
    global _catalog_cache
    with _catalog_lock:
        _catalog_cache = None


def refresh_official_service_catalog(*, timeout_seconds: float = 25.0) -> int:
    """Fetch and atomically replace the cached official catalog; old data survives a failed refresh."""
    global _catalog_cache
    services = _download_catalog(timeout_seconds)
    with _catalog_lock:
        _catalog_cache = (time.monotonic(), services)
    return len(services)


def warm_official_service_catalog(*, timeout_seconds: float = 25.0) -> None:
    try:
        count = refresh_official_service_catalog(timeout_seconds=timeout_seconds)
        log.info("official_service_catalog_loaded", extra={"service_count": count})
    except Exception as exc:
        log.warning("official_service_catalog_unavailable", extra={"error": type(exc).__name__})


def run_official_service_refresh_loop(
    stop_event: threading.Event,
    *,
    refresh_seconds: int = 3600,
    timeout_seconds: float = 25.0,
) -> None:
    """Continuously re-read the official source; the snapshot lives only in process memory."""
    while not stop_event.is_set():
        warm_official_service_catalog(timeout_seconds=timeout_seconds)
        if stop_event.wait(max(60, refresh_seconds)):
            return


def _cached_or_live_catalog(*, ttl_seconds: int, timeout_seconds: float) -> tuple[OfficialService, ...]:
    with _catalog_lock:
        hit = _catalog_cache
    if hit is not None and time.monotonic() - hit[0] <= ttl_seconds:
        return hit[1]
    try:
        refresh_official_service_catalog(timeout_seconds=timeout_seconds)
    except Exception as exc:
        log.warning("official_service_search_degraded", extra={"error": type(exc).__name__})
        return hit[1] if hit is not None else ()
    with _catalog_lock:
        return _catalog_cache[1] if _catalog_cache is not None else ()


def _rank_catalog(query: str, services: tuple[OfficialService, ...], limit: int) -> list[OfficialService]:
    normalized_query = normalize_ar(query)
    query_tokens = set(lexical_tokens(query, drop_stopwords=True))
    ranked: list[tuple[float, OfficialService]] = []
    for service in services:
        title_normalized = normalize_ar(service.title_ar)
        title_tokens = set(lexical_tokens(service.title_ar, drop_stopwords=True))
        description_tokens = set(lexical_tokens(service.description_ar, drop_stopwords=True))
        shared_title = query_tokens & title_tokens
        shared_description = query_tokens & description_tokens
        exact = bool(title_normalized and title_normalized in normalized_query)
        title_coverage = len(shared_title) / max(1, len(title_tokens))
        if not exact and not (
            len(shared_title) >= 2 and title_coverage >= 0.4
        ):
            continue
        score = (100 if exact else 0) + 12 * len(shared_title) + 8 * title_coverage + len(shared_description)
        ranked.append((score, service))
    ranked.sort(key=lambda item: (-item[0], item[1].title_ar))
    return [service for _, service in ranked[: max(0, limit)]]


def suggest_official_services(
    query: str,
    *,
    limit: int = 2,
    live: bool = False,
    cache_ttl_seconds: int = 21_600,
    timeout_seconds: float = 25.0,
) -> list[dict[str, str | bool]]:
    """Search reviewed fallbacks, then the complete official MOJ/Najiz service catalog."""
    normalized = normalize_ar(query)
    matches: list[tuple[int, OfficialService]] = []
    for service in _SERVICES:
        score = sum(len(phrase) for phrase in service.match_phrases if phrase in normalized)
        if score:
            matches.append((score, service))
    matches.sort(key=lambda item: (-item[0], item[1].id))
    if matches:
        return [service.public_dict() for _, service in matches[: max(0, limit)]]
    if not live or limit <= 0:
        return []
    # Intent words such as colloquial "أبغى/أبي" are stopwords for legal retrieval, but are
    # decisive here. Preserve them for service routing while the ranker still uses content words.
    intent_tokens = set(lexical_tokens(query, drop_stopwords=False))
    if not (intent_tokens & _SERVICE_INTENT):
        return []
    catalog = _cached_or_live_catalog(ttl_seconds=cache_ttl_seconds, timeout_seconds=timeout_seconds)
    return [service.public_dict() for service in _rank_catalog(query, catalog, limit)]
