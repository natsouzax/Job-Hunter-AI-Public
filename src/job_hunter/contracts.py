"""Strict, dependency-free data contracts for the Job Hunter MVP."""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import ClassVar


class ValidationError(ValueError):
    """A record or its JSON representation violates the shared interface."""


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fail(path: str, reason: str) -> None:
    raise ValidationError(f"{path}: {reason}")


def _string(value: object, path: str, *, nonempty: bool = True) -> None:
    if type(value) is not str or (nonempty and not value.strip()):
        _fail(path, "expected nonempty string" if nonempty else "expected string")


def _uuid(value: object, path: str) -> None:
    _string(value, path)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValidationError(f"{path}: invalid UUID") from exc
    if str(parsed) != value:
        _fail(path, "expected canonical lowercase UUID")


def _timestamp(value: object, path: str) -> None:
    _string(value, path)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{path}: invalid ISO8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(path, "timestamp must have a timezone")


def _number(value: object, path: str, lo: float = 0, hi: float = 100) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or not lo <= value <= hi:
        _fail(path, f"expected finite number from {lo} to {hi}")


def _integer(value: object, path: str, lo: int, hi: int | None = None) -> None:
    if type(value) is not int or value < lo or (hi is not None and value > hi):
        _fail(path, f"expected integer from {lo}" + (f" to {hi}" if hi is not None else ""))


def _list(value: object, path: str, item_check) -> None:
    if type(value) is not list:
        _fail(path, "expected list")
    for i, item in enumerate(value):
        item_check(item, f"{path}[{i}]")


def _mapping(value: object, path: str, required: set[str], optional: set[str] = frozenset()) -> None:
    if type(value) is not dict:
        _fail(path, "expected object")
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        _fail(path, f"missing fields: {', '.join(sorted(missing))}")
    if unknown:
        _fail(path, f"unknown fields: {', '.join(sorted(map(str, unknown)))}")


def _json_value(value: object, path: str) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail(path, "nonfinite JSON number")
        return
    if type(value) is list:
        _list(value, path, _json_value)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _fail(path, "object keys must be strings")
            _json_value(item, f"{path}.{key}")
        return
    _fail(path, "expected JSON value")


def _string_list(value: object, path: str) -> None:
    _list(value, path, _string)


def _uuid_list(value: object, path: str) -> None:
    _list(value, path, _uuid)


def _criteria(value: object, path: str) -> None:
    if type(value) is not dict:
        _fail(path, "expected object")
    for key, score in value.items():
        _string(key, f"{path} key")
        _number(score, f"{path}.{key}")


_HHMM = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")


def _window(value: object, path: str) -> None:
    _mapping(value, path, {"day", "start", "end"})
    _integer(value["day"], f"{path}.day", 0, 6)
    for key in ("start", "end"):
        if type(value[key]) is not str or not _HHMM.fullmatch(value[key]):
            _fail(f"{path}.{key}", "expected HH:MM")
    if value["start"] >= value["end"]:
        _fail(path, "start must precede end")


def _windows(value: object, path: str) -> None:
    _list(value, path, _window)


def _availability(value: object, path: str) -> None:
    _mapping(value, path, {"confirmed_at", "status", "windows"})
    if value["status"] not in ("unconfirmed", "confirmed") or type(value["status"]) is not str:
        _fail(f"{path}.status", "expected unconfirmed or confirmed")
    if value["confirmed_at"] is not None:
        _timestamp(value["confirmed_at"], f"{path}.confirmed_at")
    if value["status"] == "confirmed" and value["confirmed_at"] is None:
        _fail(path, "confirmed status requires confirmed_at")
    if value["status"] == "unconfirmed" and value["confirmed_at"] is not None:
        _fail(path, "unconfirmed status cannot have confirmed_at")
    _windows(value["windows"], f"{path}.windows")


def _schedule(value: object, path: str) -> None:
    if value is None:
        return
    _mapping(value, path, {"windows"})
    _windows(value["windows"], f"{path}.windows")


def _requirement(value: object, path: str) -> None:
    _mapping(value, path, {"key", "description", "mandatory"})
    _string(value["key"], f"{path}.key")
    _string(value["description"], f"{path}.description")
    if type(value["mandatory"]) is not bool:
        _fail(f"{path}.mandatory", "expected boolean")


