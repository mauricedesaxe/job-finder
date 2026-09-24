from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, ClassVar, Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_finder.database import Connection
from job_finder.evaluation.models import EvaluationOutcome

_Digest = str


class ManifestOperationError(ValueError):
    pass


class _ManifestModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class ManifestPolicy(_ManifestModel):
    regular_trial_count: int = Field(default=1, gt=0)
    critical_trial_count: int = Field(default=3, gt=1)
    max_false_positive_rate: Decimal = Field(default=Decimal("0.05"), ge=0, le=1)
    max_false_negative_rate: Decimal = Field(default=Decimal("0.10"), ge=0, le=1)

    @model_validator(mode="after")
    def thresholds_and_trials_are_ordered(self) -> Self:
        if self.critical_trial_count <= self.regular_trial_count:
            raise ValueError("Critical cases must run more trials than regular cases")
        if self.max_false_positive_rate >= self.max_false_negative_rate:
            raise ValueError("The false-positive threshold must be stricter")
        return self


class EvaluationCaseInput(_ManifestModel):
    title: str
    company: str
    url: str
    source: str
    description: str
    location: str
    keywords: tuple[str, ...]
    date_posted: date | None
    observed_at: datetime
    original_outcome: EvaluationOutcome
    review_decision: Literal["pursue", "reject"]
    target_profile: str | None


class CuratedReviewEvent(_ManifestModel):
    id: UUID
    review_event_id: UUID
    action: Literal["include", "exclude"]
    expected_outcome: EvaluationOutcome | None
    critical: bool
    reason: str
    actor: str
    created_at: datetime

    @model_validator(mode="after")
    def action_has_valid_fields(self) -> Self:
        if self.action == "include" and self.expected_outcome is None:
            raise ValueError("Included feedback requires an expected outcome")
        if self.action == "exclude" and (self.expected_outcome is not None or self.critical):
            raise ValueError("Excluded feedback cannot define evaluation behavior")
        return self


class EvaluationManifestCase(_ManifestModel):
    position: int = Field(ge=0)
    curation_id: UUID
    review_event_id: UUID
    expected_outcome: EvaluationOutcome
    critical: bool
    trial_count: int = Field(gt=0)
    input: EvaluationCaseInput


class EvaluationManifest(_ManifestModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: ManifestPolicy
    cases: tuple[EvaluationManifestCase, ...]
    created_at: datetime
    created_by: str

    @model_validator(mode="after")
    def cases_follow_policy(self) -> Self:
        if not self.cases:
            raise ValueError("An evaluation manifest requires at least one case")
        for position, case in enumerate(self.cases):
            expected_trials = (
                self.policy.critical_trial_count
                if case.critical
                else self.policy.regular_trial_count
            )
            if case.position != position or case.trial_count != expected_trials:
                raise ValueError("Manifest cases must be ordered and follow the trial policy")
        return self


class ManifestSummary(_ManifestModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: ManifestPolicy
    case_count: int = Field(ge=0)
    qualified_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    critical_count: int = Field(ge=0)
    trial_count: int = Field(ge=0)
    created_at: datetime | None = None
    created_by: str | None = None


class ManifestSummaryPage(_ManifestModel):
    items: Annotated[tuple[ManifestSummary, ...], Field(max_length=100)]
    next_offset: int | None = Field(default=None, ge=0)


def include_review_event(
    connection: Connection,
    *,
    review_event_id: UUID,
    critical: bool,
    reason: str,
    actor: str,
    created_at: datetime,
    idempotency_key: str,
) -> CuratedReviewEvent:
    _require_autocommit(connection)
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"evaluation_curation:{idempotency_key}",),
        )
        existing = _load_curation_by_key(connection, idempotency_key)
        if existing is not None:
            _require_matching_curation(
                existing, review_event_id, "include", critical, reason, actor
            )
            return existing
        row = connection.execute(
            "SELECT decision FROM review_events WHERE id = %s",
            (review_event_id,),
        ).fetchone()
        if row is None:
            raise ManifestOperationError("Review event does not exist")
        decision = str(row[0])
        if decision == "unsure":
            raise ManifestOperationError("Unsure feedback cannot define an evaluation expectation")
        expected: EvaluationOutcome = "qualified" if decision == "pursue" else "rejected"
        curation = CuratedReviewEvent(
            id=uuid5(NAMESPACE_URL, f"evaluation-curation:{idempotency_key}"),
            review_event_id=review_event_id,
            action="include",
            expected_outcome=expected,
            critical=critical,
            reason=reason,
            actor=actor,
            created_at=created_at,
        )
        _insert_curation(connection, idempotency_key, curation)
        return curation


