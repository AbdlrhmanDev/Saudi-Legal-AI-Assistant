"""Deterministic effective-date evidence (PRD v2 sections 5.3, 7.3, 9).

A date is accepted only when an official text states it next to a commencement phrase
("يعمل به اعتباراً من ...", "يسري من تاريخ نشره ..."). Publication date is never used on its own;
it is used only when the text itself says the rule applies from publication. Hijri-only dates are
reported, not converted, so they are quarantined instead of guessed.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from legal_core.normalize import normalize_ar

# Commencement phrases, in normalized form (no diacritics, unified alef/ya/ta marbuta).
_CUE = (
    r"(?:يعمل ب(?:ه|ها|هذا|هذه|احكام)|يبدا العمل|يسري|تسري|سريان|نافذ|نفاذ|يدخل حيز|"
    r"اعتبارا من|ابتداء من|اعتبار من)"
)
_WINDOW = r"[^.؛\n]{0,80}?"
_ISO = r"(?P<y>(?:19|20)\d\d) (?P<m>\d{1,2}) (?P<d>\d{1,2})\b"
_DMY = r"(?P<d2>\d{1,2}) (?P<m2>\d{1,2}) (?P<y2>(?:19|20)\d\d) ?م?\b"
_HIJRI = r"\b\d{1,2} \d{1,2} 1[34]\d\d ?ه?\b|\b1[34]\d\d \d{1,2} \d{1,2} ?ه?\b"

_EXPLICIT = re.compile(rf"{_CUE}{_WINDOW}(?:{_ISO}|{_DMY})")
_FROM_PUBLICATION = re.compile(rf"{_CUE}{_WINDOW}من تاريخ (?:نشر|صدور)")
_OFFSET = re.compile(rf"{_CUE}{_WINDOW}بعد (?:مضي )?(?P<n>\d+|[^\d ]+(?: و[^\d ]+)?) (?:يوما|يوم) من تاريخ نشر")
_HIJRI_CUE = re.compile(rf"{_CUE}{_WINDOW}(?:{_HIJRI})")

_NUMBER_WORDS = {
    normalize_ar(word): value
    for word, value in {
        "ثلاثين": 30, "خمسة وأربعين": 45, "ستين": 60, "تسعين": 90, "مئة وعشرين": 120,
        "مائة وعشرين": 120, "مئة وثمانين": 180, "مائة وثمانين": 180, "ثلاثمائة وخمسة وستين": 365,
    }.items()
}  # fmt: skip


@dataclass(frozen=True)
class DateEvidence:
    value: date
    kind: str  # explicit | from_publication | publication_offset
    span: str  # normalized text the date was read from
    origin: str  # notice | document

    def as_dict(self) -> dict:
        return {"value": self.value.isoformat(), "kind": self.kind, "span": self.span, "origin": self.origin}


@dataclass
class DateFindings:
    evidence: list[DateEvidence] = field(default_factory=list)
    hijri_spans: list[str] = field(default_factory=list)
    needs_publication_date: bool = False


def _safe_date(y: str, m: str, d: str) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def extract_effective_dates(text: str, *, origin: str, published_at: date | None) -> DateFindings:
    norm = normalize_ar(text)
    found = DateFindings()
    for m in _EXPLICIT.finditer(norm):
        value = (
            _safe_date(m["y"], m["m"], m["d"]) if m["y"] else _safe_date(m["y2"], m["m2"], m["d2"])
        )
        if value:
            found.evidence.append(DateEvidence(value, "explicit", m.group(0), origin))
    for m in _OFFSET.finditer(norm):
        raw = m["n"]
        days = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)
        if days is None:
            continue
        if published_at is None:
            found.needs_publication_date = True
            continue
        found.evidence.append(DateEvidence(published_at + timedelta(days=days), "publication_offset", m.group(0), origin))
    offset_spans = [e.span for e in found.evidence if e.kind == "publication_offset"]
    for m in _FROM_PUBLICATION.finditer(norm):
        if any(m.group(0) in span or span in m.group(0) for span in offset_spans) or "بعد" in m.group(0):
            continue
        if published_at is None:
            found.needs_publication_date = True
            continue
        found.evidence.append(DateEvidence(published_at, "from_publication", m.group(0), origin))
    found.hijri_spans = [m.group(0) for m in _HIJRI_CUE.finditer(norm)]
    return found


@dataclass
class EffectiveDate:
    value: date | None
    reason_code: str | None
    detail: str | None
    evidence: list[DateEvidence]

    def as_dict(self) -> dict:
        return {
            "value": self.value.isoformat() if self.value else None,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "evidence": [e.as_dict() for e in self.evidence],
        }


def resolve_effective_date(
    *,
    notice_text: str,
    document_text: str,
    published_at: date | None,
    consolidated_amendment: bool,
) -> EffectiveDate:
    """One unambiguous effective date, or a quarantine reason.

    In a consolidated amended text, "applies from its publication" is the original statute's own
    commencement clause, not the amendment's, so only the notice may supply a relative date then.
    """
    notice = extract_effective_dates(notice_text, origin="notice", published_at=published_at)
    document = extract_effective_dates(document_text, origin="document", published_at=published_at)
    doc_evidence = [
        e for e in document.evidence if not (consolidated_amendment and e.kind != "explicit")
    ]
    evidence = notice.evidence + doc_evidence
    values = sorted({e.value for e in evidence})
    if len(values) > 1:
        return EffectiveDate(
            None, "CONFLICTING_EFFECTIVE_DATES", "official texts state different effective dates: "
            + ", ".join(v.isoformat() for v in values), evidence,
        )  # fmt: skip
    if len(values) == 1:
        return EffectiveDate(values[0], None, None, evidence)
    if notice.hijri_spans or document.hijri_spans:
        return EffectiveDate(
            None, "HIJRI_DATE_UNVERIFIED", "only a Hijri commencement date was found; it is not converted automatically",
            evidence,
        )  # fmt: skip
    if notice.needs_publication_date or (document.needs_publication_date and not consolidated_amendment):
        return EffectiveDate(None, "PUBLICATION_DATE_MISSING", "commencement depends on an unknown publication date", evidence)
    return EffectiveDate(None, "EFFECTIVE_DATE_MISSING", "no commencement clause with a verifiable date was found", evidence)
