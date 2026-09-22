from decimal import Decimal
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from job_finder.ats.models import AtsAvailable
from job_finder.ats.policy import format_ats_block
from job_finder.evaluation.corpus import (
    CORPUS_ROOT,
    MAX_FALSE_NEGATIVE_RATE,
    MAX_FALSE_POSITIVE_RATE,
    EvaluationCorpusResult,
    evaluate_corpus_case,
    load_ats_evaluation_corpus,
    load_evaluation_corpus,
    score_evaluation_corpus,
)
from scripts.evaluate_corpus import parse_arguments
from job_finder.evaluation.models import Qualified, Rejected


def test_loads_only_direct_evaluation_fixtures_by_default() -> None:
    cases = load_evaluation_corpus()

    assert len(cases) == 122
    assert sum(case.expected_outcome == "qualified" for case in cases) == 57
    assert sum(case.expected_outcome == "rejected" for case in cases) == 65
    assert len({case.relative_path for case in cases}) == 122
    assert all("/ats/" not in case.relative_path for case in cases)
    assert all(case.job.description for case in cases)


def test_loads_ats_fixtures_only_through_the_explicit_suite() -> None:
    cases = load_ats_evaluation_corpus()

    assert len(cases) == 25
    assert sum(case.expected_outcome == "qualified" for case in cases) == 9
    assert sum(case.expected_outcome == "rejected" for case in cases) == 16
    assert len({case.relative_path for case in cases}) == 25
    assert all("/ats/" in case.relative_path for case in cases)
    assert (
        next(
            case for case in cases if case.name == "raya-remote-us-only-silent-body"
        ).evidence.country
        == "US"
    )


def test_rejects_duplicate_jobs_in_the_same_corpus(tmp_path: Path) -> None:
    fixture = """Title: Senior Engineer

URL Source: https://example.com/job

Markdown Content:
Build the product.
"""
    for outcome in ("pass", "reject"):
        directory = tmp_path / outcome
        directory.mkdir()
        _ = (directory / f"{outcome}.md").write_text(fixture)

    duplicate_message = "Evaluation corpus repeats https://example.com/job"
    with pytest.raises(ValueError, match=duplicate_message):
        _ = load_evaluation_corpus(tmp_path)


def test_loads_country_from_the_current_ats_format(tmp_path: Path) -> None:
    directory = tmp_path / "pass" / "ats"
    directory.mkdir(parents=True)
    evidence = AtsAvailable(
        source="lever",
        location="Remote",
        locations=("Remote",),
        workplace_type="Remote",
        country="US",
    )
    fixture = f"""{format_ats_block(evidence)}

Title: Senior Engineer

URL Source: https://example.com/job

Markdown Content:
Remote role.
"""
    _ = (directory / "remote.md").write_text(fixture)

    cases = load_ats_evaluation_corpus(tmp_path)

    assert cases[0].evidence.country == "US"


def test_rejects_direct_structural_failures_before_evaluation() -> None:
    case = next(
        case for case in load_evaluation_corpus() if case.name == "connecthum-careers-index"
    )

    result = evaluate_corpus_case(case, lambda _job: _unexpected_evaluation())

    assert result.actual_outcome == "rejected"
    assert result.reason.startswith("Careers-index page")


def test_rejects_ats_structural_failures_before_other_stages() -> None:
    case = next(
        case for case in load_ats_evaluation_corpus() if case.name == "polymarket-onsite-ny"
    )
    case = case.model_copy(
        update={"job": case.job.model_copy(update={"url": "https://jobs.lever.co/polymarket"})}
    )

    result = evaluate_corpus_case(case, lambda _job: _unexpected_evaluation())

    assert result.actual_outcome == "rejected"
    assert result.reason.startswith("ATS workplaceType=OnSite")


def test_formats_ats_evidence_before_evaluation() -> None:
    case = next(
        case
        for case in load_ats_evaluation_corpus()
        if case.name == "aifi-senior-backend-europe-remote"
    )
    evaluated_descriptions: list[str] = []

    result = evaluate_corpus_case(
        case,
        lambda job: _record_qualified(evaluated_descriptions, job.description),
    )

    assert result.actual_outcome == "qualified"
    assert len(evaluated_descriptions) == 1
    expected_prefix = """## ATS Structured Data (from lever API)
- Primary location: Europe
- All listed locations: Europe, Lisbon, Madrid
- Workplace type: Remote
- Country fallback when locations are non-geographic: United States
---

"""
    assert evaluated_descriptions[0].startswith(expected_prefix)
    assert evaluated_descriptions[0].count("## ATS Structured Data") == 1


def test_recent_policy_fixtures_reach_model_evaluation_with_their_intended_outcomes(
    tmp_path: Path,
) -> None:
    corpus = {case.name: case for case in load_evaluation_corpus()}
    staged_directory = tmp_path / "reject"
    staged_directory.mkdir()
    shutil.copy(
        CORPUS_ROOT / "reject" / "candidates" / "hype-copy-permanent-availability-product-role.md",
        staged_directory / "hype-copy-permanent-availability-product-role.md",
    )
    staged = {case.name: case for case in load_evaluation_corpus(root=tmp_path)}
    evaluated: list[str] = []

    fine_tuning = evaluate_corpus_case(
        corpus["fine-tuning-existing-model-product-engineer"],
        lambda _job: _record_qualified(evaluated, "fine-tuning"),
    )
    always_on = evaluate_corpus_case(
        staged["hype-copy-permanent-availability-product-role"],
        lambda _job: _record_rejected(evaluated, "always-on"),
    )

    assert fine_tuning.actual_outcome == "qualified"
    assert always_on.actual_outcome == "rejected"
    assert evaluated == ["fine-tuning", "always-on"]


def test_exposes_the_corpus_gate_as_a_module_command() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.evaluate_corpus", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--suite {direct,ats}" in result.stdout
    assert "--provider {openrouter,jev}" in result.stdout


def test_parses_the_provider_with_openrouter_as_the_default() -> None:
    assert parse_arguments([]).provider == "openrouter"
    assert parse_arguments(["--provider", "jev"]).provider == "jev"
    assert parse_arguments([]).trials is None
    assert parse_arguments(["--provider", "jev", "--trials", "5"]).trials == 5


def test_scores_false_positives_and_false_negatives_against_separate_populations() -> None:
    results = (
        _result("negative-wrong", "rejected", "qualified"),
        _result("negative-right", "rejected", "rejected"),
        _result("positive-wrong", "qualified", "rejected"),
        _result("positive-error", "qualified", None),
    )

    report = score_evaluation_corpus(results)

    assert MAX_FALSE_POSITIVE_RATE == Decimal("0.15")
    assert MAX_FALSE_NEGATIVE_RATE == Decimal("0.10")
    assert report.false_positive_rate == Decimal("0.5000000")
    assert report.false_negative_rate == Decimal("0.5000000")
    assert report.operational_failure_count == 1
    assert not report.passed


def _unexpected_evaluation() -> Qualified:
    raise AssertionError("LLM evaluation ran before structural filtering")


def _record_qualified(descriptions: list[str], description: str) -> Qualified:
    descriptions.append(description)
    return Qualified(reason="Matched.", profile_name="test-profile")


def _record_rejected(names: list[str], name: str) -> Rejected:
    names.append(name)
    return Rejected(reason="Policy rejection.")


def _result(name: str, expected: str, actual: str | None) -> EvaluationCorpusResult:
    return EvaluationCorpusResult.model_validate(
        {
            "name": name,
            "expected_outcome": expected,
            "actual_outcome": actual,
            "reason": "fixture result",
        }
    )
