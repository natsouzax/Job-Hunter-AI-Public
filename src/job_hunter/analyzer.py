"""Analyzer: independent review of a CV + letter pair.

Reads only the persisted records and the files on disk (never Creator internals), and
re-verifies fidelity to verified facts, requirement coverage, completeness, PDF integrity
and LaTeX safety. Deterministic: no model judgement is involved, and the report says so.
"""
from __future__ import annotations

import re
from pathlib import Path

from .contracts import ReviewReport
from .latex import PROJECT_ROOT, escape, pdf_page_count, sha256_file
from .scout import supporting_facts

WEIGHTS = {"fidelity": 35, "relevance": 25, "completeness": 15, "format": 15, "safety": 10}
APPROVAL_SCORE = 90.0
INCLUDE_PREFIX = "include_fact:"
ALLOWED_COMMANDS = {
    "documentclass", "usepackage", "setlist", "pagestyle", "setlength", "begin", "end",
    "section", "item", "parindent", "parskip", "LARGE", "bfseries", "textbullet", "textbackslash",
    "textasciitilde", "textasciicircum",
}
ALLOWED_PACKAGES = {"geometry", "fontspec", "enumitem"}
FORBIDDEN_CLAIMS = ["carta de condução", "driving licen", "autorização de trabalho",
                    "work permit", "visto de trabalho", "documentos migratórios", "residência legal"]
MAX_PAGES = {"cv": 2, "letter": 1}


def _current_pair(store, application_id):
    best = {}
    for artifact in store.list_records("DocumentArtifact"):
        if artifact["application_id"] != application_id:
            continue
        previous = best.get(artifact["kind"])
        if previous is None or artifact["version"] > previous["version"]:
            best[artifact["kind"]] = artifact
    if set(best) != {"cv", "letter"}:
        raise ValueError("current cv and letter artifacts required for review")
    if best["cv"]["version"] != best["letter"]["version"]:
        raise ValueError("cv and letter versions differ")
    return best


def _items(tex: str) -> list[str]:
    return [line[len("\\item "):].strip() for line in tex.splitlines() if line.startswith("\\item ")]


def _commands_outside_whitelist(tex: str) -> list[str]:
    found = set(re.findall(r"\\([a-zA-Z]+)", tex)) - ALLOWED_COMMANDS
    packages = set(re.findall(r"\\usepackage(?:\[[^\]]*\])?\{([^}]*)\}", tex))
    return sorted(found) + [f"package:{p}" for p in sorted(packages - ALLOWED_PACKAGES)]


