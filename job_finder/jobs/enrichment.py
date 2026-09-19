from __future__ import annotations

from collections.abc import Mapping

from job_finder.ats.models import CompensationObservation
from job_finder.evaluation.models import (
    OperationalFailure,
    EvaluationModel,
    ModelCallContext,
    PromptAccepted,
)
from job_finder.evaluation.openrouter import (
    ChatCompletionSender,
    ModelCallPersistence,
    RetryPolicy,
    invoke_prompt,
)
from job_finder.evaluation.prompt_releases import PromptRelease
from job_finder.jobs.models import JobListing


class EnrichedJob(EvaluationModel):
    title: str
    company: str
    description: str
    location: str
    compensation: CompensationObservation | None = None


def enrich_job(
    job: JobListing,
    release: PromptRelease,
    context: ModelCallContext,
    persistence: ModelCallPersistence,
    *,
    api_key: str,
    sender: ChatCompletionSender | None = None,
    retry_policy: RetryPolicy | None = None,
) -> PromptAccepted[EnrichedJob] | OperationalFailure:
    values = enrichment_values(job)
    return invoke_prompt(
        release.version("job-finder-enrichment"),
        values,
        context,
        persistence,
        EnrichedJob,
        api_key=api_key,
        sender=sender,
        retry_policy=retry_policy,
    )


def enrichment_values(job: JobListing) -> dict[str, str]:
    fields: Mapping[str, str] = {
        "Job Title": job.title,
        "Company": job.company,
        "Source": job.source,
        "URL": job.url,
    }
    header = "\n".join(f"{name}: {value}" for name, value in fields.items())
    return {"job": f"{header}\n\nRaw Description:\n{job.description}"}
