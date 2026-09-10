from __future__ import annotations

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
    id: UUID
    evaluation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    lane: ReviewLane
    position: int = Field(ge=0)
    outcome: ReviewOutcome
    matched_profile: str | None
    evaluation_reason: str
    job: ReviewJob

    @model_validator(mode="after")
    def lane_matches_outcome(self) -> Self:
        if (self.lane == "qualified") != (self.outcome == "qualified"):
            raise ValueError("Review lane must match the evaluation outcome")
        return self


class ReviewLaneState(ReviewModel):
    lane: ReviewLane
    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    pending: tuple[ReviewItem, ...]

    @model_validator(mode="after")
    def counts_and_items_match(self) -> Self:
        if self.completed + len(self.pending) != self.total:
            raise ValueError("Review lane counts must account for every item")
        if any(item.lane != self.lane for item in self.pending):
            raise ValueError("Pending review items must belong to their lane")
        return self


class DailyReview(ReviewModel):
    day: date
    qualified: ReviewLaneState
    rejected_audit: ReviewLaneState

    @model_validator(mode="after")
    def require_named_lanes(self) -> DailyReview:
        if self.qualified.lane != "qualified":
            raise ValueError("Qualified review state has the wrong lane")
        if self.rejected_audit.lane != "rejected_audit":
            raise ValueError("Rejected audit state has the wrong lane")
        return self

    @property
    def total(self) -> int:
        return self.qualified.total + self.rejected_audit.total

    @property
    def completed(self) -> int:
        return self.qualified.completed + self.rejected_audit.completed

    @property
    def current(self) -> ReviewItem | None:
        if self.qualified.pending:
            return self.qualified.pending[0]
        if self.rejected_audit.pending:
            return self.rejected_audit.pending[0]
        return None


class ReviewSubmission(ReviewModel):
    review_item_id: UUID
    evaluation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ReviewDecision
    target_profile: TargetProfile
    primary_reason: PrimaryReason
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
