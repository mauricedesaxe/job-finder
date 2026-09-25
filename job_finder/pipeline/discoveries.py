from __future__ import annotations

from datetime import datetime
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from job_finder.jobs.decision_pipeline import job_id_for_url
from job_finder.pipeline.connection import Connection, require_autocommit


class DiscoveryModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class DiscoveryRegistration(DiscoveryModel):
    discovered_count: int = Field(ge=0)
    new_work_count: int = Field(ge=0)
    processed_url_count: int = Field(default=0, ge=0)


def register_discoveries(
    connection: Connection,
    *,
    run_id: UUID,
    keyword: str,
    domain: str,
    raw_urls: tuple[str, ...],
    discovered_at: datetime,
    onboarding_request_key: str | None = None,
    max_new_work: int | None = None,
) -> DiscoveryRegistration:
    require_autocommit(connection)
    if max_new_work is not None and max_new_work < 0:
        raise ValueError("Maximum new work must be nonnegative")
    discovered_count = 0
    new_work_count = 0
    processed_url_count = 0
    with connection.transaction():
        for raw_url in raw_urls:
            if max_new_work is not None and new_work_count >= max_new_work:
                break
            processed_url_count += 1
            job_id = job_id_for_url(raw_url)
            inserted_job = connection.execute(
                """
                INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (raw_url) DO NOTHING
                RETURNING id
                """,
                (job_id, raw_url, discovered_at, discovered_at),
            ).fetchone()
            job_row = connection.execute(
                """
                UPDATE jobs
                SET last_discovered_at = GREATEST(last_discovered_at, %s)
                WHERE raw_url = %s
                RETURNING id
                """,
                (discovered_at, raw_url),
            ).fetchone()
            if job_row is None:
                raise RuntimeError("Registered job could not be loaded")
            job_id = UUID(str(job_row[0]))
            inserted_discovery = connection.execute(
                """
                INSERT INTO job_discoveries (
                  pipeline_run_id, job_id, keyword, domain, discovered_at
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING job_id
                """,
                (run_id, job_id, keyword, domain, discovered_at),
            ).fetchone()
            if inserted_discovery is not None:
                discovered_count += 1
            if inserted_job is not None:
                inserted_work = connection.execute(
                    """
                    INSERT INTO job_work_items (
                      job_id, discovery_run_id, keyword, state, created_at,
                      onboarding_request_key
                    ) VALUES (%s, %s, %s, 'pending', %s, %s)
                    RETURNING job_id
                    """,
                    (job_id, run_id, keyword, discovered_at, onboarding_request_key),
                ).fetchone()
                if inserted_work is not None:
                    new_work_count += 1
    return DiscoveryRegistration(
        discovered_count=discovered_count,
        new_work_count=new_work_count,
        processed_url_count=processed_url_count,
    )
