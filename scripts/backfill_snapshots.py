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
import hashlib
import time
from collections.abc import Sequence
from datetime import UTC, datetime

import psycopg
from pydantic import JsonValue

from job_finder.ats.client import fetch_ats_data
from job_finder.ats.models import AtsAvailable
from job_finder.ats.policy import format_ats_block
from job_finder.config import BackfillSettings
from job_finder.database import apply_migrations
from job_finder.jobs.scraping import detect_source

ATS_SOURCES = frozenset(("lever", "ashbyhq", "greenhouse", "workable"))


class _Arguments(argparse.Namespace):
    dry_run: bool = False


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
    ashby_cache: dict[str, JsonValue] = {}
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        applied = apply_migrations(connection)
        print(f"migrations applied: {len(applied)}")
        rows: list[tuple[object, ...]] = connection.execute(
            """
            SELECT s.id, s.raw_url, s.title, length(s.description),
                   s.compensation_source IS NOT NULL
            FROM job_snapshots s
            ORDER BY s.observed_at
            """
        ).fetchall()
        print(f"snapshots scanned: {len(rows)}")
        planned = 0
        written = 0
        for row in rows:
            snapshot_id = str(row[0])
            raw_url = str(row[1])
            title = str(row[2])
            body_length = int(str(row[3]))
            has_compensation = bool(row[4])
            if detect_source(raw_url) not in ATS_SOURCES:
                continue
            evidence = fetch_ats_data(raw_url, title=title, ashby_cache=ashby_cache)
            if not isinstance(evidence, AtsAvailable):
                continue
            correction = _plan_correction(
                snapshot_id=snapshot_id,
                body_length=body_length,
                has_compensation=has_compensation,
                evidence=evidence,
                min_body_length=settings.min_body_length,
            )
            if correction is None:
                continue
            planned += 1
            if dry_run:
                print(f"would correct {raw_url}: {correction[9]}")
                continue
            _write_correction(connection, correction)
            written += 1
            time.sleep(0.05)
        mode = "planned" if dry_run else "written"
        print(f"corrections {mode}: {planned if dry_run else written}")
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
        _correction_id(snapshot_id),
        snapshot_id,
        description,
        *fields,
        reason,
        datetime.now(UTC),
    )


def _correction_id(snapshot_id: str) -> str:
    return hashlib.sha256(f"snapshot-correction:{snapshot_id}".encode()).hexdigest()


def _write_correction(
    connection: psycopg.Connection[tuple[object, ...]], correction: tuple[object, ...]
) -> None:
    _ = connection.execute(
        """
        INSERT INTO snapshot_corrections (
          id, snapshot_id, description, compensation_min, compensation_max,
          compensation_currency, compensation_period, compensation_source,
          reason, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
