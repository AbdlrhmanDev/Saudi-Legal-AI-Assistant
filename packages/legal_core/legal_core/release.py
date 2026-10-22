"""Deterministic release gate (PRD v2 7.3, 8.1).

The agent's proposal is an input, never the decision. A candidate is published automatically only
when every code-level check passes: official source identity, a stored snapshot, a supported legal
type, a valid article structure, a resolved target instrument, one evidenced effective date and no
conflicting authoritative version. Anything else is quarantined with a reason code; nothing is
guessed.
"""

from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import and_, not_, select
from sqlalchemy.orm import Session

from legal_core.agent import AgentProposal
from legal_core.config import Settings
from legal_core.legal_dates import DateEvidence, EffectiveDate, resolve_effective_date
from legal_core.models import (
    NON_LEGISLATIVE_KINDS,
    PUBLISHABLE_CLASSIFICATIONS,
    DocumentVersion,
    SourceSnapshot,
    UpdateCandidate,
)
from legal_core.parser import parse_boe_metadata
from legal_core.review import authoritative_versions

RIYADH = ZoneInfo("Asia/Riyadh")

# Keyword rules cannot tell what a bare decree/decision/circular does; the agent may classify these.
INCONCLUSIVE_CLASSIFICATIONS = ("royal_decree", "decision", "circular")
# Reasons a later run may clear on its own (the source was down, a budget was hit).
TRANSIENT_REASONS = ("SOURCE_UNAVAILABLE", "EMBEDDING_BUDGET_EXCEEDED", "INDEX_HEALTH_FAILED", "PUBLISH_FAILED")


@dataclass
class GateDecision:
    outcome: str  # publish | quarantine | exclude
    kind: str | None = None  # new_law | amendment | repeal (what publication will do)
    reasons: list[tuple[str, str]] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    effective: EffectiveDate | None = None

    @property
    def reason_code(self) -> str | None:
        return self.reasons[0][0] if self.reasons else None

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "kind": self.kind,
            "reasons": [{"code": c, "detail": d} for c, d in self.reasons],
            "checks": self.checks,
            "effective_date": self.effective.as_dict() if self.effective else None,
        }


def _official_source(candidate: UpdateCandidate, settings: Settings) -> bool:
    source = candidate.source
    if source is None or not source.enabled:
        return False
    if source.crawl_policy == "fixture":
        return settings.allow_fixture_sources and source.is_test_data
    host = (urlsplit(candidate.canonical_url or "").hostname or "").lower()
    return (
        urlsplit(candidate.canonical_url or "").scheme == "https"
        and host in settings.allowlisted_hosts
        and source.allowlisted_host.lower() in settings.allowlisted_hosts
    )


def _structure_problem(version: DocumentVersion) -> tuple[str, str] | None:
    warnings = version.parse_warnings or []
    if version.article_count == 0 or "no_articles_detected" in warnings or "empty_official_text" in warnings:
        return "ARTICLE_STRUCTURE_INVALID", "no articles could be extracted from the official text"
    duplicates = [w.split(":", 1)[1] for w in warnings if w.startswith("duplicate_article_number:")]
    if duplicates:
        return "CONFLICTING_ARTICLE_NUMBERS", f"article numbers appear more than once: {', '.join(duplicates)}"
    numbers = [a.article_number for a in version.articles if a.article_number]
    if len(numbers) != len(set(numbers)):
        return "CONFLICTING_ARTICLE_NUMBERS", "article numbers appear more than once"
    return None


