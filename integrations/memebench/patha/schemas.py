"""Strict S1 data-transfer contracts.

The three top-level models are intentionally not subclasses of one another.
Only :class:`MaintenanceInput` may cross the maintenance-runtime boundary.
Questions are released through :class:`AnswerBundle` after a state snapshot is
sealed, while :class:`ScoringSidecar` is for offline scoring only.
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "patha-s1-v1"


def canonical_json_bytes(value: Any) -> bytes:
    """Return the single canonical JSON representation used by S1 hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


_OPAQUE_PATTERNS = {
    "episode": re.compile(r"^ep_[0-9a-f]{24}$"),
    "source": re.compile(r"^src_[0-9a-f]{24}$"),
    "question": re.compile(r"^q_[0-9a-f]{24}$"),
    "bundle": re.compile(r"^ab_[0-9a-f]{24}$"),
    "record": re.compile(r"^rec_[0-9a-f]{24}$"),
}
_OPAQUE_PREFIXES = {
    "episode": "ep",
    "source": "src",
    "question": "q",
    "bundle": "ab",
    "record": "rec",
}


def opaque_id(kind: str, dataset_sha256: str, *parts: object) -> str:
    """Derive a non-semantic, non-reversible identifier from source identity."""

    if kind not in _OPAQUE_PATTERNS:
        raise ValueError(f"unknown opaque ID kind: {kind}")
    material = "\0".join([SCHEMA_VERSION, dataset_sha256, kind, *(str(p) for p in parts)])
    return f"{_OPAQUE_PREFIXES[kind]}_" + sha256(
        material.encode("utf-8")
    ).hexdigest()[:24]


def validate_opaque_id(value: str, kind: str) -> str:
    pattern = _OPAQUE_PATTERNS[kind]
    if not pattern.fullmatch(value):
        raise ValueError(
            f"{kind} ID must be opaque and match {pattern.pattern}; "
            "raw domain/session/task identifiers are forbidden"
        )
    return value


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Message(FrozenModel):
    role: Literal["user", "assistant", "system", "tool"]
    content: str


class MaintenanceSource(FrozenModel):
    source_id: str
    ordinal: int = Field(ge=0)
    source_version: int = Field(default=1, ge=1)
    timestamp: str | None = None
    messages: tuple[Message, ...]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("source_id")
    @classmethod
    def _opaque_source_id(cls, value: str) -> str:
        return validate_opaque_id(value, "source")

    @model_validator(mode="after")
    def _content_hash_matches(self) -> "MaintenanceSource":
        payload = [message.model_dump(mode="json") for message in self.messages]
        if canonical_sha256(payload) != self.content_sha256:
            raise ValueError("source content_sha256 does not match messages")
        return self


class MaintenanceInput(FrozenModel):
    """The only object accepted by P1/P2 maintenance code."""

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    dataset_id: str = Field(pattern=r"^meme_filler32k@[0-9a-f]{12}$")
    episode_id: str
    sources: tuple[MaintenanceSource, ...]
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("episode_id")
    @classmethod
    def _opaque_episode_id(cls, value: str) -> str:
        return validate_opaque_id(value, "episode")

    @model_validator(mode="after")
    def _ordered_and_hashed(self) -> "MaintenanceInput":
        ordinals = [source.ordinal for source in self.sources]
        if ordinals != list(range(len(self.sources))):
            raise ValueError("maintenance sources must be complete and ordered from ordinal 0")
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source IDs must be unique within an episode")
        payload = {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "episode_id": self.episode_id,
            "sources": [source.model_dump(mode="json") for source in self.sources],
        }
        if canonical_sha256(payload) != self.input_sha256:
            raise ValueError("input_sha256 does not match maintenance payload")
        return self


class AnswerQuestion(FrozenModel):
    question_id: str
    text: str

    @field_validator("question_id")
    @classmethod
    def _opaque_question_id(cls, value: str) -> str:
        return validate_opaque_id(value, "question")


