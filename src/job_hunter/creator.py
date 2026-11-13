"""Creator: renders a CV and cover letter (LaTeX -> PDF) from verified profile facts only.

Rules that the independent Analyzer re-checks:
* every ``\\item`` in a document is the verbatim statement of a VerifiedFact;
* job-posting text is untrusted: only company, title and location are used, escaped;
* nothing is said about facts the profile lists as unconfirmed, and availability is
  only mentioned once the candidate has confirmed it.
"""
from __future__ import annotations

from pathlib import Path

from .contracts import DocumentArtifact
from .latex import PROJECT_ROOT, compile_pdf, escape, sha256_file
from .scout import supporting_facts

SECTIONS = [("education", "Formação"), ("experience", "Experiência"),
            ("skills", "Competências"), ("language", "Línguas")]
DAYS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
MAX_LETTER_FACTS = 4
INCLUDE_PREFIX = "include_fact:"

PREAMBLE = r"""\documentclass[11pt,a4paper]{article}
\usepackage[margin=2cm]{geometry}
\usepackage{fontspec}
\usepackage{enumitem}
\setlist[itemize]{leftmargin=*,itemsep=2pt,topsep=2pt}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\setlength{\parskip}{6pt}
"""


def load_context(store, application_id: str) -> dict:
    application = store.get("Application", application_id)
    if application is None:
        raise ValueError("application missing")
    job = store.get("JobPosting", application["job_id"])
    profile = store.get("CandidateProfile", application["profile_id"])
    facts = [store.get("VerifiedFact", fid) for fid in profile["fact_ids"]]
    if job is None or any(f is None for f in facts):
        raise ValueError("application references are incomplete")
    return {"application": application, "job": job, "profile": profile, "facts": facts}


def _relevant(job: dict, facts: list[dict]) -> list[dict]:
    """Facts backing the job's requirements, mandatory ones first, without duplicates."""
    ordered, seen = [], set()
    for requirement in sorted(job["requirements"], key=lambda r: not r["mandatory"]):
        for fact in supporting_facts(requirement["key"], facts):
            if fact["id"] not in seen:
                seen.add(fact["id"])
                ordered.append(fact)
    return ordered


def _requested_extra(corrections, facts):
    wanted = {c[len(INCLUDE_PREFIX):].split()[0] for c in corrections
              if c.startswith(INCLUDE_PREFIX) and c[len(INCLUDE_PREFIX):].split()}
    return [f for f in facts if f["id"] in wanted]


def _items(facts) -> str:
    return "\\begin{itemize}\n" + "".join(f"\\item {escape(f['statement'])}\n" for f in facts) + "\\end{itemize}\n"


def _header(profile: dict) -> str:
    lines = [escape(profile["location"])]
    contact = profile["preferences"].get("contact", {})
    for key in ("email", "phone"):
        if contact.get(key):
            lines.append(escape(contact[key]))
    return "{\\LARGE\\bfseries " + escape(profile["name"]) + "}\\\\[3pt]\n" + " \\textbullet{} ".join(lines) + "\n\n"


def render_cv(profile: dict, job: dict, facts: list[dict]) -> tuple[str, list[str]]:
    relevant_ids = [f["id"] for f in _relevant(job, facts)]
    rank = {fid: i for i, fid in enumerate(relevant_ids)}
    body = [PREAMBLE, "\\begin{document}\n", _header(profile)]
    used = []
    for category, title in SECTIONS:
        group = sorted((f for f in facts if f["category"] == category),
                       key=lambda f: rank.get(f["id"], len(rank)))
        if group:
            body.append(f"\\section*{{{title}}}\n" + _items(group))
            used += [f["id"] for f in group]
    body.append("\\end{document}\n")
    return "".join(body), used


def render_letter(profile: dict, job: dict, facts: list[dict], corrections=()) -> tuple[str, list[str]]:
    chosen = _relevant(job, facts)
    for extra in _requested_extra(corrections, facts):
        if extra not in chosen:
            chosen.append(extra)
    if not chosen:
        raise ValueError("no verified fact supports this job; refusing to write a generic letter")
    requested = {f["id"] for f in _requested_extra(corrections, facts)}
    chosen = [f for f in chosen if f["id"] in requested] + [f for f in chosen if f["id"] not in requested]
    chosen = chosen[:max(MAX_LETTER_FACTS, len(requested))]
    body = [PREAMBLE, "\\begin{document}\n", _header(profile),
            f"Exmos. Senhores da {escape(job['company'])},\n\n",
            f"Venho candidatar-me à vaga de {escape(job['title'])}, em {escape(job['location'])}.\n\n",
            "Do meu percurso, destaco:\n", _items(chosen)]
    availability = profile["availability"]
    if availability["status"] == "confirmed" and availability["windows"]:
        windows = "; ".join(f"{DAYS[w['day']]} {w['start']}–{w['end']}" for w in availability["windows"])
        body.append(f"Disponibilidade confirmada: {escape(windows)}.\n\n")
    body.append("Fico à disposição para uma conversa.\n\nCom os melhores cumprimentos,\\\\\n"
                + escape(profile["name"]) + "\n\\end{document}\n")
    return "".join(body), [f["id"] for f in chosen]


def next_version(store, application_id: str) -> int:
    versions = [a["version"] for a in store.list_records("DocumentArtifact")
                if a["application_id"] == application_id]
    return max(versions, default=0) + 1


def create_documents(store, application_id: str, *, root: Path = PROJECT_ROOT,
                     corrections=()) -> list[dict]:
    """Write, compile and register a new cv + letter version; returns both artifacts."""
    root = Path(root)
    ctx = load_context(store, application_id)
    version = next_version(store, application_id)
    folder = root / "artifacts" / application_id / f"v{version}"
    folder.mkdir(parents=True, exist_ok=True)
    cv_tex, cv_ids = render_cv(ctx["profile"], ctx["job"], ctx["facts"])
    letter_tex, letter_ids = render_letter(ctx["profile"], ctx["job"], ctx["facts"], corrections)
    built = []
    for kind, tex, fact_ids in (("cv", cv_tex, cv_ids), ("letter", letter_tex, letter_ids)):
        source = folder / f"{kind}.tex"
        source.write_text(tex, encoding="utf-8")
        pdf = compile_pdf(source, root=root)
        built.append((kind, source, pdf, fact_ids))
    saved = []
    for kind, source, pdf, fact_ids in built:
        record = DocumentArtifact(application_id=application_id, kind=kind,
                                  source_path=source.relative_to(root).as_posix(),
                                  pdf_path=pdf.relative_to(root).as_posix(),
                                  sha256=sha256_file(pdf), fact_ids=fact_ids, version=version)
        saved.append(store.save("DocumentArtifact", record.to_dict()))
    return saved
