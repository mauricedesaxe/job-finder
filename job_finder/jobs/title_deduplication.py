from __future__ import annotations

from pydantic import Field

from job_finder.evaluation.models import (
    CriterionUnavailable,
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


class TitleDuplicate(EvaluationModel):
    is_duplicate: bool = Field(alias="isDuplicate")
    matched_title: str | None = Field(default=None, alias="matchedTitle")


def deduplicate_title(
    new_title: str,
    existing_titles: tuple[str, ...],
    release: PromptRelease,
    context: ModelCallContext,
    persistence: ModelCallPersistence,
    *,
    api_key: str,
    sender: ChatCompletionSender | None = None,
    retry_policy: RetryPolicy | None = None,
) -> PromptAccepted[TitleDuplicate] | CriterionUnavailable:
    prompt_name = "job-finder-title-deduplication"
    if not existing_titles:
        return PromptAccepted(prompt_name=prompt_name, output=TitleDuplicate(isDuplicate=False))
    exact_title = next(
        (title for title in existing_titles if title.strip().lower() == new_title.strip().lower()),
        None,
    )
    if exact_title is not None:
        return PromptAccepted(
            prompt_name=prompt_name,
            output=TitleDuplicate(isDuplicate=True, matchedTitle=exact_title),
        )
    values = {
        "newTitle": new_title,
        "existingTitles": "\n".join(
            f'{index}. "{title}"' for index, title in enumerate(existing_titles, start=1)
        ),
    }
    return invoke_prompt(
        release.version(prompt_name),
        values,
        context,
        persistence,
        TitleDuplicate,
        api_key=api_key,
        sender=sender,
        retry_policy=retry_policy,
    )
