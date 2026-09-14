"""Backfill thin ATS-hosted snapshots with their canonical posting text and
structured compensation.

Snapshots are immutable observations: a scrape that came back thin (Jina
intermittently returns HTTP 200 with an empty body on JS-rendered ATS pages)
stays as recorded. This script writes the corrected facts to
snapshot_corrections, which the review queue reads instead of the broken
fields. Re-runs converge: corrections are upserted, never duplicated.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import ClassVar

import psycopg
from pydantic import BaseModel, ConfigDict, JsonValue

from job_finder.ats.client import fetch_ats_data
from job_finder.ats.models import AtsAvailable
from job_finder.ats.policy import format_ats_block
from job_finder.config import BackfillSettings
from job_finder.database import apply_migrations
from job_finder.jobs.scraping import detect_source

ATS_SOURCES = frozenset(("lever", "ashbyhq", "greenhouse", "workable"))
WORKER_COUNT = 12


class _Arguments(argparse.Namespace):
    dry_run: bool = False


class _Snapshot(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str
    raw_url: str
    title: str
    body_length: int
    has_compensation: bool


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report planned corrections without writing them",
    )
    arguments = parser.parse_args(argv, namespace=_Arguments())
    dry_run = bool(arguments.dry_run)
    settings = BackfillSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        applied = apply_migrations(connection)
        print(f"migrations applied: {len(applied)}", flush=True)
        rows: list[tuple[object, ...]] = connection.execute(
            """
            SELECT s.id, s.raw_url, s.title, length(s.description),
                   s.compensation_source IS NOT NULL
            FROM job_snapshots s
            ORDER BY s.observed_at
            """
        ).fetchall()
        candidates: list[_Snapshot] = []
        for row in rows:
            raw_url = str(row[1])
            if detect_source(raw_url) not in ATS_SOURCES:
                continue
            candidates.append(
                _Snapshot.model_validate(
                    {
                        "snapshot_id": str(row[0]),
                        "raw_url": raw_url,
                        "title": str(row[2]),
                        "body_length": int(str(row[3])),
                        "has_compensation": bool(row[4]),
                    }
                )
            )
        print(f"snapshots scanned: {len(rows)}, ATS candidates: {len(candidates)}", flush=True)
        ashby_cache: dict[str, JsonValue] = {}
        planned = 0
        written = 0

        def fetch(candidate: _Snapshot) -> tuple[_Snapshot, AtsAvailable | None]:
            evidence = fetch_ats_data(
                candidate.raw_url, title=candidate.title, ashby_cache=ashby_cache
            )
            return candidate, evidence if isinstance(evidence, AtsAvailable) else None

        with ThreadPoolExecutor(max_workers=WORKER_COUNT) as executor:
            for index, (candidate, evidence) in enumerate(executor.map(fetch, candidates), start=1):
                if evidence is None:
                    continue
                correction = _plan_correction(
                    snapshot_id=candidate.snapshot_id,
                    body_length=candidate.body_length,
                    has_compensation=candidate.has_compensation,
                    evidence=evidence,
                    min_body_length=settings.min_body_length,
                )
                if correction is None:
                    continue
                planned += 1
                if dry_run:
                    print(f"would correct {candidate.raw_url}: {correction[8]}", flush=True)
                    continue
                _write_correction(connection, correction)
                written += 1
                if index % 50 == 0:
                    print(f"processed {index}/{len(candidates)}", flush=True)
        mode = "planned" if dry_run else "written"
        print(f"corrections {mode}: {planned if dry_run else written}", flush=True)
    return 0


def _plan_correction(
    *,
    snapshot_id: str,
    body_length: int,
    has_compensation: bool,
    evidence: AtsAvailable,
    min_body_length: int,
) -> tuple[object, ...] | None:
    description: str | None = None
    reason_parts: list[str] = []
    if evidence.description is not None and body_length < min_body_length:
        description = f"{format_ats_block(evidence)}\n\n{evidence.description}"
        reason_parts.append(f"description {body_length} -> {len(description)} chars")
    compensation = evidence.compensation
    fields: tuple[object, ...] = (None, None, None, None, None)
    if compensation is not None and not has_compensation:
        if compensation.minimum is not None or compensation.maximum is not None:
            fields = (
                compensation.minimum,
                compensation.maximum,
                compensation.currency,
                compensation.period,
                "ats",
            )
            reason_parts.append("compensation added")
    if description is None and fields[4] is None:
        return None
    reason = "; ".join(reason_parts) or "canonical ATS data"
    return (
        snapshot_id,
        description,
        *fields,
        reason,
        datetime.now(UTC),
    )


def _write_correction(
    connection: psycopg.Connection[tuple[object, ...]], correction: tuple[object, ...]
) -> None:
    _ = connection.execute(
        """
        INSERT INTO snapshot_corrections (
          snapshot_id, description, compensation_min, compensation_max,
          compensation_currency, compensation_period, compensation_source,
          reason, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (snapshot_id) DO UPDATE
        SET description = EXCLUDED.description,
            compensation_min = EXCLUDED.compensation_min,
            compensation_max = EXCLUDED.compensation_max,
            compensation_currency = EXCLUDED.compensation_currency,
            compensation_period = EXCLUDED.compensation_period,
            compensation_source = EXCLUDED.compensation_source,
            reason = EXCLUDED.reason,
            created_at = EXCLUDED.created_at
        """,
        correction,
    )


if __name__ == "__main__":
    raise SystemExit(main())
