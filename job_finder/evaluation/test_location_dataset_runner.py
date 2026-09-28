from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

import pytest
from langfuse.api import DatasetItem, DatasetStatus
from langfuse.experiment import ExperimentItemResult

from scripts.evaluate_location_dataset import main
from scripts.evaluate_location_dataset import _counts, _exact_match  # pyright: ignore[reportPrivateUsage]


def test_help_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.evaluate_location_dataset", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--minimum-rejection-rate" in result.stdout
    assert "--dataset" in result.stdout


@pytest.mark.parametrize("rate", ("-0.5", "1.5"))
def test_rejects_an_out_of_range_minimum_rejection_rate(rate: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        _ = main(
            [
                "--dataset",
                "locations",
                "--run-name",
                "run",
                "--minimum-rejection-rate",
                rate,
            ]
        )

    assert exit_info.value.code == 2


def test_counts_rejection_recall_and_kept_controls_from_experiment_results() -> None:
    results = (
        _experiment_result(expected=False, observed=False),
        _experiment_result(expected=False, observed=True),
        _experiment_result(expected=True, observed=True),
        _experiment_result(expected=True, observed=False),
    )

    assert _counts(results) == (1, 2, 1, 2)


def test_counts_an_all_good_run_for_the_exit_success_path() -> None:
    results = (
        _experiment_result(expected=False, observed=False),
        _experiment_result(expected=False, observed=False),
        _experiment_result(expected=False, observed=False),
        _experiment_result(expected=False, observed=False),
        _experiment_result(expected=True, observed=True),
    )

    negative_caught, negative_total, positive_kept, positive_total = _counts(results)

    assert (negative_caught, negative_total, positive_kept, positive_total) == (4, 4, 1, 1)
    assert negative_caught / negative_total >= 0.8
    assert positive_kept == positive_total


def test_counts_a_recall_regression_below_the_minimum_rejection_rate() -> None:
    results = (
        _experiment_result(expected=False, observed=False),
        _experiment_result(expected=False, observed=False),
        _experiment_result(expected=False, observed=False),
        _experiment_result(expected=False, observed=True),
    )

    negative_caught, negative_total, positive_kept, positive_total = _counts(results)

    assert (negative_caught, negative_total, positive_kept, positive_total) == (3, 4, 0, 0)
    assert negative_caught / negative_total < 0.8


def test_counts_a_dropped_remote_eligible_control() -> None:
    results = (
        _experiment_result(expected=False, observed=False),
        _experiment_result(expected=True, observed=True),
        _experiment_result(expected=True, observed=False),
    )

    negative_caught, negative_total, positive_kept, positive_total = _counts(results)

    assert (negative_caught, negative_total, positive_kept, positive_total) == (1, 1, 1, 2)
    assert positive_kept != positive_total


def test_counts_a_run_without_negative_cases() -> None:
    results = (
        _experiment_result(expected=True, observed=True),
        _experiment_result(expected=True, observed=True),
    )

    assert _counts(results) == (0, 0, 2, 2)


def test_counts_requires_an_output_for_every_case() -> None:
    failed = ExperimentItemResult(
        item=_dataset_item(expected=True),
        output=None,
        evaluations=[],
        trace_id=None,
        dataset_run_id=None,
    )

    with pytest.raises(RuntimeError, match="failed without an output"):
        _ = _counts((failed,))


def test_exact_match_scores_observed_eligibility_against_the_expected_outcome() -> None:
    matched = _exact_match(
        input={},
        output={"eligible_remote_from_romania": False, "route": "ats_structural", "reason": ""},
        expected_output={"eligible_remote_from_romania": False},
        metadata=None,
    )
    mismatched = _exact_match(
        input={},
        output={"eligible_remote_from_romania": True, "route": "jev_atomic", "reason": ""},
        expected_output={"eligible_remote_from_romania": False},
        metadata=None,
    )

    assert matched.value == 1.0
    assert mismatched.value == 0.0
    assert mismatched.name == "location_eligibility_correct"


def _dataset_item(expected: bool) -> DatasetItem:
    return DatasetItem(
        id=f"item-{expected}",
        status=DatasetStatus.ACTIVE,
        input={},
        expected_output={"eligible_remote_from_romania": expected},
        metadata={},
        dataset_id="dataset",
        dataset_name="locations",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        updated_at=datetime(2026, 9, 1, tzinfo=UTC),
        media_references=[],
    )


def _experiment_result(*, expected: bool, observed: bool) -> ExperimentItemResult:
    return ExperimentItemResult(
        item=_dataset_item(expected),
        output={
            "eligible_remote_from_romania": observed,
            "route": "jev_atomic",
            "reason": "",
        },
        evaluations=[],
        trace_id=None,
        dataset_run_id=None,
    )
