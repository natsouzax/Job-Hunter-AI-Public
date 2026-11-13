"""Coordinator: drives Scout -> Creator -> Analyzer and owns every Application transition.

Stops at READY_FOR_APPROVAL for human review. The Coordinator never approves, submits or
contacts anyone: only the human (actor "user", via ``approve``) can reach USER_APPROVED, and the
store rejects SUBMITTING / SUBMITTED in this MVP.
"""
from __future__ import annotations

from pathlib import Path

from .analyzer import review as analyzer_review
from .contracts import AgentTask, Application, EventLog, new_id, utc_now
from .creator import create_documents
from .latex import PROJECT_ROOT
from .scout import assess
from .storage import TransitionError

USER = "user"

MAX_REVISIONS = 3


def _task(store, application_id, agent, objective, inputs, fn):
    """Run ``fn`` as a recorded AgentTask; ``fn`` returns (result_dict, evidence_list)."""
    record = AgentTask(application_id=application_id, agent=agent, objective=objective,
                       input_references=inputs, constraints=["sem submissões", "só factos verificados"],
                       status="pending", result=None, errors=[], evidence=[], attempts=0,
                       updated_at=utc_now())
    task = store.save("AgentTask", record.to_dict())
    store.update_task(task["id"], "running")
    try:
        result, evidence = fn()
    except Exception as exc:
        store.update_task(task["id"], "failed", errors=[f"{type(exc).__name__}: {exc}"])
        raise
    done = store.update_task(task["id"], "completed", result=result, evidence=evidence)
    store.save("EventLog", EventLog(
        application_id=application_id, task_id=task["id"], agent=agent, event_type="task_completed",
        previous_state=None, next_state=None, result=result, evidence=evidence,
        next_action="coordinator validates result", idempotency_key=f"task:{task['id']}:completed",
    ).to_dict())
    return done


def _go(store, app, target, evidence, next_action):
    return store.transition(
        app["id"], target, expected_version=app["version"],
        idempotency_key=f"coordinator:{app['id']}:v{app['version']}:{target}",
        evidence=evidence, next_action=next_action)


def _latest(store, kind, **match):
    rows = [r for r in store.list_records(kind) if all(r.get(k) == v for k, v in match.items())]
    return rows[-1] if rows else None


def ensure_application(store, profile_id: str, job_id: str) -> dict:
    for app in store.list_records("Application"):
        if app["profile_id"] == profile_id and app["job_id"] == job_id:
            return app
    record = Application(job_id=job_id, profile_id=profile_id, state="DISCOVERED",
                         revision_count=0, version=0, updated_at=utc_now())
    return store.save("Application", record.to_dict())


def _resume_state(store, application_id):
    for event in reversed(store.events(application_id)):
        if event["next_state"] == "NEEDS_INFORMATION":
            return event["previous_state"]
    return None


def advance(store, application_id: str, *, root: Path = PROJECT_ROOT, resume: bool = False,
            creator=create_documents, analyzer=analyzer_review, max_steps: int = 60) -> dict:
    """Move one application as far as the gates allow and report why it stopped."""
    root, steps, resumed, stop = Path(root), [], False, None
    for _ in range(max_steps):
        app = store.get("Application", application_id)
        state, job_id = app["state"], app["job_id"]

        def moved(target, why, nxt):
            steps.append(f"{state} -> {target}: {why}")
            return _go(store, app, target, [why], nxt)

        if state == "DISCOVERED":
            assessment = _latest(store, "JobAssessment", job_id=job_id, profile_id=app["profile_id"])
            moved("SCREENED", f"avaliação {assessment['id']}" if assessment else "sem avaliação",
                  "decidir shortlist")
        elif state == "SCREENED":
            a = _latest(store, "JobAssessment", job_id=job_id, profile_id=app["profile_id"])
            if a["recommendation"] == "reject":
                moved("REJECTED", f"avaliação {a['id']}: requisito obrigatório não cumprido", "arquivar")
            elif a["recommendation"] == "recommend" and not a["missing_information"]:
                try:
                    moved("SHORTLISTED", f"avaliação {a['id']} recomenda a vaga", "gerar CV e carta")
                except TransitionError as exc:
                    moved("NEEDS_INFORMATION", f"gate recusou shortlist: {exc}", "obter informação em falta")
            else:
                moved("NEEDS_INFORMATION", "informação em falta: " + "; ".join(a["missing_information"]),
                      "candidato deve responder; depois correr 'run --resume'")
        elif state == "NEEDS_INFORMATION":
            target = _resume_state(store, application_id)
            if resume and not resumed and target and app["revision_count"] < MAX_REVISIONS:
                resumed = True
                moved(target, "retoma após resposta do candidato", "continuar pipeline")
            else:
                stop = "needs_information"
                break
        elif state == "SHORTLISTED":
            moved("CV_GENERATING", "vaga na shortlist", "Creator gera CV e carta")
        elif state == "CV_GENERATING":
            last = _latest(store, "ReviewReport", application_id=application_id)
            corrections = last["corrections"] if last and not last["approved"] else []
            task = _task(store, application_id, "creator", "Gerar CV e carta em LaTeX/PDF",
                         [job_id, app["profile_id"]],
                         lambda: _create(store, creator, application_id, root, corrections))
            moved("CV_REVIEW", f"documentos {task['result']['artifact_ids']}", "Analyzer revê")
        elif state == "CV_REVIEW":
            task = _task(store, application_id, "analyzer", "Revisão independente do CV e carta",
                         [application_id], lambda: _review(store, analyzer, application_id, root))
            report = store.get("ReviewReport", task["result"]["report_id"])
            if report["approved"]:
                moved("CV_APPROVED", f"revisão {report['id']} score {report['score']}", "preparar aprovação humana")
            elif app["revision_count"] < MAX_REVISIONS:
                moved("REVISION_REQUIRED", f"revisão {report['id']} score {report['score']}; "
                      f"{len(report['critical_issues'])} críticos", "Creator corrige")
            else:
                moved("NEEDS_INFORMATION", "limite de 3 revisões atingido; escalar ao candidato",
                      "revisão humana")
        elif state == "REVISION_REQUIRED":
            moved("CV_GENERATING", "nova ronda de geração", "Creator corrige")
        elif state == "CV_APPROVED":
            moved("READY_FOR_APPROVAL", "revisão aprovada (score >= 90)", "aguardar decisão humana")
        else:
            stop = "ready_for_human_approval" if state == "READY_FOR_APPROVAL" else state.lower()
            break
    else:
        stop = "max_steps"
    app = store.get("Application", application_id)
    report = _latest(store, "ReviewReport", application_id=application_id)
    assessment = _latest(store, "JobAssessment", job_id=app["job_id"], profile_id=app["profile_id"])
    return {"application_id": application_id, "state": app["state"], "stop_reason": stop,
            "revision_count": app["revision_count"], "steps": steps,
            "missing_information": assessment["missing_information"] if assessment else [],
            "review": report,
            "artifacts": [a for a in store.list_records("DocumentArtifact")
                          if a["application_id"] == application_id
                          and report and a["id"] in report["artifact_ids"]]}


