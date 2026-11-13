"""SQLite persistence and guarded MVP application transitions.

Call ``recover_tasks`` only after an operator has confirmed that no native
dispatch is still working on the recorded tasks. This store cannot fence an
external worker.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class StorageError(ValueError):
    pass


class ConflictError(StorageError):
    pass


class TransitionError(StorageError):
    pass


class DuplicateError(StorageError):
    pass


KINDS = frozenset({
    "CandidateProfile", "VerifiedFact", "JobPosting", "JobAssessment",
    "Application", "DocumentArtifact", "ReviewReport", "AgentTask", "EventLog",
})
ACTORS = frozenset({"coordinator", "scout", "creator", "analyzer", "applicant"})
STATES = frozenset({
    "DISCOVERED", "SCREENED", "SHORTLISTED", "CV_GENERATING", "CV_REVIEW",
    "CV_APPROVED", "READY_FOR_APPROVAL", "USER_APPROVED", "SUBMITTING",
    "SUBMITTED", "REJECTED", "NEEDS_INFORMATION", "REVISION_REQUIRED",
    "MANUAL_ACTION_REQUIRED", "SUBMISSION_FAILED", "WITHDRAWN",
})
FORWARD = {
    "DISCOVERED": {"SCREENED"},
    "SCREENED": {"SHORTLISTED"},
    "SHORTLISTED": {"CV_GENERATING"},
    "CV_GENERATING": {"CV_REVIEW"},
    "CV_REVIEW": {"CV_APPROVED", "REVISION_REQUIRED"},
    "REVISION_REQUIRED": {"CV_GENERATING"},
    "CV_APPROVED": {"READY_FOR_APPROVAL"},
}
ACTIVE = frozenset(FORWARD) | {"READY_FOR_APPROVAL"}
BLOCKED_SUBMISSION = frozenset({"SUBMITTING", "SUBMITTED"})
# Logical actor policy. Only the human ("user") may approve; no one may submit in this MVP.
ACTOR_TARGETS = {"coordinator": STATES - {"USER_APPROVED"},
                 "user": frozenset({"USER_APPROVED", "WITHDRAWN"})}

TRACKING = frozenset({
    "gclid", "fbclid", "msclkid", "dclid", "yclid", "mc_cid", "mc_eid",
    "igshid",
})


def _contracts():
    # The contracts worker owns this production module. Import only at runtime
    # so an isolated storage test can inject a clearly test-only validator.
    from job_hunter import contracts
    return contracts


def _validate(kind: str, payload: dict) -> dict:
    if kind not in KINDS:
        raise StorageError(f"unknown record kind: {kind}")
    return _contracts().parse_record(kind, payload).to_dict()


def _dump(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _identity_part(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(value.split())


def _job_identity(job: dict) -> str:
    # These fields affect screening and distinguish materially different
    # postings that happen to share a title and location.
    return _dump({
        "company": _identity_part(job["company"]),
        "title": _identity_part(job["title"]),
        "location": _identity_part(job["location"]),
        "schedule": job["schedule"],
        "work_mode": job["work_mode"],
        "requirements": [
            {"key": _identity_part(item["key"]),
             "description": _identity_part(item["description"]),
             "mandatory": item["mandatory"]}
            for item in job["requirements"]
        ],
    })


def _canonical_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme.lower() == "fixture":
        return urlunsplit(("fixture", parts.netloc.casefold(), parts.path,
                           urlencode(sorted((k, v) for k, v in parse_qsl(parts.query,
                                                                          keep_blank_values=True)
                                            if not k.casefold().startswith("utm_")
                                            and k.casefold() not in TRACKING)), ""))
    host = (parts.hostname or "").casefold()
    try:
        port = parts.port
    except ValueError as exc:
        raise StorageError("invalid job URL port") from exc
    if port and not ((parts.scheme.lower() == "http" and port == 80) or
                     (parts.scheme.lower() == "https" and port == 443)):
        host += f":{port}"
    if parts.username or parts.password:
        raise StorageError("job URL must not contain credentials")
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if not k.casefold().startswith("utm_") and k.casefold() not in TRACKING)
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/") or "/",
                       urlencode(query), ""))


class Store:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT NOT NULL,
                    id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    canonical_url TEXT,
                    job_identity TEXT,
                    profile_id TEXT,
                    job_id TEXT,
                    state TEXT,
                    version INTEGER,
                    PRIMARY KEY (kind, id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_job_url
                    ON records(canonical_url) WHERE kind='JobPosting';
                CREATE UNIQUE INDEX IF NOT EXISTS uq_job_identity
                    ON records(job_identity) WHERE kind='JobPosting';
                CREATE UNIQUE INDEX IF NOT EXISTS uq_application_identity
                    ON records(profile_id, job_id) WHERE kind='Application';
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    application_id TEXT,
                    task_id TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_events_application
                    ON events(application_id, sequence);
            """)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _row(conn, kind: str, record_id: str):
        if kind == "EventLog":
            row = conn.execute("SELECT payload FROM events WHERE id=?", (record_id,)).fetchone()
        else:
            row = conn.execute("SELECT payload FROM records WHERE kind=? AND id=?",
                               (kind, record_id)).fetchone()
        return json.loads(row["payload"]) if row else None

    @classmethod
    def _require(cls, conn, kind, record_id):
        row = cls._row(conn, kind, record_id)
        if row is None:
            raise StorageError(f"missing {kind} reference: {record_id}")
        return row

    def _references(self, conn, kind, item):
        if kind == "CandidateProfile":
            for fact_id in item["fact_ids"]:
                fact = self._require(conn, "VerifiedFact", fact_id)
                if fact["profile_id"] != item["id"]:
                    raise StorageError("profile fact belongs to another profile")
        elif kind == "VerifiedFact":
            self._require(conn, "CandidateProfile", item["profile_id"])
        elif kind == "JobAssessment":
            self._require(conn, "JobPosting", item["job_id"])
            self._require(conn, "CandidateProfile", item["profile_id"])
        elif kind == "Application":
            self._require(conn, "JobPosting", item["job_id"])
            self._require(conn, "CandidateProfile", item["profile_id"])
        elif kind == "DocumentArtifact":
            application = self._require(conn, "Application", item["application_id"])
            for fact_id in item["fact_ids"]:
                fact = self._require(conn, "VerifiedFact", fact_id)
                if fact["profile_id"] != application["profile_id"]:
                    raise StorageError("artifact fact belongs to another profile")
        elif kind == "ReviewReport":
            self._require(conn, "Application", item["application_id"])
            for artifact_id in item["artifact_ids"]:
                artifact = self._require(conn, "DocumentArtifact", artifact_id)
                if artifact["application_id"] != item["application_id"]:
                    raise StorageError("review artifact belongs to another application")
        elif kind == "AgentTask" and item["application_id"] is not None:
            self._require(conn, "Application", item["application_id"])
        elif kind == "EventLog":
            if item["application_id"] is not None:
                self._require(conn, "Application", item["application_id"])
            if item["task_id"] is not None:
                self._require(conn, "AgentTask", item["task_id"])

    def _save(self, conn, kind, item):
        old = self._row(conn, kind, item["id"])
        if old is not None:
            if old == item:
                return old
            raise ConflictError(f"{kind} id already has different content")
        self._references(conn, kind, item)
        if kind == "Application":
            if item["state"] != "DISCOVERED" or item["version"] != 0 or item["revision_count"] != 0:
                raise TransitionError("new application must be DISCOVERED at version 0")
        if kind == "AgentTask" and (item["status"] != "pending" or item["attempts"] != 0):
            raise StorageError("new task must be pending with zero attempts")
        if kind == "EventLog":
            try:
                conn.execute("INSERT INTO events(id,application_id,task_id,idempotency_key,payload) "
                             "VALUES (?,?,?,?,?)",
                             (item["id"], item["application_id"], item["task_id"],
                              item["idempotency_key"], _dump(item)))
            except sqlite3.IntegrityError as exc:
                raise DuplicateError("event idempotency key already used") from exc
            return item
        columns = {"canonical_url": None, "job_identity": None, "profile_id": None,
                   "job_id": None, "state": None, "version": None}
        if kind == "JobPosting":
            columns["canonical_url"] = _canonical_url(item["source_url"])
            columns["job_identity"] = _job_identity(item)
            duplicate = conn.execute(
                "SELECT payload FROM records WHERE kind='JobPosting' "
                "AND (canonical_url=? OR job_identity=?) LIMIT 1",
                (columns["canonical_url"], columns["job_identity"])).fetchone()
            if duplicate:
                raise DuplicateError("job URL or normalized identity already exists")
        if kind == "Application":
            columns.update(profile_id=item["profile_id"], job_id=item["job_id"],
                           state=item["state"], version=item["version"])
        try:
            conn.execute("INSERT INTO records(kind,id,payload,canonical_url,job_identity,"
                         "profile_id,job_id,state,version) VALUES (?,?,?,?,?,?,?,?,?)",
                         (kind, item["id"], _dump(item), *columns.values()))
        except sqlite3.IntegrityError as exc:
            raise DuplicateError("record identity already exists") from exc
        return item

    def save(self, kind: str, payload: dict) -> dict:
        item = _validate(kind, payload)
        with self._write() as conn:
            return self._save(conn, kind, item)

    def update_profile(self, profile_id: str, *, expected: dict, changes: dict) -> dict:
        """Update preferences, availability or fact references with a checked snapshot."""
        allowed = {"preferences", "availability", "fact_ids", "unconfirmed"}
        if not changes or set(changes) - allowed:
            raise StorageError("only profile preferences, availability and facts may change")
        with self._write() as conn:
            old = self._require(conn, "CandidateProfile", profile_id)
            if old != expected:
                raise ConflictError("stale profile snapshot")
            updated = _validate("CandidateProfile", {**old, **changes})
            self._references(conn, "CandidateProfile", updated)
            conn.execute("UPDATE records SET payload=? WHERE kind='CandidateProfile' AND id=?",
                         (_dump(updated), profile_id))
            return updated

    def get(self, kind: str, record_id: str) -> dict | None:
        if kind not in KINDS:
            raise StorageError(f"unknown record kind: {kind}")
        with self._connection() as conn:
            return self._row(conn, kind, record_id)

    def list_records(self, kind: str) -> list[dict]:
        if kind not in KINDS:
            raise StorageError(f"unknown record kind: {kind}")
        with self._connection() as conn:
            if kind == "EventLog":
                rows = conn.execute("SELECT payload FROM events ORDER BY sequence").fetchall()
            else:
                rows = conn.execute("SELECT payload FROM records WHERE kind=? ORDER BY rowid",
                                    (kind,)).fetchall()
            return [json.loads(row["payload"]) for row in rows]

    def events(self, application_id: str) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute("SELECT payload FROM events WHERE application_id=? "
                                "ORDER BY sequence", (application_id,)).fetchall()
            return [json.loads(row["payload"]) for row in rows]

    def add_job(self, payload: dict) -> tuple[dict, bool]:
        item = _validate("JobPosting", payload)
        url, identity = _canonical_url(item["source_url"]), _job_identity(item)
        with self._write() as conn:
            existing = self._row(conn, "JobPosting", item["id"])
            if existing:
                if existing == item:
                    return existing, False
                raise ConflictError("JobPosting id already has different content")
            row = conn.execute("SELECT payload FROM records WHERE kind='JobPosting' "
                               "AND (canonical_url=? OR job_identity=?) ORDER BY rowid LIMIT 1",
                               (url, identity)).fetchone()
            if row:
                return json.loads(row["payload"]), False
            return self._save(conn, "JobPosting", item), True

    def _latest_assessment(self, conn, application):
        rows = conn.execute("SELECT payload FROM records WHERE kind='JobAssessment' "
                            "ORDER BY rowid DESC").fetchall()
        for row in rows:
            assessment = json.loads(row["payload"])
            if (assessment["job_id"] == application["job_id"] and
                    assessment["profile_id"] == application["profile_id"]):
                return assessment
        return None

    def _require_assessment(self, conn, application):
        assessment = self._latest_assessment(conn, application)
        if assessment is None:
            raise TransitionError("matching assessment required")
        return assessment

    def _require_shortlist(self, conn, application):
        assessment = self._require_assessment(conn, application)
        if assessment["recommendation"] != "recommend":
            raise TransitionError("assessment does not recommend this job")
        if assessment["missing_information"]:
            raise TransitionError("required information remains unresolved")
        job = self._require(conn, "JobPosting", application["job_id"])
        profile = self._require(conn, "CandidateProfile", application["profile_id"])
        if job["schedule"] is not None and profile["availability"]["status"] != "confirmed":
            raise TransitionError("schedule needs confirmed availability")
        mandatory = {r["key"] for r in job["requirements"] if r["mandatory"]}
        met = set(assessment["met_requirements"])
        unmet = set(assessment["unmet_requirements"])
        if mandatory & unmet:
            raise TransitionError("unmet mandatory requirement")
        if mandatory - met:
            raise TransitionError("mandatory requirement has no verified assessment")
        return assessment

    def _current_artifacts(self, conn, application_id):
        rows = conn.execute("SELECT payload FROM records WHERE kind='DocumentArtifact' "
                            "ORDER BY rowid DESC").fetchall()
        found = {}
        for row in rows:
            artifact = json.loads(row["payload"])
            if artifact["application_id"] != application_id:
                continue
            previous = found.get(artifact["kind"])
            if previous is None or artifact["version"] > previous["version"]:
                found[artifact["kind"]] = artifact
        if set(found) != {"cv", "letter"}:
            raise TransitionError("current CV and letter artifacts required")
        return found

    def _require_review(self, conn, application):
        self._require_shortlist(conn, application)
        current = self._current_artifacts(conn, application["id"])
        rows = conn.execute("SELECT payload FROM records WHERE kind='ReviewReport' "
                            "ORDER BY rowid DESC").fetchall()
        review = next((json.loads(row["payload"]) for row in rows
                       if json.loads(row["payload"])["application_id"] == application["id"]),
                      None)
        if review is None or not review["approved"] or review["score"] < 90 or review["critical_issues"]:
            raise TransitionError("approved review with score at least 90 required")
        if set(review["artifact_ids"]) != {current["cv"]["id"], current["letter"]["id"]} \
                or len(review["artifact_ids"]) != 2:
            raise TransitionError("review is stale for current artifacts")

    def _gate(self, conn, application, target):
        if target == "SCREENED":
            self._require_assessment(conn, application)
        elif target in {"SHORTLISTED", "CV_GENERATING"}:
            self._require_shortlist(conn, application)
        elif target == "CV_REVIEW":
            self._require_shortlist(conn, application)
            self._current_artifacts(conn, application["id"])
        elif target in {"CV_APPROVED", "READY_FOR_APPROVAL", "USER_APPROVED"}:
            self._require_review(conn, application)

    def _resume_state(self, conn, application_id):
        row = conn.execute("SELECT payload FROM events WHERE application_id=? "
                           "ORDER BY sequence DESC", (application_id,)).fetchall()
        for event_row in row:
            event = json.loads(event_row["payload"])
            if event["next_state"] == "NEEDS_INFORMATION":
                return event["previous_state"]
        return None

    def transition(self, application_id, target, *, expected_version: int,
                   idempotency_key: str, evidence: list[str], next_action: str,
                   actor: str = "coordinator") -> dict:
        if actor not in ACTOR_TARGETS:
            raise TransitionError("only coordinator or user may transition applications")
        if target not in STATES:
            raise TransitionError("unknown target state")
        if target not in ACTOR_TARGETS[actor]:
            raise TransitionError(f"actor {actor} cannot transition to {target}")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise TransitionError("expected_version must be a nonnegative integer")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise TransitionError("idempotency_key required")
        if not isinstance(evidence, list) or any(not isinstance(x, str) for x in evidence):
            raise TransitionError("evidence must be a list of strings")
        if not isinstance(next_action, str) or not next_action.strip():
            raise TransitionError("next_action required")
        request = {"application_id": application_id, "target": target,
                   "expected_version": expected_version, "evidence": evidence,
                   "next_action": next_action, "actor": actor}
        with self._write() as conn:
            old_event = conn.execute("SELECT payload FROM events WHERE idempotency_key=?",
                                     (idempotency_key,)).fetchone()
            if old_event:
                result = json.loads(old_event["payload"])["result"]
                if result.get("request") == request:
                    return result["application"]
                raise ConflictError("idempotency key belongs to another operation")
            application = self._require(conn, "Application", application_id)
            if application["version"] != expected_version:
                raise ConflictError("stale application version")
            previous = application["state"]
            if target in BLOCKED_SUBMISSION:
                raise TransitionError("approval and submission are blocked in MVP")
            allowed = target in FORWARD.get(previous, set())
            if previous in ACTIVE and target in {"NEEDS_INFORMATION", "WITHDRAWN"}:
                allowed = True
            if previous in {"DISCOVERED", "SCREENED", "SHORTLISTED"} and target == "REJECTED":
                allowed = True
            if previous == "READY_FOR_APPROVAL" and target == "USER_APPROVED":
                allowed = True            # actor policy above restricts this to the human
            if previous == "USER_APPROVED" and target == "WITHDRAWN":
                allowed = True
            if previous == "NEEDS_INFORMATION":
                allowed = target == "WITHDRAWN" or target == self._resume_state(conn, application_id)
            if not allowed:
                raise TransitionError(f"transition {previous} -> {target} is not allowed")
            if target == "REVISION_REQUIRED" and application["revision_count"] >= 3:
                raise TransitionError("revision limit reached; escalate to NEEDS_INFORMATION")
            self._gate(conn, application, target)
            updated = dict(application)
            updated["state"] = target
            updated["version"] += 1
            if target == "REVISION_REQUIRED":
                updated["revision_count"] += 1
            updated["updated_at"] = _contracts().utc_now()
            updated = _validate("Application", updated)
            conn.execute("UPDATE records SET payload=?, state=?, version=? "
                         "WHERE kind='Application' AND id=? AND version=?",
                         (_dump(updated), target, updated["version"], application_id,
                          expected_version))
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ConflictError("application changed concurrently")
            event = _validate("EventLog", {
                "id": _contracts().new_id(), "created_at": _contracts().utc_now(),
                "application_id": application_id, "task_id": None, "agent": actor,
                "event_type": "application_transition", "previous_state": previous,
                "next_state": target, "result": {"request": request, "application": updated},
                "evidence": evidence, "next_action": next_action,
                "idempotency_key": idempotency_key,
            })
            self._save(conn, "EventLog", event)
            return updated

    def update_task(self, task_id, status, *, result=None, errors=None, evidence=None) -> dict:
        if status not in {"pending", "running", "completed", "failed", "interrupted"}:
            raise StorageError("invalid task status")
        with self._write() as conn:
            old = self._require(conn, "AgentTask", task_id)
            if old["status"] == "completed":
                raise ConflictError("completed task cannot be rerun")
            allowed = {
                "pending": {"running"},
                "running": {"completed", "failed", "interrupted"},
                "failed": {"running"},
                "interrupted": {"running"},
            }
            if status not in allowed.get(old["status"], set()):
                raise StorageError(f"task transition {old['status']} -> {status} is not allowed")
            updated = dict(old)
            updated["status"] = status
            if status == "running":
                if old["attempts"] >= 3:
                    raise StorageError("task attempt limit reached")
                updated["attempts"] += 1
            if result is not None:
                updated["result"] = result
            if errors is not None:
                updated["errors"] = errors
            if evidence is not None:
                updated["evidence"] = evidence
            updated["updated_at"] = _contracts().utc_now()
            updated = _validate("AgentTask", updated)
            conn.execute("UPDATE records SET payload=? WHERE kind='AgentTask' AND id=?",
                         (_dump(updated), task_id))
            return updated

    def recover_tasks(self) -> list[dict]:
        """Interrupt recorded running tasks after external liveness is confirmed absent."""
        recovered = []
        with self._write() as conn:
            rows = conn.execute("SELECT payload FROM records WHERE kind='AgentTask' "
                                "ORDER BY rowid").fetchall()
            for row in rows:
                task = json.loads(row["payload"])
                if task["status"] != "running":
                    continue
                updated = dict(task)
                updated["status"] = "interrupted"
                updated["updated_at"] = _contracts().utc_now()
                updated = _validate("AgentTask", updated)
                conn.execute("UPDATE records SET payload=? WHERE kind='AgentTask' AND id=?",
                             (_dump(updated), task["id"]))
                event = _validate("EventLog", {
                    "id": _contracts().new_id(), "created_at": _contracts().utc_now(),
                    "application_id": task["application_id"], "task_id": task["id"],
                    "agent": task["agent"], "event_type": "task_recovered",
                    "previous_state": None, "next_state": None,
                    "result": {"previous_status": "running", "status": "interrupted"},
                    "evidence": [], "next_action": "resume task after operator liveness check",
                    "idempotency_key": f"task-recovery:{task['id']}:{task['attempts']}",
                })
                self._save(conn, "EventLog", event)
                recovered.append(updated)
        return recovered