@dataclass(frozen=True, kw_only=True)
class _Record:
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=utc_now)
    _registry: ClassVar[dict[str, type[_Record]]] = {}

    def __post_init__(self) -> None:
        _uuid(self.id, "id")
        _timestamp(self.created_at, "created_at")

    def to_dict(self) -> dict:
        # Validation also catches mutation through the lists/dicts in frozen records.
        self.__post_init__()
        return json.loads(json.dumps({f.name: getattr(self, f.name) for f in fields(self)}, allow_nan=False))

    @classmethod
    def from_dict(cls, payload: dict):
        if type(payload) is not dict:
            _fail(cls.__name__, "expected object")
        names = {f.name for f in fields(cls)}
        required = names - {"id", "created_at"}
        _mapping(payload, cls.__name__, required, {"id", "created_at"})
        try:
            return cls(**payload)
        except TypeError as exc:
            raise ValidationError(f"{cls.__name__}: {exc}") from exc

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _Record._registry[cls.__name__] = cls


@dataclass(frozen=True, kw_only=True)
class CandidateProfile(_Record):
    name: str
    location: str
    fact_ids: list[str]
    preferences: dict
    availability: dict
    unconfirmed: list[str]

    def __post_init__(self):
        super().__post_init__()
        _string(self.name, "name")
        _string(self.location, "location")
        _uuid_list(self.fact_ids, "fact_ids")
        if type(self.preferences) is not dict:
            _fail("preferences", "expected object")
        _json_value(self.preferences, "preferences")
        _availability(self.availability, "availability")
        _string_list(self.unconfirmed, "unconfirmed")


@dataclass(frozen=True, kw_only=True)
class VerifiedFact(_Record):
    profile_id: str
    category: str
    statement: str
    source: str
    verified_at: str
    tags: list[str]

    def __post_init__(self):
        super().__post_init__()
        _uuid(self.profile_id, "profile_id")
        for name in ("category", "statement", "source"):
            _string(getattr(self, name), name)
        _timestamp(self.verified_at, "verified_at")
        _string_list(self.tags, "tags")


@dataclass(frozen=True, kw_only=True)
class JobPosting(_Record):
    title: str
    company: str
    location: str
    description: str
    source_url: str
    requirements: list[dict]
    schedule: dict | None
    work_mode: str
    consulted_at: str

    def __post_init__(self):
        super().__post_init__()
        for name in ("title", "company", "location", "description"):
            _string(getattr(self, name), name)
        _string(self.source_url, "source_url")
        from urllib.parse import urlsplit
        try:
            parsed = urlsplit(self.source_url)
        except ValueError as exc:
            raise ValidationError(f"source_url: invalid URI: {exc}") from exc
        if not ((parsed.scheme in ("http", "https") and parsed.netloc) or
                (parsed.scheme == "fixture" and (parsed.netloc or parsed.path))):
            _fail("source_url", "expected http, https or fixture URI")
        _list(self.requirements, "requirements", _requirement)
        _schedule(self.schedule, "schedule")
        if self.work_mode not in ("onsite", "remote", "hybrid", "unknown") or type(self.work_mode) is not str:
            _fail("work_mode", "invalid work mode")
        _timestamp(self.consulted_at, "consulted_at")


@dataclass(frozen=True, kw_only=True)
class JobAssessment(_Record):
    job_id: str
    profile_id: str
    score: float
    criteria: dict[str, float]
    met_requirements: list[str]
    unmet_requirements: list[str]
    missing_information: list[str]
    recommendation: str
    evidence: list[str]

    def __post_init__(self):
        super().__post_init__()
        _uuid(self.job_id, "job_id")
        _uuid(self.profile_id, "profile_id")
        _number(self.score, "score")
        _criteria(self.criteria, "criteria")
        for name in ("met_requirements", "unmet_requirements", "missing_information", "evidence"):
            _string_list(getattr(self, name), name)
        if type(self.recommendation) is not str or self.recommendation not in ("recommend", "needs_information", "reject"):
            _fail("recommendation", "invalid recommendation")


APPLICATION_STATES = frozenset((
    "DISCOVERED", "SCREENED", "SHORTLISTED", "CV_GENERATING", "CV_REVIEW",
    "CV_APPROVED", "READY_FOR_APPROVAL", "USER_APPROVED", "SUBMITTING",
    "SUBMITTED", "REJECTED", "NEEDS_INFORMATION", "REVISION_REQUIRED",
    "MANUAL_ACTION_REQUIRED", "SUBMISSION_FAILED", "WITHDRAWN",
))


@dataclass(frozen=True, kw_only=True)
class Application(_Record):
    job_id: str
    profile_id: str
    state: str
    revision_count: int
    version: int
    updated_at: str

    def __post_init__(self):
        super().__post_init__()
        _uuid(self.job_id, "job_id")
        _uuid(self.profile_id, "profile_id")
        if type(self.state) is not str or self.state not in APPLICATION_STATES:
            _fail("state", "invalid application state")
        _integer(self.revision_count, "revision_count", 0, 3)
        _integer(self.version, "version", 0)
        _timestamp(self.updated_at, "updated_at")