def exclude_review_event(
    connection: Connection,
    *,
    review_event_id: UUID,
    reason: str,
    actor: str,
    created_at: datetime,
    idempotency_key: str,
) -> CuratedReviewEvent:
    _require_autocommit(connection)
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"evaluation_curation:{idempotency_key}",),
        )
        existing = _load_curation_by_key(connection, idempotency_key)
        if existing is not None:
            _require_matching_curation(existing, review_event_id, "exclude", False, reason, actor)
            return existing
        exists = connection.execute(
            "SELECT 1 FROM review_events WHERE id = %s",
            (review_event_id,),
        ).fetchone()
        if exists is None:
            raise ManifestOperationError("Review event does not exist")
        curation = CuratedReviewEvent(
            id=uuid5(NAMESPACE_URL, f"evaluation-curation:{idempotency_key}"),
            review_event_id=review_event_id,
            action="exclude",
            expected_outcome=None,
            critical=False,
            reason=reason,
            actor=actor,
            created_at=created_at,
        )
        _insert_curation(connection, idempotency_key, curation)
        return curation


def create_manifest(
    connection: Connection,
    *,
    policy: ManifestPolicy,
    created_at: datetime,
    created_by: str,
    idempotency_key: str,
) -> EvaluationManifest:
    _require_autocommit(connection)
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"evaluation_manifest:{idempotency_key}",),
        )
        existing = _load_manifest_request(connection, idempotency_key)
        if existing is not None:
            manifest = load_manifest(connection, existing[0])
            if manifest.policy != policy or existing[1] != created_by:
                raise ManifestOperationError(
                    "Idempotency key belongs to a different manifest request"
                )
            return manifest
        _ = connection.execute("LOCK TABLE evaluation_case_curations IN SHARE MODE")
        cases = _load_current_cases(connection, policy)
        if not cases:
            raise ManifestOperationError(
                "An evaluation manifest requires at least one included case"
            )
        content = {
            "policy": policy.model_dump(mode="json"),
            "cases": [case.model_dump(mode="json") for case in cases],
        }
        digest = _digest(content)
        inserted = connection.execute(
            """
            INSERT INTO evaluation_manifests (
              id, content_digest, expected_case_count, regular_trial_count,
              critical_trial_count, max_false_positive_rate,
              max_false_negative_rate, created_at, created_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (content_digest) DO NOTHING
            RETURNING id
            """,
            (
                digest,
                digest,
                len(cases),
                policy.regular_trial_count,
                policy.critical_trial_count,
                policy.max_false_positive_rate,
                policy.max_false_negative_rate,
                created_at,
                created_by,
            ),
        ).fetchone()
        if inserted is None:
            manifest = load_manifest(connection, digest)
        else:
            for case in cases:
                _ = connection.execute(
                    """
                    INSERT INTO evaluation_manifest_cases (
                      manifest_id, position, curation_id, review_event_id,
                      expected_outcome, critical, trial_count, input
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        digest,
                        case.position,
                        case.curation_id,
                        case.review_event_id,
                        case.expected_outcome,
                        case.critical,
                        case.trial_count,
                        Jsonb(case.input.model_dump(mode="json")),
                    ),
                )
            manifest = EvaluationManifest(
                id=digest,
                policy=policy,
                cases=cases,
                created_at=created_at,
                created_by=created_by,
            )
            enqueue_projection(connection, "evaluation_manifest", digest, manifest, created_at)
        _ = connection.execute(
            """
            INSERT INTO evaluation_manifest_requests (
              idempotency_key, manifest_id, created_by, created_at
            ) VALUES (%s, %s, %s, %s)
            """,
            (idempotency_key, manifest.id, created_by, created_at),
        )
        return manifest


def preview_manifest(connection: Connection, policy: ManifestPolicy) -> ManifestSummary:
    _require_autocommit(connection)
    return _summarize_manifest_cases("0" * 64, policy, _load_current_cases(connection, policy))


def list_manifests(
    connection: Connection,
    *,
    limit: int = 25,
    offset: int = 0,
) -> ManifestSummaryPage:
    _require_autocommit(connection)
    if limit < 1 or limit > 100:
        raise ValueError("Manifest page size must be between 1 and 100")
    if offset < 0:
        raise ValueError("Manifest offset cannot be negative")
    rows = connection.execute(
        """
        SELECT m.id, m.regular_trial_count, m.critical_trial_count,
               m.max_false_positive_rate, m.max_false_negative_rate,
               m.expected_case_count,
               count(*) FILTER (WHERE c.expected_outcome = 'qualified'),
               count(*) FILTER (WHERE c.expected_outcome = 'rejected'),
               count(*) FILTER (WHERE c.critical),
               COALESCE(sum(c.trial_count), 0), m.created_at, m.created_by
        FROM evaluation_manifests m
        JOIN evaluation_manifest_cases c ON c.manifest_id = m.id
        GROUP BY m.id
        ORDER BY m.created_at DESC, m.id DESC
        LIMIT %s OFFSET %s
        """,
        (limit + 1, offset),
    ).fetchall()
    items = tuple(_parse_manifest_summary(row) for row in rows[:limit])
    return ManifestSummaryPage(
        items=items,
        next_offset=offset + limit if len(rows) > limit else None,
    )


def load_manifest(connection: Connection, manifest_id: _Digest) -> EvaluationManifest:
    row = connection.execute(
        """
        SELECT regular_trial_count, critical_trial_count, max_false_positive_rate,
               max_false_negative_rate, created_at, created_by
        FROM evaluation_manifests WHERE id = %s
        """,
        (manifest_id,),
    ).fetchone()
    if row is None:
        raise ManifestOperationError("Evaluation manifest does not exist")
    case_rows = connection.execute(
        """
        SELECT position, curation_id, review_event_id, expected_outcome,
               critical, trial_count, input
        FROM evaluation_manifest_cases
        WHERE manifest_id = %s ORDER BY position
        """,
        (manifest_id,),
    ).fetchall()
    return EvaluationManifest(
        id=manifest_id,
        policy=ManifestPolicy(
            regular_trial_count=int(str(row[0])),
            critical_trial_count=int(str(row[1])),
            max_false_positive_rate=Decimal(str(row[2])),
            max_false_negative_rate=Decimal(str(row[3])),
        ),
        cases=tuple(
            EvaluationManifestCase.model_validate(
                {
                    "position": case[0],
                    "curation_id": case[1],
                    "review_event_id": case[2],
                    "expected_outcome": case[3],
                    "critical": case[4],
                    "trial_count": case[5],
                    "input": case[6],
                }
            )
            for case in case_rows
        ),
        created_at=datetime.fromisoformat(str(row[4])),
        created_by=str(row[5]),
    )


def summarize_manifest(manifest: EvaluationManifest) -> ManifestSummary:
    return _summarize_manifest_cases(
        manifest.id,
        manifest.policy,
        manifest.cases,
        created_at=manifest.created_at,
        created_by=manifest.created_by,
    )


def _load_current_cases(
    connection: Connection, policy: ManifestPolicy
) -> tuple[EvaluationManifestCase, ...]:
    rows = connection.execute(
        """
        WITH current_curations AS (
          SELECT DISTINCT ON (review_event_id) *
          FROM evaluation_case_curations
          ORDER BY review_event_id, created_at DESC, id DESC
        )
        SELECT c.id, c.review_event_id, c.expected_outcome, c.critical,
               s.title, s.company, s.raw_url, s.source,
               COALESCE(sc.description, s.description), s.location,
               s.keywords, s.date_posted, s.observed_at, d.outcome,
               e.decision, e.target_profile
        FROM current_curations c
        JOIN review_events e ON e.id = c.review_event_id
        JOIN review_items i ON i.id = e.review_item_id
        JOIN evaluation_decisions d ON d.id = i.evaluation_id
        JOIN job_snapshots s ON s.id = d.snapshot_id
        LEFT JOIN snapshot_corrections sc ON sc.snapshot_id = s.id
        WHERE c.action = 'include'
          AND NOT EXISTS (
            SELECT 1 FROM review_events newer
            WHERE newer.review_item_id = e.review_item_id
              AND (newer.created_at, newer.id) > (e.created_at, e.id)
          )
        ORDER BY c.review_event_id
        """
    ).fetchall()
    cases: list[EvaluationManifestCase] = []
    for position, row in enumerate(rows):
        critical = bool(row[3])
        cases.append(
            EvaluationManifestCase.model_validate(
                {
                    "position": position,
                    "curation_id": row[0],
                    "review_event_id": row[1],
                    "expected_outcome": row[2],
                    "critical": critical,
                    "trial_count": (
                        policy.critical_trial_count if critical else policy.regular_trial_count
                    ),
                    "input": {
                        "title": row[4],
                        "company": row[5],
                        "url": row[6],
                        "source": row[7],
                        "description": row[8],
                        "location": row[9],
                        "keywords": row[10],
                        "date_posted": row[11],
                        "observed_at": row[12],
                        "original_outcome": row[13],
                        "review_decision": row[14],
                        "target_profile": row[15],
                    },
                }
            )
        )
    return tuple(cases)


def _summarize_manifest_cases(
    manifest_id: str,
    policy: ManifestPolicy,
    cases: tuple[EvaluationManifestCase, ...],
    *,
    created_at: datetime | None = None,
    created_by: str | None = None,
) -> ManifestSummary:
    return ManifestSummary(
        id=manifest_id,
        policy=policy,
        case_count=len(cases),
        qualified_count=sum(case.expected_outcome == "qualified" for case in cases),
        rejected_count=sum(case.expected_outcome == "rejected" for case in cases),
        critical_count=sum(case.critical for case in cases),
        trial_count=sum(case.trial_count for case in cases),
        created_at=created_at,
        created_by=created_by,
    )


def _parse_manifest_summary(row: tuple[object, ...]) -> ManifestSummary:
    return ManifestSummary(
        id=str(row[0]),
        policy=ManifestPolicy(
            regular_trial_count=int(str(row[1])),
            critical_trial_count=int(str(row[2])),
            max_false_positive_rate=Decimal(str(row[3])),
            max_false_negative_rate=Decimal(str(row[4])),
        ),
        case_count=int(str(row[5])),
        qualified_count=int(str(row[6])),
        rejected_count=int(str(row[7])),
        critical_count=int(str(row[8])),
        trial_count=int(str(row[9])),
        created_at=datetime.fromisoformat(str(row[10])),
        created_by=str(row[11]),
    )


def _insert_curation(
    connection: Connection, idempotency_key: str, curation: CuratedReviewEvent
) -> None:
    _ = connection.execute(
        """
        INSERT INTO evaluation_case_curations (
          id, idempotency_key, review_event_id, action, expected_outcome,
          critical, reason, actor, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            curation.id,
            idempotency_key,
            curation.review_event_id,
            curation.action,
            curation.expected_outcome,
            curation.critical,
            curation.reason,
            curation.actor,
            curation.created_at,
        ),
    )


