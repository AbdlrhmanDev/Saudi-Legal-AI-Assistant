"""Search the live Ministry of Justice service directory without mixing it into legal RAG.

Every service query reads the public Ministry source at request time.  Results are transient and
are never persisted or cached by this application.  Service cards are never legal evidence.
"""

import logging
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
        "متابعه", "انشاء", "اسوي", "سوي", "الكتروني", "الكترونيه", "الكترونيا", "اريد", "ودي",
        "احتاج", "كيف", "طريقه", "اجراء", "رابط", "افتح", "الغي", "الغاء", "فسخ", "اثبات",
    }
)
_GENERIC_SERVICE_TOKENS = frozenset(
    {"خدمه", "طلب", "استعلام", "تحقق", "بيانات", "اصدار", "تقديم", "الكتروني", "وثيقه"}
)


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
        "User-Agent": "SaudiLegalAI-MVP/0.1 (live official service lookup)",
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
        distinctive_title = shared_title - _GENERIC_SERVICE_TOKENS
        exact = bool(title_normalized and title_normalized in normalized_query)
        title_coverage = len(shared_title) / max(1, len(title_tokens))
        if not exact and not (
            (len(shared_title) >= 2 and title_coverage >= 0.4)
            or (distinctive_title and title_coverage >= 0.4)
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
    timeout_seconds: float = 25.0,
) -> list[dict[str, str | bool]]:
    """Search the current official MOJ/Najiz directory without persisting a local copy."""
    if not live or limit <= 0:
        return []
    # Intent words such as colloquial "أبغى/أبي" are stopwords for legal retrieval, but are
    # decisive here. Preserve them for service routing while the ranker still uses content words.
    intent_tokens = set(lexical_tokens(query, drop_stopwords=False))
    if not (intent_tokens & _SERVICE_INTENT):
        return []
    try:
        current_services = _download_catalog(timeout_seconds)
    except Exception as exc:
        log.warning("official_service_live_search_failed", extra={"error": type(exc).__name__})
        return []
    return [service.public_dict() for service in _rank_catalog(query, current_services, limit)]
