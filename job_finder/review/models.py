from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import ClassVar, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ReviewLane = Literal["qualified", "rejected_audit"]
ReviewDecision = Literal["pursue", "reject", "unsure"]
ReviewOutcome = Literal["qualified", "rejected"]
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
    note: str | None = Field(default=None, max_length=2000)
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
