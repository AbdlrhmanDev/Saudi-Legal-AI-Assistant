import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Domain = Literal[
    "labor", "commercial", "data_protection", "other", "real_estate", "tax_customs", "banking_finance",
    "capital_markets", "insurance", "investment", "social_insurance", "health", "food_drug", "transport",
    "municipal", "cybersecurity", "digital_telecom", "intellectual_property", "judicial", "administrative",
    "criminal", "consumer", "competition",
]  # fmt: skip
Classification = Literal[
    "new_law", "amendment", "repeal", "regulation", "royal_decree", "decision", "circular", "notice", "draft",
    "irrelevant",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


def _strong_password(value: str) -> str:
    if len(value) < 10 or not any(c.isdigit() for c in value) or not any(c.isascii() and c.isalpha() for c in value):
        raise ValueError("password needs at least 10 characters, a Latin letter and a digit")
    return value


class RegisterRequest(StrictModel):
    name: str = Field(min_length=2, max_length=200)
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=10, max_length=200)
    accept_terms: Literal[True]

    _check_password = field_validator("password")(_strong_password)


class ForgotRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)


class ResetRequest(StrictModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=10, max_length=200)

    _check_password = field_validator("password")(_strong_password)


class ChatRequest(StrictModel):
    query: str = Field(min_length=2, max_length=1000, examples=["ما الذي ينص عليه نظام العمل بشأن فترة التجربة؟"])
    domain: Domain | None = Field(default=None, examples=["labor"])
    as_of_date: date | None = Field(default=None, examples=["2026-10-12"])

    @field_validator("query")
    @classmethod
    def _printable(cls, value: str) -> str:
        cleaned = "".join(ch for ch in value if ch.isprintable() or ch in "\n\t")
        if len(cleaned.strip()) < 2:
            raise ValueError("query must contain text")
        return cleaned

    @field_validator("as_of_date")
    @classmethod
    def _sane_date(cls, value: date | None) -> date | None:
        if value is not None and not (date(1930, 1, 1) <= value <= date(2100, 1, 1)):
            raise ValueError("as_of_date is out of range")
        return value


class Citation(BaseModel):
    law_id: str
    instrument_id: str
    article_id: str
    version_id: str
    source_url: str | None
    official_url: str | None
    law_title_ar: str
    article_number: str | None
    article_label: str | None
    chapter: str | None
    quote: str
    effective_from: date
    effective_to: date | None
    legal_status: str
    issuing_authority: str | None = None
    status_basis: str | None = Field(
        default=None, description="`publisher_status`: verified in force by the official publisher on `effective_from`."
    )
    is_test_data: bool


class AnswerAction(BaseModel):
    kind: Literal["legal_step", "fact_to_confirm"]
    text: str
    article_ids: list[str] = Field(default_factory=list)


class AnswerSource(BaseModel):
    law_title_ar: str
    article_label: str | None
    article_number: str | None
    issuing_authority: str | None
    legal_status_ar: str
    effective_from: date
    official_url: str | None


class AnswerSections(BaseModel):
    """الجواب / وش تقدر تسوي؟ / المصدر الرسمي. Present only for grounded or partial answers."""

    headings: dict[str, str]
    answer: str
    actions: list[AnswerAction]
    sources: list[AnswerSource]


class JudicialReference(BaseModel):
    """A published judgment shown for orientation. Never evidence for the answer, never binding."""

    id: str
    title: str
    material_type: str
    court_name: str | None
    court_level: str
    city: str | None
    hijri_date: str | None
    judgment_date: date | None
    finality_status: str
    source_url: str | None
    excerpt: str
    binding: Literal[False] = False
    note_ar: str
    is_test_data: bool


class DraftReference(BaseModel):
    title: str
    official_url: str | None
    published_at: date | None
    legal_status: Literal["draft"] = "draft"
    note_ar: str
    is_test_data: bool


class RelatedService(BaseModel):
    """A reviewed official transaction link; never a source for the legal answer."""

    id: str
    title_ar: str
    description_ar: str
    guidance_ar: str
    provider_ar: str
    official_url: str
    official_info_url: str
    last_verified_at: date
    legal_evidence: Literal[False] = False


class ChatAnswer(BaseModel):
    """PRD 9.3 answer contract, plus display fields for the UI."""

    answer_ar: str
    answer_status: Literal["grounded", "partial", "insufficient_evidence", "out_of_scope"] = Field(
        description="`out_of_scope`: not a Saudi legal question; `answer_ar` is then exactly «غير متوفر معلومات عن ذا الشي»."
    )
    as_of_date: date
    citations: list[Citation]
    caveats: list[str]
    trace_id: str
    disclaimer_ar: str
    forthcoming: list[dict]
    corpus_verified_at: str | None = Field(description="Finish time of the last verified sync (Asia/Riyadh).")
    coverage_notice: str
    source_health: Literal["healthy", "delayed", "degraded"]
    related_services: list[RelatedService] = Field(default_factory=list)
    answer_sections: AnswerSections | None = None
    related_judicial: list[JudicialReference] = Field(default_factory=list)
    related_drafts: list[DraftReference] = Field(default_factory=list)
    query_analysis: dict = Field(
        default_factory=dict, description="Detected register, legal intents, entities and facts to confirm (no user text)."
    )


class ChatResponse(BaseModel):
    success: Literal[True] = True
    data: ChatAnswer


class ApproveRequest(StrictModel):
    effective_from: date | None = Field(
        default=None, description="Confirmed legal effective date. Required unless already set on the version."
    )
    effective_date_evidence: str = Field(
        min_length=5, max_length=2000, description="Where the effective date was verified (clause, gazette issue…)."
    )
    published_at: date | None = None
    reason: str | None = Field(default=None, max_length=2000)
    acknowledge_warnings: bool = Field(
        default=False, description="Required when the document carries blocking parse/diff warnings."
    )


class RejectRequest(StrictModel):
    reason: str = Field(min_length=3, max_length=2000)


class RollbackRequest(StrictModel):
    reason: str = Field(min_length=3, max_length=2000)


class DocumentMetadataPatch(StrictModel):
    title_ar: str | None = Field(default=None, min_length=1, max_length=500)
    domain: Domain | None = None
    instrument_type: str | None = Field(default=None, max_length=40)
    issuing_authority: str | None = Field(default=None, max_length=300)
    source_url: str | None = Field(default=None, max_length=2000)
    version_label: str | None = Field(default=None, max_length=200)
    issued_at: date | None = None
    published_at: date | None = None
    effective_from: date | None = None


class UpdateCandidatePatch(StrictModel):
    classification: Classification | None = None
    target_instrument_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=2000)


class ProbeRequest(StrictModel):
    source_id: uuid.UUID | None = Field(default=None, description="Probe one source; default: every source.")


class SyncRequest(StrictModel):
    source_id: uuid.UUID | None = None


class SourcePatch(StrictModel):
    enabled: bool | None = None
    check_interval: int | None = Field(default=None, ge=15, le=10080, description="Minutes between checks.")


class IndexRollbackRequest(StrictModel):
    reason: str = Field(min_length=3, max_length=2000)
    target_generation_id: uuid.UUID | None = Field(
        default=None, description="Generation to restore. Default: the one before the active generation."
    )
