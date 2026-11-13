"""Provisional storage tests using a test-process-only contracts validator.

The independent contracts worker's implementation is not copied into this
workspace. These tests verify storage mechanics; integrated validation must
run again with the real job_hunter.contracts module.
"""

import copy
import importlib
import sys
import tempfile
import threading
import types
import unittest
import uuid
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path
from job_hunter import contracts


def uid():
    return str(uuid.uuid4())


def now():
    return datetime.now(timezone.utc).isoformat()


from job_hunter import storage


def base(**fields):
    return {"id": uid(), "created_at": now(), **fields}


def profile():
    return base(name="Synthetic Person", location="Test City", fact_ids=[],
                preferences={}, availability={"status": "unconfirmed", "confirmed_at": None,
                                              "windows": []}, unconfirmed=[])


def job(*, url="https://example.test/jobs/123?utm_source=feed", title="Analyst",
        requirements=None):
    return base(title=title, company="Example Co", location="Test City",
                description="Synthetic role", source_url=url,
                requirements=requirements or [], schedule=None, work_mode="unknown",
                consulted_at=now())


def assessment(job_id, profile_id, *, recommendation="recommend", met=None,
               unmet=None, missing=None):
    return base(job_id=job_id, profile_id=profile_id, score=95.0, criteria={},
                met_requirements=met or [], unmet_requirements=unmet or [],
                missing_information=missing or [], recommendation=recommendation,
                evidence=[])


def application(job_id, profile_id):
    return base(job_id=job_id, profile_id=profile_id, state="DISCOVERED",
                revision_count=0, version=0, updated_at=now())


def artifact(app_id, kind, version=1, fact_ids=None):
    return base(application_id=app_id, kind=kind, source_path=f"{kind}.docx",
                pdf_path=f"{kind}.pdf", sha256="a" * 64, fact_ids=fact_ids or [],
                version=version)


def review(app_id, artifacts):
    return base(application_id=app_id, score=94.0, approved=True,
                critical_issues=[], corrections=[], criteria={},
                artifact_ids=[a["id"] for a in artifacts])


