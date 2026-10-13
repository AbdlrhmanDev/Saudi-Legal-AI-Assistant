import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Domain = Literal["labor", "commercial", "data_protection", "other"]
Classification = Literal[
    "new_law", "amendment", "regulation", "royal_decree", "decision", "circular", "notice", "irrelevant"
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
    article_id: str
    version_id: str
    source_url: str | None
    law_title_ar: str
    article_number: str | None
    article_label: str | None
    chapter: str | None
    quote: str
    effective_from: date
    effective_to: date | None
    legal_status: str
    is_test_data: bool


class ChatAnswer(BaseModel):
    """PRD 9.3 answer contract, plus display fields for the UI."""

    answer_ar: str
    answer_status: Literal["grounded", "partial", "insufficient_evidence"]
    as_of_date: date
    citations: list[Citation]
    caveats: list[str]
    trace_id: str
    disclaimer_ar: str
    forthcoming: list[dict]


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


class SyncRequest(StrictModel):
    source_id: uuid.UUID | None = None


class SourcePatch(StrictModel):
    enabled: bool | None = None
    check_interval: int | None = Field(default=None, ge=15, le=10080, description="Minutes between checks.")
