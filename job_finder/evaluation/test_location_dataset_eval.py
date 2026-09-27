from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

import pytest
from langfuse.api import DatasetItem
from langfuse.api.commons.types.dataset_item import DatasetStatus
from langfuse.experiment import ExperimentItemResult

import scripts.evaluate_location_dataset as location_dataset
from scripts.evaluate_location_dataset import ExpectedLocationOutcome, _counts, main

_STAMP = datetime(2026, 1, 1, 12, 0, 0)
_IDS = itertools.count()


def _dataset_item(eligible: bool | None) -> DatasetItem:
    return DatasetItem(
        id=f"item-{next(_IDS)}",
        status=DatasetStatus.ACTIVE,
        input={},
        expected_output=None if eligible is None else {"eligible_remote_from_romania": eligible},
        metadata=None,
        dataset_id="dataset",
        dataset_name="location",
        created_at=_STAMP,
        updated_at=_STAMP,
        media_references=[],
    )


def _result(eligible: bool, observed: bool | None) -> ExperimentItemResult:
    return ExperimentItemResult(
        item=_dataset_item(eligible),
        output=None if observed is None else {"eligible_remote_from_romania": observed},
        evaluations=[],
        trace_id=None,
        dataset_run_id=None,
    )


@dataclass(frozen=True)
class _DatasetHandle:
    items: tuple[DatasetItem, ...]


@dataclass(frozen=True)
class _ExperimentRun:
    item_results: tuple[ExperimentItemResult, ...]
    dataset_run_url: str


def _fake_langfuse(
    items: Sequence[DatasetItem], simulate: Callable[[DatasetItem], bool]
) -> tuple[type, list[list[DatasetItem]]]:
    received: list[list[DatasetItem]] = []

    class _Client:
        def __init__(self, *, public_key: str, secret_key: str, host: str) -> None:
            _ = (public_key, secret_key, host)

        def get_dataset(self, name: str) -> _DatasetHandle:
            _ = name
            return _DatasetHandle(items=tuple(items))

        def run_experiment(self, *, data: list[DatasetItem], **kwargs: object) -> _ExperimentRun:
            _ = kwargs
            received.append(list(data))
            return _ExperimentRun(
                item_results=tuple(
                    ExperimentItemResult(
                        item=item,
                        output={"eligible_remote_from_romania": simulate(item)},
                        evaluations=[],
                        trace_id=None,
                        dataset_run_id=None,
                    )
                    for item in data
                ),
                dataset_run_url="https://langfuse.test/run",
            )

    return _Client, received


def _authorize(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public-key")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret-key")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.test")
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-key")


def _mirror_expected(item: DatasetItem) -> bool:
    assert item.expected_output is not None
    return ExpectedLocationOutcome.model_validate(item.expected_output).eligible_remote_from_romania


def test_counts_splits_rejection_recall_from_eligible_controls() -> None:
    results = (
        _result(False, False),
        _result(False, False),
        _result(False, True),
        _result(True, True),
        _result(True, False),
    )

    assert _counts(results) == (2, 3, 1, 2)


def test_counts_rejects_a_case_without_an_output() -> None:
    with pytest.raises(RuntimeError, match="failed without an output"):
        _ = _counts((_result(True, None),))


def test_main_passes_when_every_scored_case_is_classified_correctly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [_dataset_item(False), _dataset_item(False), _dataset_item(True), _dataset_item(None)]
    client, received = _fake_langfuse(items, _mirror_expected)
    _authorize(monkeypatch)
    monkeypatch.setattr(location_dataset, "Langfuse", client)

    exit_code = main(["--dataset", "locations", "--run-name", "release-check"])

    assert exit_code == 0
    assert [item.id for item in received[0]] == [item.id for item in items[:3]]
    output = capsys.readouterr().out
    assert "Location rejects caught: 2/2" in output
    assert "Remote-eligible controls kept: 1/1" in output


def test_main_fails_when_rejection_recall_falls_below_the_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missed: list[str] = []

    def simulate(item: DatasetItem) -> bool:
        expected = _mirror_expected(item)
        if not expected and not missed:
            missed.append(item.id)
            return True
        return expected

    items = [_dataset_item(False) for _ in range(4)] + [_dataset_item(True)]
    client, _received = _fake_langfuse(items, simulate)
    _authorize(monkeypatch)
    monkeypatch.setattr(location_dataset, "Langfuse", client)

    assert main(["--dataset", "locations", "--run-name", "release-check"]) == 1


def test_main_fails_when_an_eligible_control_is_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    lost: list[str] = []

    def simulate(item: DatasetItem) -> bool:
        expected = _mirror_expected(item)
        if expected and not lost:
            lost.append(item.id)
            return False
        return expected

    items = [_dataset_item(False), _dataset_item(False), _dataset_item(True), _dataset_item(True)]
    client, _received = _fake_langfuse(items, simulate)
    _authorize(monkeypatch)
    monkeypatch.setattr(location_dataset, "Langfuse", client)

    assert main(["--dataset", "locations", "--run-name", "release-check"]) == 1


def test_main_fails_when_the_dataset_contains_no_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [_dataset_item(True), _dataset_item(True)]
    client, _received = _fake_langfuse(items, _mirror_expected)
    _authorize(monkeypatch)
    monkeypatch.setattr(location_dataset, "Langfuse", client)

    assert main(["--dataset", "locations", "--run-name", "release-check"]) == 1


def test_main_rejects_a_dataset_without_scored_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _received = _fake_langfuse([_dataset_item(None)], _mirror_expected)
    _authorize(monkeypatch)
    monkeypatch.setattr(location_dataset, "Langfuse", client)

    with pytest.raises(ValueError, match="no scored cases"):
        _ = main(["--dataset", "locations", "--run-name", "release-check"])


@pytest.mark.parametrize("rate", ("-0.5", "1.5"))
def test_main_rejects_an_out_of_bounds_minimum_rejection_rate(rate: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _ = main(
            [
                "--dataset",
                "locations",
                "--run-name",
                "release-check",
                "--minimum-rejection-rate",
                rate,
            ]
        )

    assert excinfo.value.code == 2
