import json
import unittest
import uuid
from dataclasses import FrozenInstanceError

from job_hunter.contracts import (
    AgentTask, Application, CandidateProfile, DocumentArtifact, EventLog,
    JobAssessment, JobPosting, ReviewReport, ValidationError, VerifiedFact,
    new_id, parse_json, parse_record, utc_now,
)


ID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
NOW = "2026-11-06T12:00:00+00:00"
WINDOW = {"day": 0, "start": "09:00", "end": "17:00"}


def cases():
    return {
        "CandidateProfile": dict(name="Ada", location="Lisbon", fact_ids=[ID],
            preferences={"remote": False, "salary": None},
            availability={"status": "unconfirmed", "confirmed_at": None, "windows": []},
            unconfirmed=["availability"]),
        "VerifiedFact": dict(profile_id=ID, category="skill", statement="Uses Python",
            source="user interview", verified_at=NOW, tags=["python"]),
        "JobPosting": dict(title="Developer", company="Example", location="Lisbon",
            description="Write code", source_url="fixture:job-1",
            requirements=[{"key": "python", "description": "Python", "mandatory": True}],
            schedule={"windows": [WINDOW]}, work_mode="hybrid", consulted_at=NOW),
        "JobAssessment": dict(job_id=ID, profile_id=OTHER, score=90.0,
            criteria={"skills": 90.0}, met_requirements=["python"], unmet_requirements=[],
            missing_information=[], recommendation="recommend", evidence=["fact"]),
        "Application": dict(job_id=ID, profile_id=OTHER, state="DISCOVERED",
            revision_count=0, version=0, updated_at=NOW),
        "DocumentArtifact": dict(application_id=ID, kind="cv", source_path="cv.md",
            pdf_path="cv.pdf", sha256="a" * 64, fact_ids=[OTHER], version=1),
        "ReviewReport": dict(application_id=ID, score=90.0, approved=True,
            critical_issues=[], corrections=[], criteria={"accuracy": 95.0}, artifact_ids=[OTHER]),
        "AgentTask": dict(application_id=None, agent="scout", objective="Find jobs",
            input_references=[], constraints=[], status="pending", result=None,
            errors=[], evidence=[], attempts=0, updated_at=NOW),
        "EventLog": dict(application_id=ID, task_id=None, agent="coordinator",
            event_type="transition", previous_state=None, next_state="DISCOVERED",
            result={"ok": False}, evidence=[], next_action="screen", idempotency_key="key-1"),
    }


