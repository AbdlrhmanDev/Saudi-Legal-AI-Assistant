"""Operator alerts. Informational only: healthy auto-publishing never waits on them (PRD v2 15)."""

import logging

import httpx

from legal_core.config import Settings

log = logging.getLogger("alerts")


def emit(settings: Settings, kind: str, message: str, details: dict | None = None) -> None:
    """Always logged as a structured `alert` event; also POSTed to ALERT_WEBHOOK_URL when set
    (Slack/Teams-compatible `text` field). Delivery failures are logged and swallowed."""
    payload = {"kind": kind, "message": message, "details": details or {}}
    log.warning("alert", extra={"alert_kind": kind, "alert_message": message, "alert_details": details or {}})
    if not settings.alert_webhook_url:
        return
    try:
        httpx.post(
            settings.alert_webhook_url,
            json={"text": f"[Saudi Legal AI] {kind}: {message}", **payload},
            timeout=5.0,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("alert_delivery_failed", extra={"alert_kind": kind, "error": type(exc).__name__})
