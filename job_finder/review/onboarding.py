from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from job_finder.configuration_service import (
    ActivateConfigurationCommand,
    ActivateConfigurationResult,
    ConfigurationActivated,
    activate_search_configuration,
)
from job_finder.database import ConnectionFactory
from job_finder.review.owner_access import OnboardingStage


@dataclass(frozen=True)
class OnboardingProgressService:
    activate_preferences: Callable[[ActivateConfigurationCommand], ActivateConfigurationResult]


def postgres_onboarding_progress_service(
    connect: ConnectionFactory,
) -> OnboardingProgressService:
    def activate_preferences(
        command: ActivateConfigurationCommand,
    ) -> ActivateConfigurationResult:
        with connect() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT stage
                FROM owner_onboarding
                WHERE singleton_id = 1
                FOR UPDATE
                """
            ).fetchone()
            if row is None:
                raise RuntimeError("Owner onboarding state is missing")
            stage = OnboardingStage(str(row[0]))
            result = activate_search_configuration(connection, command)
            if not isinstance(result, ConfigurationActivated):
                return result
            if stage is OnboardingStage.PREFERENCES:
                _ = connection.execute(
                    """
                    UPDATE owner_onboarding
                    SET stage = 'budget', updated_at = CURRENT_TIMESTAMP
                    WHERE singleton_id = 1 AND stage = 'preferences'
                    """
                )
        return result

    return OnboardingProgressService(activate_preferences=activate_preferences)
