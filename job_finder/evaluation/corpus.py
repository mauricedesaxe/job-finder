from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from job_finder.ats.models import AtsAvailable
from job_finder.ats.policy import ats_structural_filter, format_ats_block
from job_finder.evaluation.models import EvaluationResult, OperationalError, Qualified
from job_finder.jobs.models import JobListing, StructuralRejection
from job_finder.jobs.scraping import detect_source, extract_company_from_url
from job_finder.jobs.structural_filter import structural_filter

CORPUS_ROOT = Path(__file__).with_name("fixtures")
MAX_FALSE_POSITIVE_RATE = Decimal("0.15")
MAX_FALSE_NEGATIVE_RATE = Decimal("0.10")
_FIXTURE_DATE = date(2026, 3, 30)
_TITLE = re.compile(r"^Title:\s*(.+)$", re.MULTILINE)
_URL = re.compile(r"^URL Source:\s*(.+)$", re.MULTILINE)
_ATS_BLOCK = re.compile(
    r"\A## ATS Structured Data \(from (?P<source>\w+) API\)\n(?P<fields>.*?)\n---[ \t]*\n*",
    re.DOTALL,
)
_PRIMARY_LOCATION = re.compile(r"^- Primary location:\s*(.+)$", re.MULTILINE)
_ALL_LOCATIONS = re.compile(r"^- All listed locations:\s*(.+)$", re.MULTILINE)
_WORKPLACE_TYPE = re.compile(r"^- Workplace type:\s*(.+)$", re.MULTILINE)
_COUNTRY = re.compile(
    r"^- (?:Country \(HQ\)|Country fallback when locations are non-geographic):\s*(.+)$",
    re.MULTILINE,
)
ExpectedOutcome = Literal["qualified", "rejected"]
CorpusSuite = Literal["direct", "ats"]
CorpusEvaluator = Callable[[JobListing], EvaluationResult]


class CorpusModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class DirectEvaluationCorpusCase(CorpusModel):
    kind: Literal["direct"] = "direct"
    name: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    expected_outcome: ExpectedOutcome
    job: JobListing


class AtsEvaluationCorpusCase(CorpusModel):
    kind: Literal["ats"] = "ats"
    name: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    expected_outcome: ExpectedOutcome
    job: JobListing
    evidence: AtsAvailable


EvaluationCorpusCase = DirectEvaluationCorpusCase | AtsEvaluationCorpusCase


class EvaluationCorpusResult(CorpusModel):
    name: str = Field(min_length=1)
    expected_outcome: ExpectedOutcome
    actual_outcome: ExpectedOutcome | None
    reason: str


class EvaluationCorpusReport(CorpusModel):
    result_count: int = Field(gt=0)
    correct_count: int = Field(ge=0)
    false_positive_count: int = Field(ge=0)
    false_negative_count: int = Field(ge=0)
    operational_failure_count: int = Field(ge=0)
    false_positive_rate: Decimal = Field(ge=0, le=1)
    false_negative_rate: Decimal = Field(ge=0, le=1)
    results: tuple[EvaluationCorpusResult, ...]

    @property
    def passed(self) -> bool:
        return (
            self.operational_failure_count == 0
            and self.false_positive_rate <= MAX_FALSE_POSITIVE_RATE
            and self.false_negative_rate <= MAX_FALSE_NEGATIVE_RATE
        )


def load_evaluation_corpus(root: Path = CORPUS_ROOT) -> tuple[DirectEvaluationCorpusCase, ...]:
    cases = tuple(
        _load_direct_case(path, root, expected)
        for expected in ("qualified", "rejected")
        for path in sorted((root / _directory(expected)).glob("*.md"))
    )
    if not cases:
        raise ValueError(f"Evaluation corpus is empty: {root}")
    _validate_unique_jobs(cases)
    return cases


def load_ats_evaluation_corpus(root: Path = CORPUS_ROOT) -> tuple[AtsEvaluationCorpusCase, ...]:
    cases = tuple(
        _load_ats_case(path, root, expected)
        for expected in ("qualified", "rejected")
        for path in sorted((root / _directory(expected) / "ats").glob("*.md"))
    )
    if not cases:
        raise ValueError(f"ATS evaluation corpus is empty: {root}")
    _validate_unique_jobs(cases)
    return cases


def _validate_unique_jobs(cases: tuple[EvaluationCorpusCase, ...]) -> None:
    paths_by_url: dict[str, str] = {}
    for case in cases:
        prior_path = paths_by_url.setdefault(case.job.url, case.relative_path)
        if prior_path != case.relative_path:
            raise ValueError(
                f"Evaluation corpus repeats {case.job.url}: {prior_path}, {case.relative_path}"
            )


