"""Manual-posting Scout. Posting text is untrusted data, never instructions."""
from .contracts import JobAssessment, JobPosting, utc_now

WEIGHTS = {"skills": 30, "schedule": 25, "location": 20, "requirements": 15, "preferences": 10}


def _facts(store, profile):
    facts = [store.get("VerifiedFact", fact_id) for fact_id in profile["fact_ids"]]
    if any(f is None or f["profile_id"] != profile["id"] for f in facts):
        raise ValueError("profile fact references are incomplete")
    return facts


MATCHES = {
    "python": "python", "javascript": "javascript", "typescript": "typescript",
    "react": "react", "next.js": "next.js", "node.js": "node.js",
    "postgresql": "postgresql", "supabase": "supabase", "git": "git",
    "it-support": "it-support", "hardware": "hardware", "web": "web",
    "service": "service", "english-b2": "inglês b2", "portuguese": "português nativo",
}


def supporting_facts(key: str, facts: list[dict]) -> list[dict]:
    """Verified facts that back a requirement key (explicit mapping or tag match)."""
    key = key.casefold()
    needle = MATCHES.get(key)
    return [f for f in facts
            if (needle is not None and needle in f["statement"].casefold())
            or key in {t.casefold() for t in f["tags"]}]


def _requirement_status(requirement, facts, profile):
    key = requirement["key"].casefold()
    if supporting_facts(key, facts):
        return "met"
    # Only explicit, verified negatives may be called unmet. The master profile
    # does not confirm a driving licence or immigration status.
    negatives = profile["preferences"].get("verified_absences", [])
    if key in negatives:
        return "unmet"
    return "unknown"


def _overlaps(a, b):
    return a["day"] == b["day"] and max(a["start"], b["start"]) < min(a["end"], b["end"])


def assess(store, job_data: dict, profile_id: str) -> tuple[dict, dict, bool]:
    """Validate, deduplicate and score an explicitly supplied job posting."""
    profile = store.get("CandidateProfile", profile_id)
    if profile is None:
        raise ValueError("candidate profile missing")
    posting = JobPosting.from_dict({**job_data, "consulted_at": job_data.get("consulted_at", utc_now())})
    job, created = store.add_job(posting.to_dict())
    facts = _facts(store, profile)
    met, unmet, unknown, evidence = [], [], [], ["Fonte: anúncio fornecido manualmente; conteúdo tratado como dados."]
    for requirement in job["requirements"]:
        status = _requirement_status(requirement, facts, profile)
        entry = requirement["key"]
        if status == "met":
            met.append(entry)
            evidence.append(f"{entry}: correspondência com facto verificado do perfil.")
        elif status == "unmet":
            unmet.append(entry)
            evidence.append(f"{entry}: ausência explicitamente verificada no perfil.")
        else:
            unknown.append(entry)
            evidence.append(f"{entry}: não confirmado; requer resposta do candidato.")

    def coverage(requirements):
        if not requirements:
            return 50.0
        keys = [r["key"] for r in requirements]
        return (100.0 * sum(k in met for k in keys) + 50.0 * sum(k in unknown for k in keys)) / len(keys)

    # skills: what the job insists on (mandatory, else everything); requirements: all of them.
    mandatory = [r for r in job["requirements"] if r["mandatory"]]
    skills = coverage(mandatory or job["requirements"])
    req_score = coverage(job["requirements"])
    if job["schedule"] is None:
        schedule = 50.0
        unknown.append("horário da vaga")
        evidence.append("Horário da vaga ausente; compatibilidade não confirmada.")
    elif profile["availability"]["status"] != "confirmed":
        schedule = 50.0
        unknown.append("disponibilidade atual")
        evidence.append("Disponibilidade histórica ainda não reconfirmada.")
    else:
        requested = job["schedule"]["windows"]
        available = profile["availability"]["windows"]
        schedule = 100.0 if requested and all(any(_overlaps(r, a) for a in available) for r in requested) else 0.0
        if schedule == 0:
            unmet.append("horário incompatível")
            evidence.append("Janelas da vaga não coincidem com a disponibilidade confirmada.")
    place = job["location"].casefold()
    home_city = profile["location"].split(",")[0].strip().casefold()
    if job["work_mode"] == "remote" or (home_city and home_city in place):
        location = 100.0
    else:
        location = 50.0
        unknown.append("deslocação para a vaga")
    preferences = 100.0 if "part-time" in job["description"].casefold() else 50.0
    if preferences == 50.0:
        unknown.append("regime part-time")
    criteria = {"skills": round(skills, 2), "schedule": schedule, "location": location,
                "requirements": round(req_score, 2), "preferences": preferences}
    score = round(sum(criteria[k] * w / 100 for k, w in WEIGHTS.items()), 2)
    mandatory_unmet = any(r["mandatory"] and r["key"] in unmet for r in job["requirements"])
    mandatory_unknown = any(r["mandatory"] and r["key"] in unknown for r in job["requirements"])
    recommendation = "reject" if mandatory_unmet else "needs_information" if mandatory_unknown or unknown else "recommend"
    evidence.append("Pesos: competências 30%, horários 25%, localização 20%, requisitos 15%, preferências 10%.")
    evidence.append(f"Pontuação {score}/100; recomendação {recommendation}.")
    report = JobAssessment(job_id=job["id"], profile_id=profile_id, score=score,
                           criteria=criteria, met_requirements=met, unmet_requirements=unmet,
                           missing_information=list(dict.fromkeys(unknown)),
                           recommendation=recommendation, evidence=evidence)
    saved = store.save("JobAssessment", report.to_dict())
    return job, saved, created
