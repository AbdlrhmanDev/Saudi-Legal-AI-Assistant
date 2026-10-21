"""Fetch official baseline laws locally and upload them to a remote deployment.

This operator path is useful when a cloud region cannot connect directly to the official
publisher. The deployment still receives the exact fetched bytes, official source URL and
verified dates, stores an immutable snapshot, parses it, and indexes it through the normal
admin review API.
"""

from __future__ import annotations

import argparse
import os
import time

import httpx
from import_official_baseline import DOCUMENTS, OfficialDocument

from legal_core.config import get_settings
from legal_core.legal_dates import resolve_effective_date
from legal_core.monitor import make_http_fetcher
from legal_core.parser import document_text, parse_boe_html


def _data(response: httpx.Response) -> dict:
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(payload.get("error", {}).get("message", "remote API request failed"))
    return payload["data"]


def _wait_for_job(client: httpx.Client, job_id: str, timeout_seconds: int = 180) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = _data(client.get(f"/api/v1/admin/jobs/{job_id}"))
        if job["status"] in {"succeeded", "duplicate", "failed"}:
            if job["status"] == "failed":
                raise RuntimeError(f"remote ingestion failed: {job.get('errors', [])}")
            return job
        time.sleep(1)
    raise TimeoutError(f"remote ingestion job {job_id} did not finish")


def upload_document(client: httpx.Client, document: OfficialDocument, content: bytes) -> dict:
    queued = _data(
        client.post(
            "/api/v1/admin/documents",
            files={"file": (f"{document.title}.html", content, "text/html")},
            data={
                "title_ar": document.title,
                "domain": document.domain,
                "instrument_type": "law",
                "issuing_authority": "هيئة الخبراء بمجلس الوزراء",
                "source_url": document.url,
                "published_at": document.published_at.isoformat(),
                "effective_from": document.expected_effective_from.isoformat(),
                "is_test_data": "false",
            },
        )
    )
    job = _wait_for_job(client, queued["job_id"])
    candidate_id = job.get("result", {}).get("candidate_id")
    if candidate_id:
        _data(
            client.post(
                f"/api/v1/admin/updates/{candidate_id}/approve",
                json={
                    "effective_from": document.expected_effective_from.isoformat(),
                    "published_at": document.published_at.isoformat(),
                    "effective_date_evidence": (
                        "تم التحقق من تاريخ النفاذ في النص الموحد المنشور في بوابة الأنظمة "
                        f"ومن رابط المصدر الرسمي؛ تاريخ النشر {document.published_at.isoformat()}"
                    ),
                    "reason": "Remote official baseline import requested by the project owner",
                    "acknowledge_warnings": False,
                },
            )
        )
    return {
        "title": document.title,
        "job_status": job["status"],
        "articles": job.get("result", {}).get("article_count"),
        "approved": bool(candidate_id) or job["status"] == "duplicate",
    }


def validate_official_document(document: OfficialDocument, content: bytes) -> None:
    """Reject incomplete, injected, or date-inconsistent publisher responses."""
    parsed = parse_boe_html(content)
    if parsed is None or len(parsed.articles) < document.minimum_articles:
        raise RuntimeError(f"official structure check failed for {document.title}")
    if "possible_prompt_injection" in parsed.warnings:
        raise RuntimeError(f"source isolation check failed for {document.title}")
    effective = resolve_effective_date(
        notice_text="",
        document_text=document_text(parsed),
        published_at=document.published_at,
        consolidated_amendment=False,
    )
    if effective.value != document.expected_effective_from or not effective.evidence:
        raise RuntimeError(
            f"effective-date check failed for {document.title}: "
            f"expected {document.expected_effective_from}, found {effective.value}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        raise RuntimeError("ADMIN_PASSWORD is required")

    settings = get_settings()
    fetch = make_http_fetcher(settings)
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=60) as client:
        session = _data(client.post("/api/v1/auth/login", json={"email": args.email, "password": password}))
        client.headers["Authorization"] = f"Bearer {session['access_token']}"
        for document in DOCUMENTS:
            content = fetch(document.url)
            validate_official_document(document, content)
            result = upload_document(client, document, content)
            print(result, flush=True)


if __name__ == "__main__":
    main()
