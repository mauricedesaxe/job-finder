from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from job_finder.database import INITIAL_SEARCH_CONFIGURATION_REVISION_ID
from job_finder.evaluation.models import PromptReleaseId
from job_finder.search_configuration import (
    DEFAULT_SEARCH_CONFIGURATION,
    PersonalCriterion,
    SearchConfiguration,
    SearchConfigurationPublication,
    SearchConfigurationRevisionId,
    SupportedSearchSource,
    TargetProfile,
    build_search_configuration_revision,
    search_configuration_revision_id,
)

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


def test_publication_model_validates_immutable_binding_shape() -> None:
    publication = SearchConfigurationPublication(
        revision_id=SearchConfigurationRevisionId("1" * 64),
        prompt_release_id=PromptReleaseId("2" * 64),
        published_at=NOW,
        published_by="owner",
    )

    assert publication.model_dump() == {
        "revision_id": "1" * 64,
        "prompt_release_id": "2" * 64,
        "published_at": NOW,
        "published_by": "owner",
    }
    with pytest.raises(ValidationError, match="frozen"):
        setattr(publication, "published_by", "other")


def test_default_configuration_reproduces_the_source_catalog() -> None:
    configuration = DEFAULT_SEARCH_CONFIGURATION

    assert len(configuration.search_keywords) == 32
    assert configuration.enabled_sources == (
        SupportedSearchSource.ASHBY,
        SupportedSearchSource.LEVER,
        SupportedSearchSource.GREENHOUSE,
        SupportedSearchSource.WORKABLE,
    )
    assert tuple(item.key for item in configuration.personal_criteria) == (
        "remote-europe-eligible",
        "compensation-minimum",
        "role-quality",
        "cheap-shop-placement",
    )
    assert tuple(item.key for item in configuration.target_profiles) == (
        "early-stage-product-engineer",
        "applied-ai-product-engineer",
    )
    assert (
        search_configuration_revision_id(configuration) == INITIAL_SEARCH_CONFIGURATION_REVISION_ID
    )


def test_revision_identity_depends_only_on_exact_ordered_content() -> None:
    first = build_search_configuration_revision(
        DEFAULT_SEARCH_CONFIGURATION,
        created_at=NOW,
        created_by="owner",
    )
    repeated = build_search_configuration_revision(
        DEFAULT_SEARCH_CONFIGURATION,
        created_at=NOW.replace(hour=13),
        created_by="other",
    )
    reordered = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "target_profiles": tuple(reversed(DEFAULT_SEARCH_CONFIGURATION.target_profiles)),
        }
    )

    assert first.id == repeated.id
    assert first.id != search_configuration_revision_id(reordered)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("search_keywords", ()),
        ("enabled_sources", ()),
        ("personal_criteria", ()),
        ("target_profiles", ()),
        ("search_keywords", ("python", "Python")),
        ("enabled_sources", (SupportedSearchSource.LEVER, SupportedSearchSource.LEVER)),
    ],
)
def test_configuration_rejects_empty_or_duplicate_members(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        SearchConfiguration.model_validate(
            {**DEFAULT_SEARCH_CONFIGURATION.model_dump(), field: value}
        )


def test_configuration_rejects_duplicate_criterion_and_profile_keys() -> None:
    criterion = PersonalCriterion(key="one", name="One", instructions="Accept one.")
    profile = TargetProfile(key="one", name="One", instructions="Match one.")

    with pytest.raises(ValidationError, match="criterion keys"):
        SearchConfiguration.model_validate(
            {
                **DEFAULT_SEARCH_CONFIGURATION.model_dump(),
                "personal_criteria": (criterion, criterion),
            }
        )

    with pytest.raises(ValidationError, match="profile keys"):
        SearchConfiguration.model_validate(
            {
                **DEFAULT_SEARCH_CONFIGURATION.model_dump(),
                "target_profiles": (profile, profile),
            }
        )


def test_configuration_rejects_untrimmed_text_and_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="whitespace"):
        PersonalCriterion(key="location", name=" Location", instructions="Accept Europe.")

    with pytest.raises(ValidationError, match="extra"):
        SearchConfiguration.model_validate(
            {**DEFAULT_SEARCH_CONFIGURATION.model_dump(), "unknown": True}
        )


@pytest.mark.parametrize("invalid_character", ["\x00", "\ud800"])
def test_configuration_rejects_text_postgres_cannot_store(invalid_character: str) -> None:
    with pytest.raises(ValidationError, match="PostgreSQL|unicode"):
        PersonalCriterion(
            key="location",
            name=f"Location{invalid_character}",
            instructions="Accept Europe.",
        )
