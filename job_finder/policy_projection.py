from __future__ import annotations

from dataclasses import dataclass

from job_finder.acquisition_policy import AcquisitionPolicy
from job_finder.qualification_definition import QualificationDefinition
from job_finder.search_configuration import SearchConfiguration


@dataclass(frozen=True, slots=True)
class LegacyPolicyProjection:
    acquisition: AcquisitionPolicy
    qualification: QualificationDefinition


def project_legacy_search_configuration(
    configuration: SearchConfiguration,
) -> LegacyPolicyProjection:
    return LegacyPolicyProjection(
        acquisition=AcquisitionPolicy(
            search_keywords=configuration.search_keywords,
            enabled_sources=configuration.enabled_sources,
        ),
        qualification=QualificationDefinition(
            personal_criteria=configuration.personal_criteria,
            target_profiles=configuration.target_profiles,
        ),
    )
