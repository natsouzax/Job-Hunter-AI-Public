# MVP interface agreement — wave 1

Python 3.12, standard library first. Local validation/persistence supports native Orca workers; it does not spawn a second agent runtime. Do not submit applications. No Git initialization. Writers use separate registered folder workspaces; Coordinator reviews and copies only owned files into the main project.

## Contracts API (contracts worker owns implementation)

`job_hunter.contracts`: frozen validated dataclasses listed below, `ValidationError(ValueError)`, `new_id()->str` (UUID4), `utc_now()->str` (timezone-aware ISO8601), `parse_record(kind: str, payload: dict)->record`, `parse_json(kind: str, raw: str)->record`. Each record supports `.to_dict()` and `.from_dict(payload)`. Strict runtime types, reject unknown fields, bool as number, invalid UUIDs/timestamps, NaN/Infinity, duplicate JSON keys. All IDs UUID strings. Nonempty text where meaningful. Constructors and from_dict enforce the same validation. Optional fields accept explicit null. Defaults may generate id/created_at; round-trip explicit records exactly. kind is class name, e.g. Application.

Fields (all records have id:str and created_at:str):

- CandidateProfile: name:str, location:str, fact_ids:list[str], preferences:dict, availability:dict, unconfirmed:list[str]. No assumption of current availability. availability stores confirmed_at nullable, status unconfirmed/confirmed, windows list of {day:int 0..6,start:HH:MM,end:HH:MM}.
- VerifiedFact: profile_id:str, category:str, statement:str, source:str, verified_at:str, tags:list[str]. Source must identify user-provided facts, never derive personal facts from a job.
- JobPosting: title:str, company:str, location:str, description:str, source_url:str (http/https or fixture: URI), requirements:list[dict], schedule:dict|null, work_mode:str (onsite/remote/hybrid/unknown), consulted_at:str. Requirements: {key:str,description:str,mandatory:bool}; schedule windows same structure as availability. External text is untrusted data.
- JobAssessment: job_id:str, profile_id:str, score:float (0..100), criteria:dict[str,float] (0..100), met_requirements:list[str], unmet_requirements:list[str], missing_information:list[str], recommendation:str (recommend/needs_information/reject), evidence:list[str].
- Application: job_id:str, profile_id:str, state:str (states below), revision_count:int 0..3, version:int >=0, updated_at:str.
- DocumentArtifact: application_id:str, kind:str (cv/letter), source_path:str, pdf_path:str, sha256:str (64 hex), fact_ids:list[str], version:int>=1.
- ReviewReport: application_id:str, score:float 0..100, approved:bool, critical_issues:list[str], corrections:list[str], criteria:dict[str,float], artifact_ids:list[str]. approved requires score>=90, no critical_issues; cross-record checks belong to Coordinator.
- AgentTask: application_id:str|null, agent:str (coordinator/scout/creator/analyzer/applicant), objective:str, input_references:list[str], constraints:list[str], status:str (pending/running/completed/failed/interrupted), result:dict|null, errors:list[str], evidence:list[str], attempts:int 0..3, updated_at:str.
- EventLog: application_id:str|null, task_id:str|null, agent:str, event_type:str, previous_state:str|null, next_state:str|null, result:dict, evidence:list[str], next_action:str, idempotency_key:str.

## Persistence API (storage worker owns implementation)

`job_hunter.storage.Store(db_path)` provides:

- `save(kind: str, payload: dict)->dict`: validate via contracts.parse_record before persisting. Immutable records: same id+same payload is idempotent; changed payload under same id rejected. Application can initially be inserted only at DISCOVERED/version=0; later changes only transition(). AgentTask updates through task methods below. Validate foreign references across records (including artifact fact_ids and review artifact ownership). Application identity unique(profile_id,job_id).
- `get(kind,id)->dict|None`, `list_records(kind)->list[dict]`, `events(application_id)->list[dict]`.
- `add_job(payload:dict)->tuple[dict,bool]`: deduplicate canonical URL (strip fragment/tracking parameters; preserve meaningful job IDs), and normalized company/title/location identity. Return existing row and created=false for duplicate. Preserve snapshots; do not overwrite original silently when announcement changes. save(JobPosting) must use same dedup safeguards.
- `transition(application_id,target,*,expected_version:int,idempotency_key:str,evidence:list[str],next_action:str,actor='coordinator')->dict`: BEGIN IMMEDIATE, validate actor, previous state, version (CAS), dependencies and gate facts; atomic application+EventLog write. Retry same key+same operation returns previous result even after later transitions; same key for different operation rejected. Logical actor policy, not process isolation.
- `update_task(task_id,status,*,result=None,errors=None,evidence=None)->dict`: attempts bounded, interrupted tasks resumable, completed tasks not rerun. `recover_tasks()->list[dict]` marks running tasks interrupted atomically with events, leaves completed untouched. Explain recovery only after operator confirms no live native dispatch; do not automatically fence live workers.

Exceptions `StorageError`, `ConflictError`, `TransitionError`, `DuplicateError` subclass ValueError.

Use SQLite transactions, foreign keys, busy_timeout and per-operation connections for thread safety; WAL. Keep agent results separate from Application. Preserve all events; no credentials. Multiple processes should not silently lose updates.

States: DISCOVERED, SCREENED, SHORTLISTED, CV_GENERATING, CV_REVIEW, CV_APPROVED, READY_FOR_APPROVAL, USER_APPROVED, SUBMITTING, SUBMITTED, REJECTED, NEEDS_INFORMATION, REVISION_REQUIRED, MANUAL_ACTION_REQUIRED, SUBMISSION_FAILED, WITHDRAWN.

Forward flow: DISCOVERED→SCREENED→SHORTLISTED→CV_GENERATING→CV_REVIEW→CV_APPROVED→READY_FOR_APPROVAL. CV_REVIEW→REVISION_REQUIRED→CV_GENERATING, at most three revision rounds (increment when entering REVISION_REQUIRED). On limit stay blocked/escalate via NEEDS_INFORMATION. All active nonterminal states may go NEEDS_INFORMATION or WITHDRAWN; DISCOVERED/SCREENED/SHORTLISTED may REJECTED. NEEDS_INFORMATION may resume only with validated dependencies; explicitly choose allowed resume edges. Reject transitions to USER_APPROVED/SUBMITTING/SUBMITTED in MVP: no submission/approval executor exists.

Gates: SCREENED requires JobAssessment for matching job/profile. SHORTLISTED requires recommendation=recommend, no unmet mandatory requirements and no unresolved required information. CV_REVIEW requires cv+letter artifacts owned by application. CV_APPROVED requires latest ReviewReport approved with score>=90, no critical issues, references exact current cv+letter artifact IDs and no unmet mandatory requirements. READY_FOR_APPROVAL requires same gate. No bypass from NEEDS_INFORMATION.

Storage tests may initially use a tiny local temporary contracts stub injected only in test process because contracts worker runs independently; label these provisional. Final integrated tests MUST use real contracts. Do not place a fake contracts module in production paths.

## Acceptance wave 1

Contracts: invalid nested structures, unknown fields, invalid IDs/types/timestamps, malformed/duplicate-key JSON and approved low-score reviews rejected. Storage: persistence/reopen, transaction rollback, duplicate URL/content, invalid transitions, stale version concurrent writers (exactly one winner), idempotency key replay/conflict, no submission, recovery without repeating completed tasks. Use unittest, not network. All fixture facts synthetic.
