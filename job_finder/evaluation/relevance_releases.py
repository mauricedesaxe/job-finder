from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Protocol, Self, assert_never, cast

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from job_finder.evaluation.models import ReleaseTarget, RelevanceReleaseId
from job_finder.evaluation.prompt_releases import PromptRelease, PromptVersion

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_STRING = TypeAdapter(str)
_FLOAT = TypeAdapter(float)
_ATOMIC_QUESTION_REGISTRY = TypeAdapter(dict[str, dict[str, object]])


class _JevQuestionCriteria(Protocol):
    true: str
    false: str


class _JevQuestionTemplate(Protocol):
    instructions: str
    criteria: _JevQuestionCriteria


class RelevanceReleaseError(RuntimeError):
    pass


class RelevanceModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class RelevanceQuestion(RelevanceModel):
    instructions: str = Field(min_length=1)
    true: str = Field(min_length=1)
    false: str = Field(min_length=1)


class CodeArtifactIdentity(RelevanceModel):
    entrypoint: str = Field(pattern=r"^[a-zA-Z_][\w.]*:[a-zA-Z_]\w*$")
    content_digest: str = Field(pattern=_DIGEST_PATTERN)


class ThresholdComposition(RelevanceModel):
    kind: Literal["threshold"] = "threshold"
    pass_threshold: float = Field(ge=0, le=1)


class NoActiveSignals(RelevanceModel):
    kind: Literal["no_active_signals"] = "no_active_signals"


class FewerThanActiveSignals(RelevanceModel):
    kind: Literal["fewer_than_active_signals"] = "fewer_than_active_signals"
    count: int = Field(gt=0)


class PositiveWithoutExclusion(RelevanceModel):
    kind: Literal["positive_without_exclusion"] = "positive_without_exclusion"
    positive_question: str = Field(min_length=1)
    exclusion_question: str = Field(min_length=1)


AtomicCriterionComposition = Annotated[
    NoActiveSignals | FewerThanActiveSignals | PositiveWithoutExclusion,
    Field(discriminator="kind"),
]


class AtomicComposition(RelevanceModel):
    kind: Literal["atomic"] = "atomic"
    pass_threshold: float = Field(ge=0, le=1)
    criteria: dict[str, AtomicCriterionComposition] = Field(min_length=1)


class GeminiExecutionPolicy(RelevanceModel):
    kind: Literal["gemini"] = "gemini"
    provider: Literal["openrouter"] = "openrouter"
    model: str = Field(min_length=1)
    protocol: Literal["openrouter-chat-completions-tools-v1"] = (
        "openrouter-chat-completions-tools-v1"
    )
    questions: Literal["prompt-release-versions"] = "prompt-release-versions"
    composition: Literal["filters-all-profiles-any-v1"] = "filters-all-profiles-any-v1"
    input_serialization: CodeArtifactIdentity
    provider_adapter: CodeArtifactIdentity
    decision_composition: tuple[CodeArtifactIdentity, ...] = Field(min_length=1)


class JevFaithfulExecutionPolicy(RelevanceModel):
    kind: Literal["jev_faithful"] = "jev_faithful"
    provider: Literal["typesafe"] = "typesafe"
    model: str = Field(min_length=1)
    protocol: Literal["typesafe-system-one-v1"] = "typesafe-system-one-v1"
    questions: dict[str, RelevanceQuestion] = Field(min_length=1)
    composition: ThresholdComposition
    input_serialization: CodeArtifactIdentity
    provider_adapter: CodeArtifactIdentity
    decision_composition: tuple[CodeArtifactIdentity, ...] = Field(min_length=1)