def evaluate(
    session: Session, candidate: UpdateCandidate, proposal: AgentProposal, settings: Settings
) -> GateDecision:
    decision = GateDecision(outcome="quarantine")
    checks = decision.checks

    def fail(code: str, detail: str) -> GateDecision:
        decision.reasons.append((code, detail))
        return decision

    source = candidate.source
    if source is not None and source.document_kind in NON_LEGISLATIVE_KINDS:
        decision.outcome = "exclude"
        checks["legislative_source"] = False
        return fail(
            "NOT_A_LEGAL_CHANGE",
            f"{source.document_kind} sources (for example Istitlaa drafts) never enter the authoritative corpus",
        )

    rules_class = candidate.classification
    agent_class = proposal.classification
    if proposal.provider != "rules" and agent_class != rules_class:
        if rules_class == "draft":
            pass  # an explicit draft/consultation marker always wins: excluded below
        elif rules_class in INCONCLUSIVE_CLASSIFICATIONS:
            candidate.classification = agent_class  # the rules could not tell; the gate still verifies
        elif rules_class in PUBLISHABLE_CLASSIFICATIONS or agent_class in PUBLISHABLE_CLASSIFICATIONS:
            checks["classification_agreement"] = False
            return fail("AGENT_RULES_DISAGREE", f"rules say {rules_class}, agent says {agent_class}")
    classification = candidate.classification

    checks["supported_type"] = classification in PUBLISHABLE_CLASSIFICATIONS
    if not checks["supported_type"]:
        decision.outcome = "exclude"
        return fail("NOT_A_LEGAL_CHANGE", f"{classification} items are never part of the authoritative corpus")

    checks["official_source"] = _official_source(candidate, settings)
    if not checks["official_source"]:
        return fail("NOT_OFFICIAL_SOURCE", "the publication is not from an enabled, allowlisted official source")

    version = candidate.proposed_version
    checks["snapshot"] = version is not None and version.snapshot_id is not None
    if version is None:
        if candidate.state == "failed":
            return fail("SOURCE_UNAVAILABLE", candidate.error or "the official text could not be fetched")
        return fail("NO_OFFICIAL_TEXT", "no official text is available for automated retrieval from this source")
    if version.snapshot_id is None:
        return fail("NO_OFFICIAL_TEXT", "the parsed version has no stored source snapshot")

    checks["no_injection"] = "possible_prompt_injection" not in (version.parse_warnings or [])
    if not checks["no_injection"]:
        return fail("PROMPT_INJECTION_DETECTED", "the document contains instructions addressed to an AI system")

    target_id = candidate.target_instrument_id
    if classification == "repeal":
        decision.kind = "repeal"
        checks["target_resolved"] = target_id is not None and target_id != version.instrument_id
        if not checks["target_resolved"]:
            return fail("TARGET_UNRESOLVED", "the repealed instrument could not be identified unambiguously")
        current = authoritative_versions(session, target_id, exclude=version.id)
        if not current:
            return fail("TARGET_UNRESOLVED", "the repealed instrument has no authoritative version")
        if any(v.end_reason == "repealed" for v in current):
            decision.outcome = "exclude"
            return fail("ALREADY_REPEALED", "the instrument is already recorded as repealed")
    else:
        problem = _structure_problem(version)
        checks["article_structure"] = problem is None
        if problem:
            return fail(*problem)
        has_base = bool(authoritative_versions(session, version.instrument_id, exclude=version.id))
        decision.kind = "amendment" if has_base else "new_law"
        if classification == "amendment":
            checks["target_resolved"] = has_base and (target_id is None or target_id == version.instrument_id)
            if not checks["target_resolved"]:
                return fail("TARGET_UNRESOLVED", "the amended instrument has no authoritative version to amend")
        diff = candidate.diff or {}
        checks["amendment_mapping"] = "possible_partial_amendment_document" not in diff.get("warnings", [])
        if not checks["amendment_mapping"]:
            return fail(
                "AMBIGUOUS_AMENDMENT_MAPPING",
                "the document looks like a partial amendment, not a consolidated text; article mapping is uncertain",
            )
        summary = diff.get("summary") or {}
        if has_base and not any(summary.get(k) for k in ("added", "changed", "removed")):
            decision.outcome = "exclude"
            return fail("NO_TEXTUAL_CHANGE", "the official text is identical to the version already in force")

    effective = resolve_effective_date(
        notice_text=f"{candidate.title}\n{candidate.summary}",
        document_text=version.raw_text,
        published_at=version.published_at,
        consolidated_amendment=decision.kind == "amendment",
    )
    if effective.value is None and decision.kind != "repeal":
        verified = _publisher_status(session, candidate, version, effective)
        if verified is not None:
            if verified.reason_code:
                return fail(verified.reason_code, verified.detail or "")
            effective = verified
            checks["publisher_status"] = True
    decision.effective = effective
    if effective.value is not None and version.effective_from and version.effective_from != effective.value:
        effective.reason_code = "CONFLICTING_EFFECTIVE_DATES"
        effective.detail = f"metadata says {version.effective_from}, the official text says {effective.value}"
        effective.value = None
    checks["effective_date"] = effective.value is not None
    if effective.value is None:
        return fail(effective.reason_code or "EFFECTIVE_DATE_MISSING", effective.detail or "")

    others = authoritative_versions(session, target_id if decision.kind == "repeal" else version.instrument_id,
                                    exclude=version.id)  # fmt: skip
    if decision.kind == "repeal":
        in_force = [v for v in others if v.effective_from < effective.value]
        later = [v for v in others if v.effective_from >= effective.value]
        checks["no_conflicting_version"] = bool(in_force) and not later
        if not checks["no_conflicting_version"]:
            return fail("CONFLICTING_EFFECTIVE_VERSION", "the repeal date does not follow the versions it would end")
    else:
        checks["no_conflicting_version"] = not any(o.effective_from == effective.value for o in others)
        if not checks["no_conflicting_version"]:
            return fail("CONFLICTING_EFFECTIVE_VERSION", "another authoritative version starts on the same date")

    decision.outcome = "publish"
    return decision


