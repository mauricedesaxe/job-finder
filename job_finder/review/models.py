from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, ClassVar, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ReviewLane = Literal["qualified", "rejected_audit"]
ReviewDecision = Literal["pursue", "reject", "unsure"]
ReviewOutcome = Literal["qualified", "rejected"]
FeedbackCurationFilter = Literal["all", "uncurated", "included", "excluded"]
TargetProfile = Literal[
    "early-stage-product-engineer",
    "applied-ai-product-engineer",
    "neither",
]
PrimaryReason = Literal[
    "crypto-company",
    "location",
    "compensation",
    "role-scope",
    "technology-fit",
    "company-quality",
    "work-environment",
    "insufficient-information",
    "other",
]


class ReviewModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class ReviewJob(ReviewModel):
    title: str
    company: str
    url: str
    source: str
    description: str
    location: str
    keywords: tuple[str, ...]
    date_posted: date | None
    compensation: Compensation | None = None


class Compensation(ReviewModel):
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    currency: str | None = None
    period: str | None = None
    source: str | None = None


class ReviewItem(ReviewModel):
    review_day: date
    id: UUID
    evaluation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    lane: ReviewLane
    position: int = Field(ge=0)
    outcome: ReviewOutcome
    matched_profile: str | None
    evaluation_reason: str
    job: ReviewJob
    reviewed: bool = False
    decision: ReviewDecision | None = None
    note: str | None = None
    block_company: bool = False

    @model_validator(mode="after")
    def lane_matches_outcome(self) -> Self:
        if (self.lane == "qualified") != (self.outcome == "qualified"):
            raise ValueError("Review lane must match the evaluation outcome")
        if self.reviewed != (self.decision is not None):
            raise ValueError("A reviewed item must carry its recorded decision")
        return self


class FeedbackCuration(ReviewModel):
    id: UUID
    action: Literal["include", "exclude"]
    expected_outcome: ReviewOutcome | None
    critical: bool
    reason: str
    actor: str
    created_at: datetime


class FeedbackCurationSummary(ReviewModel):
    id: UUID
    action: Literal["include", "exclude"]
    expected_outcome: ReviewOutcome | None
    critical: bool


class ReviewFeedbackSummary(ReviewModel):
    review_event_id: UUID
    decision: ReviewDecision
    target_profile: str | None
    primary_reason: str | None
    created_at: datetime
    original_outcome: ReviewOutcome
    title: str
    company: str
    curation: FeedbackCurationSummary | None = None
    frozen_manifest_count: int = Field(ge=0)


class ReviewFeedback(ReviewModel):
    review_event_id: UUID
    review_item_id: UUID
    evaluation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ReviewDecision
    target_profile: str | None
    primary_reason: str | None
    note: str | None
    block_company: bool
    actor: str
    created_at: datetime
    original_outcome: ReviewOutcome
    matched_profile: str | None
    evaluation_reason: str
    job: ReviewJob
    curation: FeedbackCuration | None = None
    frozen_manifest_count: int = Field(ge=0)


class ReviewFeedbackPage(ReviewModel):
    items: Annotated[tuple[ReviewFeedbackSummary, ...], Field(max_length=100)]
    next_offset: int | None = Field(default=None, ge=0)


class ReviewQueue(ReviewModel):
    items: tuple[ReviewItem, ...] = ()
    reviewed_items: tuple[ReviewItem, ...] = ()
    reviewed_counts: Mapping[date, int] = {}

    @model_validator(mode="after")
    def counts_are_not_negative(self) -> ReviewQueue:
        if any(count < 0 for count in self.reviewed_counts.values()):
            raise ValueError("Reviewed counts cannot be negative")
        return self

    def reviewed_count(self, review_day: date) -> int:
        return self.reviewed_counts.get(review_day, 0)


class ReviewSubmission(ReviewModel):
    review_item_id: UUID
    evaluation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ReviewDecision
    target_profile: TargetProfile | None = None
    primary_reason: PrimaryReason | None = None
    note: str | None = None
    block_company: bool = False
    actor: str = Field(min_length=1)
    created_at: datetime

    @field_validator("note", mode="before")
    @classmethod
    def empty_note_is_absent(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class ReviewSaved(ReviewModel):
    kind: Literal["saved"] = "saved"
    review_event_id: UUID


class ReviewConflict(ReviewModel):
    kind: Literal["conflict"] = "conflict"
    reason: str


ReviewSubmitResult = ReviewSaved | ReviewConflict