def evaluate_corpus_case(
    case: EvaluationCorpusCase,
    evaluate: CorpusEvaluator,
) -> EvaluationCorpusResult:
    job = case.job
    if isinstance(case, AtsEvaluationCorpusCase):
        job = job.model_copy(
            update={
                "description": f"{format_ats_block(case.evidence)}\n\n{job.description}",
                "location": case.evidence.location,
            }
        )
        ats_decision = ats_structural_filter(case.evidence)
        if isinstance(ats_decision, StructuralRejection):
            return _case_result(case, "rejected", ats_decision.reason)
    structural_decision = structural_filter(job)
    if isinstance(structural_decision, StructuralRejection):
        return _case_result(case, "rejected", structural_decision.reason)
    evaluation = evaluate(job)
    return _case_result(
        case,
        None
        if isinstance(evaluation, OperationalError)
        else "qualified"
        if isinstance(evaluation, Qualified)
        else "rejected",
        evaluation.reason,
    )


def score_evaluation_corpus(
    results: tuple[EvaluationCorpusResult, ...],
) -> EvaluationCorpusReport:
    if not results:
        raise ValueError("Evaluation results cannot be empty")
    false_positives = sum(
        result.expected_outcome == "rejected" and result.actual_outcome == "qualified"
        for result in results
    )
    false_negatives = sum(
        result.expected_outcome == "qualified" and result.actual_outcome == "rejected"
        for result in results
    )
    operational = sum(result.actual_outcome is None for result in results)
    rejected_count = sum(result.expected_outcome == "rejected" for result in results)
    qualified_count = len(results) - rejected_count
    correct = len(results) - false_positives - false_negatives - operational
    return EvaluationCorpusReport(
        result_count=len(results),
        correct_count=correct,
        false_positive_count=false_positives,
        false_negative_count=false_negatives,
        operational_failure_count=operational,
        false_positive_rate=_rate(false_positives, rejected_count),
        false_negative_rate=_rate(false_negatives, qualified_count),
        results=results,
    )


def _load_direct_case(
    path: Path,
    root: Path,
    expected_outcome: ExpectedOutcome,
) -> DirectEvaluationCorpusCase:
    content = path.read_text()
    return DirectEvaluationCorpusCase(
        name=path.stem,
        relative_path=str(path.relative_to(root)),
        expected_outcome=expected_outcome,
        job=_load_job(path, content),
    )


def _load_ats_case(
    path: Path,
    root: Path,
    expected_outcome: ExpectedOutcome,
) -> AtsEvaluationCorpusCase:
    content = path.read_text()
    evidence, description = _parse_ats_fixture(path, content)
    return AtsEvaluationCorpusCase(
        name=path.stem,
        relative_path=str(path.relative_to(root)),
        expected_outcome=expected_outcome,
        job=_load_job(path, description),
        evidence=evidence,
    )


def _load_job(path: Path, content: str) -> JobListing:
    title_match = _TITLE.search(content)
    url_match = _URL.search(content)
    title = title_match.group(1).strip() if title_match is not None else path.stem
    url = (
        url_match.group(1).strip() if url_match is not None else f"https://example.com/{path.stem}"
    )
    return JobListing(
        title=title,
        company=extract_company_from_url(url),
        url=url,
        source=detect_source(url),
        keywords_matched=("test",),
        date_posted=None,
        date_scraped=_FIXTURE_DATE,
        description=content,
    )


def _parse_ats_fixture(path: Path, content: str) -> tuple[AtsAvailable, str]:
    block = _ATS_BLOCK.match(content)
    if block is None:
        raise ValueError(f"ATS fixture has no structured data block: {path}")
    fields = block.group("fields")
    workplace_type = _field(_WORKPLACE_TYPE, fields)
    evidence = AtsAvailable.model_validate(
        {
            "source": block.group("source"),
            "location": _field(_PRIMARY_LOCATION, fields) or "",
            "locations": tuple(
                location.strip()
                for location in (_field(_ALL_LOCATIONS, fields) or "").split(",")
                if location.strip()
            ),
            "workplace_type": None if workplace_type in (None, "unspecified") else workplace_type,
            "country": _field(_COUNTRY, fields),
        }
    )
    return evidence, content[block.end() :]


def _field(pattern: re.Pattern[str], content: str) -> str | None:
    match = pattern.search(content)
    return match.group(1).strip() if match is not None else None


def _case_result(
    case: EvaluationCorpusCase,
    actual_outcome: ExpectedOutcome | None,
    reason: str,
) -> EvaluationCorpusResult:
    return EvaluationCorpusResult(
        name=case.name,
        expected_outcome=case.expected_outcome,
        actual_outcome=actual_outcome,
        reason=reason,
    )


def _directory(expected_outcome: ExpectedOutcome) -> str:
    return "pass" if expected_outcome == "qualified" else "reject"


def _rate(count: int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal(0)
    return (Decimal(count) / Decimal(denominator)).quantize(Decimal("0.0000001"))