class ContractTests(unittest.TestCase):
    def test_all_nine_round_trip_explicit_records(self):
        for kind, body in cases().items():
            with self.subTest(kind=kind):
                payload = {"id": ID, "created_at": NOW, **body}
                record = parse_record(kind, payload)
                self.assertEqual(record.to_dict(), payload)
                self.assertEqual(parse_json(kind, json.dumps(payload)), record)

    def test_generated_identifiers_and_timestamps(self):
        record = CandidateProfile.from_dict(cases()["CandidateProfile"])
        self.assertEqual(uuid.UUID(record.id).version, 4)
        self.assertIn("+", record.created_at)
        self.assertEqual(uuid.UUID(new_id()).version, 4)
        self.assertIn("+", utc_now())

    def test_frozen_record(self):
        record = Application.from_dict(cases()["Application"])
        with self.assertRaises(FrozenInstanceError):
            record.version = 1

    def test_unknown_top_level_field(self):
        with self.assertRaisesRegex(ValidationError, "unknown fields"):
            Application.from_dict({**cases()["Application"], "typo": 1})

    def test_missing_field(self):
        payload = cases()["Application"].copy()
        del payload["version"]
        with self.assertRaisesRegex(ValidationError, "missing fields"):
            Application.from_dict(payload)

    def test_invalid_uuid(self):
        with self.assertRaises(ValidationError):
            Application(job_id="no", **{k: v for k, v in cases()["Application"].items() if k != "job_id"})

    def test_naive_timestamp(self):
        with self.assertRaises(ValidationError):
            Application.from_dict({**cases()["Application"], "updated_at": "2026-11-06T12:00:00"})

    def test_invalid_nested_availability(self):
        payload = cases()["CandidateProfile"]
        with self.assertRaises(ValidationError):
            CandidateProfile.from_dict({**payload, "availability": {"status": "confirmed", "confirmed_at": None, "windows": []}})

    def test_unknown_nested_window_field(self):
        payload = cases()["CandidateProfile"]
        with self.assertRaisesRegex(ValidationError, "unknown fields"):
            CandidateProfile.from_dict({**payload, "availability": {"status": "unconfirmed", "confirmed_at": None, "windows": [{**WINDOW, "timezone": "UTC"}]}})

    def test_bad_window_hours_and_order(self):
        for window in ({"day": 0, "start": "25:00", "end": "17:00"},
                       {"day": 0, "start": "17:00", "end": "09:00"}):
            with self.subTest(window=window), self.assertRaises(ValidationError):
                JobPosting.from_dict({**cases()["JobPosting"], "schedule": {"windows": [window]}})

    def test_requirement_mandatory_is_boolean(self):
        with self.assertRaises(ValidationError):
            JobPosting.from_dict({**cases()["JobPosting"], "requirements": [{"key": "a", "description": "A", "mandatory": 1}]})

    def test_malformed_url_rejected_as_validation_error(self):
        with self.assertRaises(ValidationError):
            JobPosting.from_dict({**cases()["JobPosting"], "source_url": "https://[broken"})

    def test_unknown_nested_requirement_field(self):
        with self.assertRaises(ValidationError):
            JobPosting.from_dict({**cases()["JobPosting"], "requirements": [{"key": "a", "description": "A", "mandatory": True, "extra": 1}]})

    def test_bool_rejected_as_score_and_integer(self):
        with self.assertRaises(ValidationError):
            ReviewReport.from_dict({**cases()["ReviewReport"], "score": True})
        with self.assertRaises(ValidationError):
            Application.from_dict({**cases()["Application"], "version": False})

    def test_nonfinite_numbers_rejected_deeply(self):
        with self.assertRaises(ValidationError):
            JobAssessment.from_dict({**cases()["JobAssessment"], "criteria": {"a": float("nan")}})
        with self.assertRaises(ValidationError):
            AgentTask.from_dict({**cases()["AgentTask"], "result": {"nested": [float("inf")]}})

    def test_approved_report_below_threshold_or_critical(self):
        for change in ({"score": 89.9}, {"critical_issues": ["fabrication"]}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                ReviewReport.from_dict({**cases()["ReviewReport"], **change})

    def test_false_report_below_threshold_allowed(self):
        record = ReviewReport.from_dict({**cases()["ReviewReport"], "score": 20.0, "approved": False})
        self.assertFalse(record.approved)

    def test_invalid_json_and_duplicate_keys(self):
        for raw in ("{", '{"id": 1, "id": 2}',
                    '{"result": {"x": 1, "x": 2}}', '{"score": NaN}'):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                parse_json("Application", raw)

    def test_invalid_json_root_and_kind(self):
        with self.assertRaises(ValidationError):
            parse_json("Application", "[]")
        with self.assertRaises(ValidationError):
            parse_record("Other", {})

    def test_unknown_does_not_become_false(self):
        with self.assertRaises(ValidationError):
            ReviewReport.from_dict({k: v for k, v in cases()["ReviewReport"].items() if k != "approved"})

    def test_mutated_nested_data_rechecked_on_serialization(self):
        record = CandidateProfile.from_dict(cases()["CandidateProfile"])
        record.availability["windows"].append({**WINDOW, "extra": 1})
        with self.assertRaises(ValidationError):
            record.to_dict()


if __name__ == "__main__":
    unittest.main()