class JevAtomicExecutionPolicy(RelevanceModel):
    kind: Literal["jev_atomic"] = "jev_atomic"
    provider: Literal["typesafe"] = "typesafe"
    model: str = Field(min_length=1)
    protocol: Literal["typesafe-system-one-v1"] = "typesafe-system-one-v1"
    questions: dict[str, dict[str, RelevanceQuestion]] = Field(min_length=1)
    composition: AtomicComposition
    input_serialization: CodeArtifactIdentity
    provider_adapter: CodeArtifactIdentity
    decision_composition: tuple[CodeArtifactIdentity, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def composition_covers_questions(self) -> Self:
        if set(self.composition.criteria) != set(self.questions):
            raise ValueError("Atomic composition must cover every criterion exactly")
        for criterion, rule in self.composition.criteria.items():
            question_names = set(self.questions[criterion])
            if isinstance(rule, FewerThanActiveSignals) and rule.count > len(question_names):
                raise ValueError("Atomic signal count exceeds available questions")
            if (
                isinstance(rule, PositiveWithoutExclusion)
                and {
                    rule.positive_question,
                    rule.exclusion_question,
                }
                - question_names
            ):
                raise ValueError("Atomic composition references an unknown question")
            if (
                isinstance(rule, PositiveWithoutExclusion)
                and rule.positive_question == rule.exclusion_question
            ):
                raise ValueError("Atomic positive and exclusion questions must differ")
        return self


RelevanceExecutionPolicy = Annotated[
    GeminiExecutionPolicy | JevFaithfulExecutionPolicy | JevAtomicExecutionPolicy,
    Field(discriminator="kind"),
]
_POLICY_ADAPTER: TypeAdapter[RelevanceExecutionPolicy] = TypeAdapter(RelevanceExecutionPolicy)


class RelevanceRelease(RelevanceModel):
    id: Annotated[RelevanceReleaseId, Field(pattern=_DIGEST_PATTERN)]
    content_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy: RelevanceExecutionPolicy


def build_gemini_policy(prompt_release: PromptRelease) -> GeminiExecutionPolicy:
    evaluation_versions = _evaluation_versions(prompt_release)
    models = {version.model for version in evaluation_versions}
    if len(models) != 1:
        raise RelevanceReleaseError("Gemini evaluation prompts must use one model")
    return GeminiExecutionPolicy(
        model=models.pop(),
        input_serialization=_evaluation_artifact("job_message"),
        provider_adapter=_module_artifact("openrouter.py", "evaluate_prompt"),
        decision_composition=(_evaluation_artifact("evaluate_job"),),
    )


def build_jev_faithful_policy(prompt_release: PromptRelease) -> JevFaithfulExecutionPolicy:
    questions = {
        version.definition.criterion: _faithful_question(version)
        for version in _evaluation_versions(prompt_release)
    }
    return JevFaithfulExecutionPolicy(
        model=_jev_model(),
        questions=questions,
        composition=ThresholdComposition(pass_threshold=_jev_pass_threshold()),
        input_serialization=_evaluation_artifact("job_message"),
        provider_adapter=_module_artifact("jev.py", "evaluate_prompt"),
        decision_composition=(
            _evaluation_artifact("evaluate_job"),
            _module_artifact("jev.py", "evaluate_prompt"),
        ),
    )


def build_jev_atomic_policy() -> JevAtomicExecutionPolicy:
    registry = _ATOMIC_QUESTION_REGISTRY.validate_python(
        getattr(importlib.import_module("job_finder.evaluation.jev"), "ATOMIC_QUESTIONS")
    )
    questions = {
        criterion: {name: _relevance_question(template) for name, template in templates.items()}
        for criterion, templates in registry.items()
    }
    compositions: dict[str, AtomicCriterionComposition] = {
        "remote-europe-eligible": NoActiveSignals(),
        "compensation-minimum": NoActiveSignals(),
        "role-quality": NoActiveSignals(),
        "cheap-shop-placement": FewerThanActiveSignals(count=2),
        "early-stage-product-engineer": PositiveWithoutExclusion(
            positive_question="owns_product_delivery",
            exclusion_question="excluded_primary_shape",
        ),
        "applied-ai-product-engineer": PositiveWithoutExclusion(
            positive_question="ships_ai_product",
            exclusion_question="excluded_ai_shape",
        ),
    }
    return JevAtomicExecutionPolicy(
        model=_jev_model(),
        questions=questions,
        composition=AtomicComposition(
            pass_threshold=_jev_pass_threshold(),
            criteria=compositions,
        ),
        input_serialization=_evaluation_artifact("job_message"),
        provider_adapter=_module_artifact("jev.py", "evaluate_prompt"),
        decision_composition=(
            _evaluation_artifact("evaluate_job"),
            _module_artifact("jev.py", "_compose_release_atomic"),
        ),
    )


def build_relevance_release(policy: RelevanceExecutionPolicy) -> RelevanceRelease:
    validated = _POLICY_ADAPTER.validate_python(policy.model_dump(mode="json"))
    digest = _digest(validated.model_dump(mode="json"))
    return RelevanceRelease(
        id=RelevanceReleaseId(digest),
        content_digest=digest,
        policy=validated,
    )


def store_relevance_release(
    connection: psycopg.Connection[tuple[object, ...]],
    release: RelevanceRelease,
    *,
    created_at: datetime,
    created_by: str,
) -> RelevanceRelease:
    if not connection.autocommit:
        raise ValueError("Relevance release storage requires an autocommit connection")
    _validate_release_identity(release)
    content = release.policy.model_dump(mode="json")
    with connection.transaction():
        _ = connection.execute(
            """
            INSERT INTO relevance_releases (
              id, content_digest, content, created_at, created_by
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                release.id,
                release.content_digest,
                Jsonb(content),
                created_at,
                created_by,
            ),
        )
        loaded = load_relevance_release(connection, release.id)
        if loaded != release:
            raise RelevanceReleaseError("Stored relevance release differs from supplied release")
        return loaded


def load_relevance_release(
    connection: psycopg.Connection[tuple[object, ...]], release_id: RelevanceReleaseId
) -> RelevanceRelease:
    row = connection.execute(
        "SELECT content_digest, content FROM relevance_releases WHERE id = %s",
        (release_id,),
    ).fetchone()
    if row is None:
        raise RelevanceReleaseError(f"Relevance release not found: {release_id}")
    try:
        policy = _POLICY_ADAPTER.validate_python(row[1])
    except ValidationError as error:
        raise RelevanceReleaseError(
            f"Relevance release {release_id} contains invalid content"
        ) from error
    digest = _digest(policy.model_dump(mode="json"))
    if str(row[0]) != digest or str(release_id) != digest:
        raise RelevanceReleaseError(f"Stored relevance release identity is corrupt: {release_id}")
    return RelevanceRelease(id=release_id, content_digest=digest, policy=policy)


def validate_release_target(
    target: ReleaseTarget,
    prompt_release: PromptRelease,
    relevance_release: RelevanceRelease,
) -> None:
    _validate_release_identity(relevance_release)
    if target.prompt_release_id != prompt_release.id:
        raise RelevanceReleaseError("Release target does not identify the prompt release")
    if target.relevance_release_id != relevance_release.id:
        raise RelevanceReleaseError("Release target does not identify the relevance release")
    policy = relevance_release.policy
    _validate_execution_artifacts(policy)
    criteria = {version.definition.criterion for version in _evaluation_versions(prompt_release)}
    if isinstance(policy, GeminiExecutionPolicy):
        models = {version.model for version in _evaluation_versions(prompt_release)}
        if models != {policy.model}:
            raise RelevanceReleaseError("Gemini policy cannot execute the stored prompt versions")
    elif isinstance(policy, JevFaithfulExecutionPolicy):
        if policy.model != _jev_model():
            raise RelevanceReleaseError("Faithful Jev model is not supported by the adapter")
        if set(policy.questions) != criteria:
            raise RelevanceReleaseError(
                "Faithful Jev questions must cover evaluation criteria exactly"
            )
    else:
        if policy.model != _jev_model():
            raise RelevanceReleaseError("Atomic Jev model is not supported by the adapter")
        if set(policy.questions) != criteria:
            raise RelevanceReleaseError(
                "Atomic Jev questions must cover evaluation criteria exactly"
            )


def _validate_execution_artifacts(policy: RelevanceExecutionPolicy) -> None:
    match policy:
        case GeminiExecutionPolicy():
            expected = (
                _evaluation_artifact("job_message"),
                _module_artifact("openrouter.py", "evaluate_prompt"),
                _evaluation_artifact("evaluate_job"),
            )
        case JevFaithfulExecutionPolicy():
            expected = (
                _evaluation_artifact("job_message"),
                _module_artifact("jev.py", "evaluate_prompt"),
                _evaluation_artifact("evaluate_job"),
                _module_artifact("jev.py", "evaluate_prompt"),
            )
        case JevAtomicExecutionPolicy():
            expected = (
                _evaluation_artifact("job_message"),
                _module_artifact("jev.py", "evaluate_prompt"),
                _evaluation_artifact("evaluate_job"),
                _module_artifact("jev.py", "_compose_release_atomic"),
            )
        case _:
            assert_never(policy)
    stored = (
        policy.input_serialization,
        policy.provider_adapter,
        *policy.decision_composition,
    )
    if stored != expected:
        raise RelevanceReleaseError(
            "Relevance policy implementation artifacts do not match current source artifacts"
        )


def _evaluation_versions(prompt_release: PromptRelease) -> tuple[PromptVersion, ...]:
    versions = tuple(
        version
        for version in prompt_release.versions
        if version.definition.phase in ("filter", "profile")
    )
    phases = {version.definition.phase for version in versions}
    if phases != {"filter", "profile"}:
        raise RelevanceReleaseError("Prompt release requires filter and profile evaluation prompts")
    criteria = [version.definition.criterion for version in versions]
    if len(criteria) != len(set(criteria)):
        raise RelevanceReleaseError("Prompt release evaluation criteria must be unique")
    return versions


def _faithful_question(version: PromptVersion) -> RelevanceQuestion:
    rubric = version.messages[0]["content"]
    return RelevanceQuestion(
        instructions=(
            "Does this job listing pass the following evaluation criterion? "
            "Apply its PASS and FAIL rules exactly.\n\n"
            f"{rubric}"
        ),
        true="The listing passes the criterion according to its PASS and FAIL rules.",
        false="The listing fails the criterion according to its PASS and FAIL rules.",
    )


def _validate_release_identity(release: RelevanceRelease) -> None:
    try:
        policy = _POLICY_ADAPTER.validate_python(release.policy.model_dump(mode="json"))
    except ValidationError as error:
        raise RelevanceReleaseError("Relevance execution policy is invalid") from error
    digest = _digest(policy.model_dump(mode="json"))
    if release.content_digest != digest or release.id != digest:
        raise RelevanceReleaseError("Relevance release identity is invalid")


def source_artifact_identity(entrypoint: str, source_path: Path) -> CodeArtifactIdentity:
    return CodeArtifactIdentity(
        entrypoint=entrypoint,
        content_digest=hashlib.sha256(source_path.read_bytes()).hexdigest(),
    )


def _evaluation_artifact(function_name: str) -> CodeArtifactIdentity:
    return _module_artifact("evaluate.py", function_name)


def _module_artifact(file_name: str, function_name: str) -> CodeArtifactIdentity:
    module_name = Path(file_name).stem
    return source_artifact_identity(
        f"job_finder.evaluation.{module_name}:{function_name}",
        Path(__file__).with_name(file_name),
    )


def _jev_model() -> str:
    return _STRING.validate_python(
        getattr(importlib.import_module("job_finder.evaluation.jev"), "JEV_MODEL")
    )


def _jev_pass_threshold() -> float:
    return _FLOAT.validate_python(
        getattr(importlib.import_module("job_finder.evaluation.jev"), "JEV_PASS_THRESHOLD")
    )


def _relevance_question(template: object) -> RelevanceQuestion:
    typed = cast(_JevQuestionTemplate, template)
    return RelevanceQuestion(
        instructions=typed.instructions,
        true=typed.criteria.true,
        false=typed.criteria.false,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()