def task(app_id=None):
    return base(application_id=app_id, agent="creator", objective="Synthetic task",
                input_references=[], constraints=[], status="pending", result=None,
                errors=[], evidence=[], attempts=0, updated_at=now())


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.db"
        self.store = storage.Store(self.db)
        self.profile = self.store.save("CandidateProfile", profile())
        self.job = self.store.add_job(job())[0]
        self.app = self.store.save("Application", application(self.job["id"], self.profile["id"]))

    def tearDown(self):
        self.tmp.cleanup()

    def step(self, target, *, key=None, actor="coordinator"):
        self.app = self.store.transition(self.app["id"], target,
                                         expected_version=self.app["version"],
                                         idempotency_key=key or uid(), evidence=[],
                                         next_action="continue", actor=actor)
        return self.app

    def prepare_shortlist(self, *, requirements=None, met=None):
        self.store.save("JobAssessment", assessment(self.job["id"], self.profile["id"], met=met))
        self.step("SCREENED")
        self.step("SHORTLISTED")

    def prepare_review(self):
        self.prepare_shortlist()
        self.step("CV_GENERATING")
        cv = self.store.save("DocumentArtifact", artifact(self.app["id"], "cv"))
        letter = self.store.save("DocumentArtifact", artifact(self.app["id"], "letter"))
        self.step("CV_REVIEW")
        return cv, letter

    def test_reopen_and_immutable_rollback(self):
        self.assertEqual(storage.Store(self.db).get("Application", self.app["id"]), self.app)
        changed = dict(self.profile, name="Changed")
        with self.assertRaises(storage.ConflictError):
            self.store.save("CandidateProfile", changed)
        self.assertEqual(self.store.get("CandidateProfile", self.profile["id"]), self.profile)
        self.assertEqual(self.store.save("CandidateProfile", self.profile), self.profile)
        with self.assertRaises(storage.StorageError):
            self.store.save("Application", application(uid(), self.profile["id"]))
        self.assertEqual(len(self.store.list_records("Application")), 1)

    def test_foreign_references_and_application_identity(self):
        with self.assertRaises(storage.StorageError):
            self.store.save("VerifiedFact", base(profile_id=uid(), category="skill",
                                                   statement="Synthetic", source="user",
                                                   verified_at=now(), tags=[]))
        with self.assertRaises(storage.DuplicateError):
            self.store.save("Application", application(self.job["id"], self.profile["id"]))
        other = self.store.save("CandidateProfile", profile())
        fact = self.store.save("VerifiedFact", base(profile_id=other["id"], category="skill",
                                                      statement="Synthetic", source="user",
                                                      verified_at=now(), tags=[]))
        with self.assertRaises(storage.StorageError):
            self.store.save("DocumentArtifact", artifact(self.app["id"], "cv", fact_ids=[fact["id"]]))

    def test_job_url_and_normalized_identity_dedup(self):
        same_url = job(url="https://example.test/jobs/123?fbclid=abc#apply", title="Other")
        existing, created = self.store.add_job(same_url)
        self.assertFalse(created)
        self.assertEqual(existing, self.job)
        same_identity = job(url="https://other.test/9", title="  ANALYST  ")
        self.assertFalse(self.store.add_job(same_identity)[1])
        with self.assertRaises(storage.DuplicateError):
            self.store.save("JobPosting", same_url)
        new_job = job(url="https://example.test/jobs/124", title="Analyst II")
        self.assertTrue(self.store.add_job(new_job)[1])
        self.assertEqual(len(self.store.list_records("JobPosting")), 2)

    def test_transition_dependencies_unknown_is_not_negative(self):
        with self.assertRaises(storage.TransitionError):
            self.step("SCREENED")
        self.assertEqual(self.store.events(self.app["id"]), [])
        self.store.save("JobAssessment", assessment(self.job["id"], self.profile["id"],
                                                      recommendation="needs_information",
                                                      missing=["availability unconfirmed"]))
        self.step("SCREENED")
        with self.assertRaises(storage.TransitionError):
            self.step("SHORTLISTED")
        self.assertEqual(self.app["state"], "SCREENED")
        self.assertEqual(len(self.store.events(self.app["id"])), 1)

    def test_unverified_mandatory_requirement_blocks_shortlist(self):
        other_job = self.store.add_job(job(url="fixture:mandatory", title="Mandatory",
                                           requirements=[{"key": "license", "description": "License",
                                                          "mandatory": True}]))[0]
        app = self.store.save("Application", application(other_job["id"], self.profile["id"]))
        self.store.save("JobAssessment", assessment(other_job["id"], self.profile["id"]))
        screened = self.store.transition(app["id"], "SCREENED", expected_version=0,
                                         idempotency_key=uid(), evidence=[], next_action="continue")
        with self.assertRaises(storage.TransitionError):
            self.store.transition(app["id"], "SHORTLISTED", expected_version=screened["version"],
                                  idempotency_key=uid(), evidence=[], next_action="continue")

    def test_stale_review_and_artifact_ownership(self):
        cv, letter = self.prepare_review()
        self.store.save("ReviewReport", review(self.app["id"], [cv, letter]))
        newer = self.store.save("DocumentArtifact", artifact(self.app["id"], "cv", 2))
        with self.assertRaises(storage.TransitionError):
            self.step("CV_APPROVED")
        self.store.save("ReviewReport", review(self.app["id"], [newer, letter]))
        self.step("CV_APPROVED")
        self.step("READY_FOR_APPROVAL")
        with self.assertRaises(storage.TransitionError):
            self.step("USER_APPROVED")
        self.assertEqual(self.app["state"], "READY_FOR_APPROVAL")

    def test_revision_bound_and_needs_information_resume(self):
        self.prepare_review()
        for round_number in range(3):
            self.step("REVISION_REQUIRED")
            self.assertEqual(self.app["revision_count"], round_number + 1)
            self.step("CV_GENERATING")
            self.step("CV_REVIEW")
        with self.assertRaises(storage.TransitionError):
            self.step("REVISION_REQUIRED")
        self.step("NEEDS_INFORMATION")
        with self.assertRaises(storage.TransitionError):
            self.step("CV_APPROVED")
        self.step("CV_REVIEW")
        self.assertEqual(self.app["revision_count"], 3)
        self.step("WITHDRAWN")

    def test_idempotency_replay_after_later_state_and_conflict(self):
        self.store.save("JobAssessment", assessment(self.job["id"], self.profile["id"]))
        key = uid()
        first = self.step("SCREENED", key=key)
        self.step("SHORTLISTED")
        replay = self.store.transition(self.app["id"], "SCREENED", expected_version=0,
                                       idempotency_key=key, evidence=[], next_action="continue")
        self.assertEqual(replay, first)
        self.assertEqual(self.store.get("Application", self.app["id"])["state"], "SHORTLISTED")
        with self.assertRaises(storage.ConflictError):
            self.store.transition(self.app["id"], "SHORTLISTED", expected_version=0,
                                  idempotency_key=key, evidence=[], next_action="continue")
        self.assertEqual(len(self.store.events(self.app["id"])), 2)

    def test_event_failure_rolls_back_application_update(self):
        self.store.save("JobAssessment", assessment(self.job["id"], self.profile["id"]))
        original_parse = contracts.parse_record

        def reject_event(kind, value):
            if kind == "EventLog":
                raise ValueError("synthetic event validation failure")
            return original_parse(kind, value)

        with patch.object(contracts, "parse_record", side_effect=reject_event):
            with self.assertRaises(ValueError):
                self.step("SCREENED")
        self.assertEqual(self.store.get("Application", self.app["id"])["version"], 0)
        self.assertEqual(self.store.events(self.app["id"]), [])

    def test_actor_policy_and_unconfirmed_schedule(self):
        scheduled_payload = job(url="fixture:scheduled-2", title="Scheduled II")
        scheduled_payload["schedule"] = {"windows": [{"day": 0, "start": "09:00", "end": "17:00"}]}
        scheduled = self.store.add_job(scheduled_payload)[0]
        app = self.store.save("Application", application(scheduled["id"], self.profile["id"]))
        self.store.save("JobAssessment", assessment(scheduled["id"], self.profile["id"]))
        screened = self.store.transition(app["id"], "SCREENED", expected_version=0,
                                         idempotency_key=uid(), evidence=[], next_action="continue",
                                         actor="coordinator")
        with self.assertRaises(storage.TransitionError):
            self.store.transition(app["id"], "SHORTLISTED", expected_version=1,
                                  idempotency_key=uid(), evidence=[], next_action="continue",
                                  actor="creator")
        with self.assertRaises(storage.TransitionError):
            self.store.transition(app["id"], "SHORTLISTED", expected_version=1,
                                  idempotency_key=uid(), evidence=[], next_action="continue",
                                  actor="analyzer")
        self.assertEqual(self.store.get("Application", app["id"]), screened)

    def test_real_connection_concurrency_one_winner(self):
        self.store.save("JobAssessment", assessment(self.job["id"], self.profile["id"]))
        barrier = threading.Barrier(3)
        results = []
        lock = threading.Lock()

        def compete():
            local = storage.Store(self.db)
            barrier.wait()
            try:
                outcome = local.transition(self.app["id"], "SCREENED", expected_version=0,
                                           idempotency_key=uid(), evidence=[], next_action="continue")
            except Exception as exc:
                outcome = exc
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=compete) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(isinstance(x, dict) for x in results), 1)
        self.assertEqual(sum(isinstance(x, storage.ConflictError) for x in results), 1)
        self.assertEqual(len(self.store.events(self.app["id"])), 1)

    def test_task_recovery_does_not_repeat_completed(self):
        first = self.store.save("AgentTask", task(self.app["id"]))
        second = self.store.save("AgentTask", task(self.app["id"]))
        self.store.update_task(first["id"], "running")
        self.store.update_task(second["id"], "running")
        self.store.update_task(second["id"], "completed", result={"ok": True})
        recovered = self.store.recover_tasks()
        self.assertEqual([row["id"] for row in recovered], [first["id"]])
        self.assertEqual(self.store.recover_tasks(), [])
        self.assertEqual(self.store.get("AgentTask", second["id"])["status"], "completed")
        with self.assertRaises(storage.ConflictError):
            self.store.update_task(second["id"], "running")
        resumed = self.store.update_task(first["id"], "running")
        self.assertEqual(resumed["attempts"], 2)
        self.assertEqual(self.store.events(self.app["id"])[0]["event_type"], "task_recovered")

    def test_task_attempt_limit(self):
        work = self.store.save("AgentTask", task())
        for _ in range(3):
            work = self.store.update_task(work["id"], "running")
            work = self.store.update_task(work["id"], "failed")
        with self.assertRaises(storage.StorageError):
            self.store.update_task(work["id"], "running")
        self.assertEqual(self.store.get("AgentTask", work["id"])["attempts"], 3)


if __name__ == "__main__":
    unittest.main()