def _create(store, creator, application_id, root, corrections):
    artifacts = creator(store, application_id, root=root, corrections=corrections)
    ids = [a["id"] for a in artifacts]
    return ({"artifact_ids": ids, "version": artifacts[0]["version"]},
            [a["pdf_path"] for a in artifacts])


def _review(store, analyzer, application_id, root):
    report = analyzer(store, application_id, root=root)
    return ({"report_id": report["id"], "score": report["score"], "approved": report["approved"]},
            [f"critical: {c}" for c in report["critical_issues"]])


def run_job(store, profile_id: str, job_data: dict, *, root: Path = PROJECT_ROOT,
            resume: bool = False, **kwargs) -> dict:
    """Entry point: assess a manually supplied posting and advance its application."""
    existing_job = None
    if not resume:
        probe = store.list_records("JobPosting")
        existing_job = next((j for j in probe if j["source_url"] == job_data.get("source_url")), None)
    if existing_job is None or resume:
        def scout():
            job, report, created = assess(store, job_data, profile_id)
            return ({"job_id": job["id"], "assessment_id": report["id"], "created": created,
                     "recommendation": report["recommendation"], "score": report["score"]},
                    report["evidence"][:5])
        done = _task(store, None, "scout", "Avaliar vaga fornecida manualmente",
                     [job_data.get("source_url", "?")], scout)
        job_id = done["result"]["job_id"]
    else:
        job_id = existing_job["id"]
    application = ensure_application(store, profile_id, job_id)
    return advance(store, application["id"], root=root, resume=resume, **kwargs)


def approval_summary(store, application_id: str) -> dict:
    """What the human is asked to approve: the job, the review and the exact files."""
    app = store.get("Application", application_id)
    if app is None:
        raise ValueError("application missing")
    job = store.get("JobPosting", app["job_id"])
    report = _latest(store, "ReviewReport", application_id=application_id)
    artifacts = [store.get("DocumentArtifact", i) for i in (report["artifact_ids"] if report else [])]
    return {"application_id": application_id, "state": app["state"],
            "job": {k: job[k] for k in ("title", "company", "location", "source_url")},
            "review": report and {"score": report["score"], "approved": report["approved"],
                                  "critical_issues": report["critical_issues"],
                                  "corrections": report["corrections"]},
            "documents": [{"kind": a["kind"], "pdf": a["pdf_path"], "sha256": a["sha256"]}
                          for a in artifacts]}


def approve(store, application_id: str, *, root: Path = PROJECT_ROOT,
            analyzer=analyzer_review) -> dict:
    """Human approval of one application. Re-reviews the files first, then records their hashes.

    The recorded sha256 values are what a future Applicant must submit: it may not send
    anything that differs from what was approved here.
    """
    app = store.get("Application", application_id)
    if app is None:
        raise ValueError("application missing")
    if app["state"] != "READY_FOR_APPROVAL":
        raise TransitionError(f"application is {app['state']}, not READY_FOR_APPROVAL")
    report = analyzer(store, application_id, root=Path(root))   # fresh check of files on disk
    if not report["approved"]:
        raise TransitionError("fresh review failed: " + "; ".join(report["critical_issues"] or ["score < 90"]))
    artifacts = [store.get("DocumentArtifact", i) for i in report["artifact_ids"]]
    evidence = [f"review:{report['id']}"] + [
        f"{a['kind']}:{a['pdf_path']}:sha256={a['sha256']}" for a in artifacts]
    return store.transition(
        application_id, "USER_APPROVED", expected_version=app["version"],
        idempotency_key=f"{USER}:{application_id}:v{app['version']}:USER_APPROVED",
        evidence=evidence, next_action="Applicant may prepare the submission (dry run first)", actor=USER)
