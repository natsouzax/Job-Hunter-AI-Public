"""Command line: ``python -m job_hunter <command>``."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import profile as profile_mod
from .analyzer import review
from .coordinator import approval_summary, approve, run_job
from .creator import create_documents
from .latex import PROJECT_ROOT, LatexUnavailable, find_tectonic
from .storage import Store, StorageError

DEFAULT_DB = PROJECT_ROOT / "data" / "job_hunter.sqlite3"
DEMO_DB = PROJECT_ROOT / "data" / "demo.sqlite3"
DEMO_JOB = PROJECT_ROOT / "examples" / "demo_job.json"


def _out(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _profile_id(store, explicit):
    if explicit:
        return explicit
    profiles = store.list_records("CandidateProfile")
    if len(profiles) != 1:
        raise SystemExit("no unique profile: run 'init' first or pass --profile")
    return profiles[0]["id"]


def _summary(result: dict) -> dict:
    review_ = result["review"]
    return {"state": result["state"], "stop_reason": result["stop_reason"],
            "revisions": result["revision_count"], "steps": result["steps"],
            "missing_information": result["missing_information"],
            "review": review_ and {"score": review_["score"], "approved": review_["approved"],
                                   "critical_issues": review_["critical_issues"],
                                   "corrections": review_["corrections"]},
            "pdfs": [a["pdf_path"] for a in result["artifacts"]],
            "application_id": result["application_id"]}


def build_parser():
    parser = argparse.ArgumentParser(prog="job_hunter")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    parser.add_argument("--profile")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create a profile from candidate supplied JSON")
    init.add_argument("profile_json", help="path to your private profile JSON")
    avail = sub.add_parser("confirm-availability", help="record that the candidate confirmed availability")
    avail.add_argument("--yes", action="store_true", required=True,
                       help="only after the candidate explicitly confirmed")
    contact = sub.add_parser("set-contact", help="store candidate-supplied contact details")
    contact.add_argument("--email")
    contact.add_argument("--phone")
    run = sub.add_parser("run", help="assess a manual job JSON and advance its application")
    run.add_argument("job_json")
    run.add_argument("--resume", action="store_true", help="re-assess and resume after NEEDS_INFORMATION")
    appr = sub.add_parser("approve", help="show what will be approved; with --yes, record your approval")
    appr.add_argument("application_id")
    appr.add_argument("--yes", action="store_true", help="you reviewed the PDFs and approve this application")
    sub.add_parser("status", help="list applications and their states")
    web = sub.add_parser("web", help="open the local web interface")
    web.add_argument("--host", default="127.0.0.1", help="loopback host (default: 127.0.0.1)")
    web.add_argument("--port", type=int, default=8765, help="local port (default: 8765)")
    web.add_argument("--no-open", action="store_true", help="do not open the browser automatically")
    for name in ("create", "review"):
        worker = sub.add_parser(name, help=f"run only the {name} step for one application (worker entry)")
        worker.add_argument("application_id")
    sub.add_parser("doctor", help="check the toolchain")
    sub.add_parser("demo", help="end-to-end run on a synthetic job in a throwaway database")
    return parser


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    root = Path(args.root)
    if args.command == "doctor":
        _out({"python": sys.version.split()[0], "tectonic": str(find_tectonic(root)) if _has_tectonic(root) else None})
        return 0
    if args.command == "demo":
        return _demo(root)
    store = Store(args.db)
    try:
        if args.command == "init":
            _out(profile_mod.create_profile(store, json.loads(Path(args.profile_json).read_text(encoding="utf-8"))))
        elif args.command == "confirm-availability":
            _out(profile_mod.confirm_availability(store, _profile_id(store, args.profile))["availability"])
        elif args.command == "set-contact":
            _out(profile_mod.set_contact(store, _profile_id(store, args.profile),
                                         email=args.email, phone=args.phone)["preferences"]["contact"])
        elif args.command == "run":
            job = json.loads(Path(args.job_json).read_text(encoding="utf-8"))
            _out(_summary(run_job(store, _profile_id(store, args.profile), job, root=root, resume=args.resume)))
        elif args.command == "status":
            _out([{"application_id": a["id"], "job": (store.get("JobPosting", a["job_id"]) or {}).get("title"),
                   "state": a["state"], "revisions": a["revision_count"]}
                  for a in store.list_records("Application")])
        elif args.command == "web":
            from .web import serve
            serve(store, root=root, host=args.host, port=args.port, open_browser=not args.no_open)
        elif args.command == "approve":
            summary = approval_summary(store, args.application_id)
            if not args.yes:
                _out({**summary, "next": "abra os PDFs, e se estiver tudo certo repita com --yes"})
                return 0
            done = approve(store, args.application_id, root=root)
            _out({"state": done["state"], "approved_documents": summary["documents"]})
        elif args.command == "create":
            _out([{k: a[k] for k in ("id", "kind", "version", "pdf_path")}
                  for a in create_documents(store, args.application_id, root=root)])
        elif args.command == "review":
            _out(review(store, args.application_id, root=root))
    except (StorageError, LatexUnavailable, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _has_tectonic(root) -> bool:
    try:
        find_tectonic(root)
        return True
    except LatexUnavailable:
        return False


def _demo(root: Path) -> int:
    for suffix in ("", "-wal", "-shm"):
        Path(str(DEMO_DB) + suffix).unlink(missing_ok=True)
    store = Store(DEMO_DB)
    profile = profile_mod.create_profile(store, json.loads(
        (PROJECT_ROOT / "examples" / "demo_profile.json").read_text(encoding="utf-8")))
    # Synthetic confirmations, valid only inside this throwaway demo database.
    profile_mod.confirm_availability(store, profile["id"])
    profile_mod.set_contact(store, profile["id"], email="demo@example.invalid")
    job = json.loads(DEMO_JOB.read_text(encoding="utf-8"))
    try:
        _out(_summary(run_job(store, profile["id"], job, root=root)))
    except LatexUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