def _load_curation_by_key(
    connection: Connection, idempotency_key: str
) -> CuratedReviewEvent | None:
    row = connection.execute(
        """
        SELECT id, review_event_id, action, expected_outcome, critical,
               reason, actor, created_at
        FROM evaluation_case_curations WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return CuratedReviewEvent.model_validate(
        {
            "id": row[0],
            "review_event_id": row[1],
            "action": row[2],
            "expected_outcome": row[3],
            "critical": row[4],
            "reason": row[5],
            "actor": row[6],
            "created_at": row[7],
        }
    )


def _load_manifest_request(connection: Connection, idempotency_key: str) -> tuple[str, str] | None:
    row = connection.execute(
        """
        SELECT manifest_id, created_by
        FROM evaluation_manifest_requests
        WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def _require_matching_curation(
    existing: CuratedReviewEvent,
    review_event_id: UUID,
    action: Literal["include", "exclude"],
    critical: bool,
    reason: str,
    actor: str,
) -> None:
    if (
        existing.review_event_id != review_event_id
        or existing.action != action
        or existing.critical != critical
        or existing.reason != reason
        or existing.actor != actor
    ):
        raise ManifestOperationError("Idempotency key belongs to a different curation command")


def enqueue_projection(
    connection: Connection,
    kind: str,
    source_id: str,
    payload: BaseModel,
    created_at: datetime,
) -> None:
    data = payload.model_dump(mode="json")
    payload_digest = _digest(data)
    projection_id = _digest({"kind": kind, "source_id": source_id})
    _ = connection.execute(
        """
        INSERT INTO langfuse_projection_items (
          id, kind, source_id, payload_digest, payload, state, created_at
        ) VALUES (%s, %s, %s, %s, %s, 'pending', %s)
        ON CONFLICT (kind, source_id) DO NOTHING
        """,
        (projection_id, kind, source_id, payload_digest, Jsonb(data), created_at),
    )


def _digest(value: object) -> _Digest:
    content = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(content.encode()).hexdigest()


def _require_autocommit(connection: Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Evaluation manifest operations require an autocommit connection")