def _publisher_status(
    session: Session, candidate: UpdateCandidate, version: DocumentVersion, unresolved: EffectiveDate
) -> EffectiveDate | None:
    """Consolidated laws on the Bureau of Experts portal rarely state a commencement date in their text
    (old statutes, many amendments), but the Bureau publishes each law's official status. A law marked
    «ساري» is verified in force *as of the day its official page was retrieved*: that day becomes the
    version's start, so the text is never presented as in force on any earlier date. Any other status
    blocks publication. Returns None when this rule does not apply."""
    source = candidate.source
    if source is None or source.source_key != "boe" or version.snapshot_id is None:
        return None
    snapshot = session.get(SourceSnapshot, version.snapshot_id)
    metadata = parse_boe_metadata(snapshot.content) if snapshot else None
    if metadata is None or not metadata.status:
        return None
    if metadata.status != "ساري":
        return EffectiveDate(None, "STATUS_NOT_IN_FORCE",
                             f"the Bureau of Experts lists this law as «{metadata.status}»", unresolved.evidence)  # fmt: skip
    if metadata.published_at and version.published_at is None:
        version.published_at = metadata.published_at
    verified_on = snapshot.retrieved_at.astimezone(RIYADH).date()
    span = (
        f"هيئة الخبراء بمجلس الوزراء: الحالة «ساري» كما في {verified_on.isoformat()}"
        + (f"؛ تاريخ النشر {metadata.published_at.isoformat()}" if metadata.published_at else "")
    )
    return EffectiveDate(verified_on, None, None, [DateEvidence(verified_on, "publisher_status", span, "publisher")])


def unprocessed_candidates(session: Session, limit: int) -> list[UpdateCandidate]:
    """Feed/agent candidates the gate has not decided yet, oldest first. Documents whose fetch was
    deferred by a connector's politeness budget (backlog) are not judged until they are fetched."""
    backlog = and_(
        UpdateCandidate.state == "candidate",
        UpdateCandidate.document_ref.is_not(None),
        UpdateCandidate.proposed_version_id.is_(None),
        UpdateCandidate.classification.not_in(("irrelevant", "draft", "notice")),
    )
    return list(
        session.scalars(
            select(UpdateCandidate)
            .where(
                UpdateCandidate.origin.in_(("feed", "agent")),
                UpdateCandidate.state.in_(("candidate", "retrieved", "needs_review", "failed")),
                not_(backlog),
            )
            .order_by(UpdateCandidate.discovered_at, UpdateCandidate.id)
            .limit(limit)
        )
    )


def is_effective(decision: GateDecision, today: date) -> bool:
    return bool(decision.effective and decision.effective.value and decision.effective.value <= today)