@dataclass(frozen=True, kw_only=True)
class DocumentArtifact(_Record):
    application_id: str
    kind: str
    source_path: str
    pdf_path: str
    sha256: str
    fact_ids: list[str]
    version: int

    def __post_init__(self):
        super().__post_init__()
        _uuid(self.application_id, "application_id")
        if type(self.kind) is not str or self.kind not in ("cv", "letter"):
            _fail("kind", "invalid artifact kind")
        _string(self.source_path, "source_path")
        _string(self.pdf_path, "pdf_path")
        if type(self.sha256) is not str or not re.fullmatch(r"[0-9a-fA-F]{64}", self.sha256):
            _fail("sha256", "expected 64 hexadecimal characters")
        _uuid_list(self.fact_ids, "fact_ids")
        _integer(self.version, "version", 1)


@dataclass(frozen=True, kw_only=True)
class ReviewReport(_Record):
    application_id: str
    score: float
    approved: bool
    critical_issues: list[str]
    corrections: list[str]
    criteria: dict[str, float]
    artifact_ids: list[str]

    def __post_init__(self):
        super().__post_init__()
        _uuid(self.application_id, "application_id")
        _number(self.score, "score")
        if type(self.approved) is not bool:
            _fail("approved", "expected boolean")
        _string_list(self.critical_issues, "critical_issues")
        _string_list(self.corrections, "corrections")
        _criteria(self.criteria, "criteria")
        _uuid_list(self.artifact_ids, "artifact_ids")
        if self.approved and (self.score < 90 or self.critical_issues):
            _fail("approved", "requires score >= 90 and no critical issues")


@dataclass(frozen=True, kw_only=True)
class AgentTask(_Record):
    application_id: str | None
    agent: str
    objective: str
    input_references: list[str]
    constraints: list[str]
    status: str
    result: dict | None
    errors: list[str]
    evidence: list[str]
    attempts: int
    updated_at: str

    def __post_init__(self):
        super().__post_init__()
        if self.application_id is not None:
            _uuid(self.application_id, "application_id")
        if type(self.agent) is not str or self.agent not in ("coordinator", "scout", "creator", "analyzer", "applicant"):
            _fail("agent", "invalid agent")
        _string(self.objective, "objective")
        for name in ("input_references", "constraints", "errors", "evidence"):
            _string_list(getattr(self, name), name)
        if type(self.status) is not str or self.status not in ("pending", "running", "completed", "failed", "interrupted"):
            _fail("status", "invalid task status")
        if self.result is not None:
            if type(self.result) is not dict:
                _fail("result", "expected object or null")
            _json_value(self.result, "result")
        _integer(self.attempts, "attempts", 0, 3)
        _timestamp(self.updated_at, "updated_at")


@dataclass(frozen=True, kw_only=True)
class EventLog(_Record):
    application_id: str | None
    task_id: str | None
    agent: str
    event_type: str
    previous_state: str | None
    next_state: str | None
    result: dict
    evidence: list[str]
    next_action: str
    idempotency_key: str

    def __post_init__(self):
        super().__post_init__()
        for name in ("application_id", "task_id"):
            value = getattr(self, name)
            if value is not None:
                _uuid(value, name)
        _string(self.agent, "agent")
        _string(self.event_type, "event_type")
        for name in ("previous_state", "next_state"):
            value = getattr(self, name)
            if value is not None:
                _string(value, name)
        if type(self.result) is not dict:
            _fail("result", "expected object")
        _json_value(self.result, "result")
        _string_list(self.evidence, "evidence")
        _string(self.next_action, "next_action")
        _string(self.idempotency_key, "idempotency_key")


def parse_record(kind: str, payload: dict) -> _Record:
    if type(kind) is not str or kind not in _Record._registry:
        _fail("kind", "unknown record kind")
    return _Record._registry[kind].from_dict(payload)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail(key, "duplicate JSON key")
        result[key] = value
    return result


def parse_json(kind: str, raw: str) -> _Record:
    if type(raw) is not str:
        _fail("raw", "expected JSON string")
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_pairs,
                             parse_constant=lambda value: _fail("raw", f"invalid JSON constant {value}"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValidationError(f"raw: malformed JSON: {exc}") from exc
    return parse_record(kind, payload)


__all__ = ["ValidationError", "new_id", "utc_now", "parse_record", "parse_json",
           "CandidateProfile", "VerifiedFact", "JobPosting", "JobAssessment",
           "Application", "DocumentArtifact", "ReviewReport", "AgentTask", "EventLog",
           "APPLICATION_STATES"]