class AnswerBundle(FrozenModel):
    """Questions released only after the named source-prefix state is sealed."""

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    episode_id: str
    bundle_id: str
    phase: Literal["before", "after"]
    release_after_ordinal: int = Field(ge=0)
    release_after_source_id: str
    source_prefix_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_state_required: Literal[True] = True
    questions: tuple[AnswerQuestion, ...]
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("episode_id")
    @classmethod
    def _opaque_episode_id(cls, value: str) -> str:
        return validate_opaque_id(value, "episode")

    @field_validator("bundle_id")
    @classmethod
    def _opaque_bundle_id(cls, value: str) -> str:
        return validate_opaque_id(value, "bundle")

    @field_validator("release_after_source_id")
    @classmethod
    def _opaque_source_id(cls, value: str) -> str:
        return validate_opaque_id(value, "source")

    @model_validator(mode="after")
    def _hash_matches(self) -> "AnswerBundle":
        payload = self.model_dump(mode="json", exclude={"bundle_sha256"})
        if canonical_sha256(payload) != self.bundle_sha256:
            raise ValueError("bundle_sha256 does not match answer bundle")
        if not self.questions:
            raise ValueError("answer bundle cannot be empty")
        return self


class ScoringSidecar(FrozenModel):
    """Privileged, offline-only record for one Cas/Abs event-target."""

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    record_id: str
    episode_id: str
    raw_episode_id: str
    domain: Literal["personal_life", "software_project"]
    task_type: Literal["Cas", "Abs"]
    hop: Literal[1, 2]
    target_entities: tuple[str, ...]
    event_root: str
    event_before: Any
    event_after: Any
    before_question_id: str
    after_question_id: str
    before_entity_values: dict[str, Any]
    after_entity_values: dict[str, Any]
    before_expected_answer: str
    after_gold_answer: str
    answer_pair_status: Literal["scoreable", "needs_adjudication"]
    path_status: Literal["scoreable", "needs_adjudication"]
    issues: tuple[str, ...] = ()
    adjudication_status: Literal["resolved_included"] | None = None
    mechanism_scoring_included: bool | None = None
    official_answer_scoring_included: bool | None = None
    data_quality_notes: tuple[str, ...] | None = None
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("record_id")
    @classmethod
    def _opaque_record_id(cls, value: str) -> str:
        return validate_opaque_id(value, "record")

    @field_validator("episode_id")
    @classmethod
    def _opaque_episode_id(cls, value: str) -> str:
        return validate_opaque_id(value, "episode")

    @field_validator("before_question_id", "after_question_id")
    @classmethod
    def _opaque_question_id(cls, value: str) -> str:
        return validate_opaque_id(value, "question")

    @model_validator(mode="after")
    def _hash_matches(self) -> "ScoringSidecar":
        # The optional adjudication fields were added for the S1-R3 v2
        # materials.  Excluding absent values keeps the archived v1 records
        # byte-for-byte verifiable without rewriting them.
        payload = self.model_dump(
            mode="json", exclude={"record_sha256"}, exclude_none=True
        )
        if canonical_sha256(payload) != self.record_sha256:
            raise ValueError("record_sha256 does not match scoring sidecar")
        if self.answer_pair_status == "scoreable" and self.issues and any(
            issue.startswith("question_") for issue in self.issues
        ):
            raise ValueError("question issues require answer_pair_status=needs_adjudication")
        adjudication_values = (
            self.adjudication_status,
            self.mechanism_scoring_included,
            self.official_answer_scoring_included,
            self.data_quality_notes,
        )
        if any(value is not None for value in adjudication_values) and any(
            value is None for value in adjudication_values
        ):
            raise ValueError("S1-R3 adjudication fields must be present together")
        if self.adjudication_status == "resolved_included" and not (
            self.mechanism_scoring_included
            and self.official_answer_scoring_included
            and self.path_status == "scoreable"
        ):
            raise ValueError("resolved-included records must remain in both score sets")
        return self