def review(store, application_id: str, *, root: Path = PROJECT_ROOT) -> dict:
    root = Path(root)
    application = store.get("Application", application_id)
    if application is None:
        raise ValueError("application missing")
    job = store.get("JobPosting", application["job_id"])
    profile = store.get("CandidateProfile", application["profile_id"])
    facts = [store.get("VerifiedFact", fid) for fid in profile["fact_ids"]]
    by_statement = {escape(f["statement"]): f for f in facts}
    assessments = [a for a in store.list_records("JobAssessment")
                   if a["job_id"] == job["id"] and a["profile_id"] == profile["id"]]
    assessment = assessments[-1] if assessments else None
    pair = _current_pair(store, application_id)

    critical, corrections = [], []
    checks = {k: [] for k in WEIGHTS}      # name -> list of (passed: bool)
    texts, pdfs = {}, {}

    for kind, artifact in pair.items():
        src, pdf = root / artifact["source_path"], root / artifact["pdf_path"]
        if not src.is_file() or not pdf.is_file():
            critical.append(f"{kind}: ficheiro .tex ou PDF em falta")
            checks["format"].append(False)
            continue
        texts[kind], pdfs[kind] = src.read_text(encoding="utf-8"), pdf

    for kind, tex in texts.items():
        artifact, pdf = pair[kind], pdfs[kind]
        # --- format: PDF integrity -------------------------------------------------
        if not pdf.read_bytes().startswith(b"%PDF"):
            critical.append(f"{kind}: o ficheiro não é um PDF válido")
            checks["format"].append(False)
        else:
            checks["format"].append(True)
        if sha256_file(pdf) != artifact["sha256"].lower():
            critical.append(f"{kind}: PDF alterado depois de registado (sha256 não confere)")
            checks["format"].append(False)
        else:
            checks["format"].append(True)
        pages = pdf_page_count(pdf)
        if pages is not None:
            ok = pages <= MAX_PAGES[kind]
            checks["format"].append(ok)
            if not ok:
                corrections.append(f"{kind}: {pages} páginas; máximo {MAX_PAGES[kind]}")

        # --- fidelity: every item is a verified fact -----------------------------
        declared = {escape(store.get("VerifiedFact", fid)["statement"]) for fid in artifact["fact_ids"]}
        for item in _items(tex):
            fact = by_statement.get(item)
            if fact is None:
                critical.append(f"{kind}: afirmação sem facto verificado: {item[:80]}")
                checks["fidelity"].append(False)
            elif item not in declared:
                critical.append(f"{kind}: facto usado mas não declarado em fact_ids")
                checks["fidelity"].append(False)
            else:
                checks["fidelity"].append(True)
        lowered = tex.casefold()
        unconfirmed = [u.casefold() for u in profile["unconfirmed"] if u.casefold() != "disponibilidade atual"]
        for claim in unconfirmed + FORBIDDEN_CLAIMS:
            if claim in lowered:
                critical.append(f"{kind}: menciona algo não confirmado pelo candidato ({claim})")
                checks["fidelity"].append(False)
        if profile["availability"]["status"] != "confirmed" and "disponibilidade" in lowered:
            critical.append(f"{kind}: declara disponibilidade que o candidato não confirmou")
            checks["fidelity"].append(False)
        for label, value in (("nome", profile["name"]), ("localização", profile["location"])):
            ok = escape(value) in tex
            checks["fidelity"].append(ok)
            if not ok:
                critical.append(f"{kind}: {label} do candidato não corresponde ao perfil")

        # --- safety ----------------------------------------------------------------
        bad = _commands_outside_whitelist(tex)
        checks["safety"].append(not bad)
        if bad:
            critical.append(f"{kind}: comandos LaTeX não permitidos: {', '.join(bad)}")
        copied = [s for s in re.split(r"[.\n]", job["description"]) if len(s.strip()) >= 40
                  and (s.strip().casefold() in lowered or escape(s.strip()).casefold() in lowered)]
        checks["safety"].append(not copied)
        if copied:
            critical.append(f"{kind}: copia texto do anúncio (dados não confiáveis)")

    # --- relevance: requirement coverage ----------------------------------------------
    if assessment is not None and {"cv", "letter"} <= set(texts):
        met = set(assessment["met_requirements"])
        for unmet in assessment["unmet_requirements"]:
            if any(r["key"] == unmet and r["mandatory"] for r in job["requirements"]):
                critical.append(f"requisito obrigatório não cumprido: {unmet}")
                checks["relevance"].append(False)
        for requirement in job["requirements"]:
            if requirement["key"] not in met:
                continue
            weight = 2 if requirement["mandatory"] else 1
            support = {f["id"] for f in supporting_facts(requirement["key"], facts)}
            for kind in ("cv", "letter"):
                covered = bool(support & set(pair[kind]["fact_ids"]))
                checks["relevance"] += [covered] * weight
                if not covered and requirement["mandatory"]:
                    corrections.append(
                        f"{INCLUDE_PREFIX}{sorted(support)[0]} "
                        f"({kind}: requisito obrigatório '{requirement['key']}' sem facto que o sustente)")
    elif assessment is None:
        critical.append("sem avaliação do Scout para esta vaga e perfil")
        checks["relevance"].append(False)

    # --- completeness -------------------------------------------------------------------
    cv, letter = texts.get("cv", ""), texts.get("letter", "")
    checks["completeness"] += [
        "\\section*{Formação}" in cv, "\\section*{Experiência}" in cv,
        escape(job["company"]).casefold() in letter.casefold(),
        escape(job["title"]).casefold() in letter.casefold(),
        "Com os melhores cumprimentos" in letter,
    ]
    has_contact = bool(profile["preferences"].get("contact"))
    checks["completeness"].append(has_contact)
    if not has_contact:
        corrections.append("Falta contacto (email/telefone) no perfil: definir com 'set-contact' antes de enviar")
    for ok, text in zip(checks["completeness"][:5],
                        ["CV sem secção Formação", "CV sem secção Experiência",
                         "carta não menciona a empresa", "carta não menciona o cargo",
                         "carta sem fecho"]):
        if not ok:
            corrections.append(text)

    criteria = {}
    for name in WEIGHTS:
        results = checks[name]
        criteria[name] = round(100.0 * sum(results) / len(results), 2) if results else 0.0
    if not all(checks["fidelity"]):   # fidelity is all-or-nothing
        criteria["fidelity"] = 0.0
    score = round(sum(criteria[k] * w / 100 for k, w in WEIGHTS.items()), 2)
    approved = score >= APPROVAL_SCORE and not critical
    report = ReviewReport(application_id=application_id, score=score, approved=approved,
                          critical_issues=list(dict.fromkeys(critical)),
                          corrections=list(dict.fromkeys(corrections)), criteria=criteria,
                          artifact_ids=[pair["cv"]["id"], pair["letter"]["id"]])
    return store.save("ReviewReport", report.to_dict())
