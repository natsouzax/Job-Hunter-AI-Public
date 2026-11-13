"""Local web interface for the Job Hunter pipeline.

The server deliberately binds only to loopback and delegates every business
operation to the same application services used by the CLI.
"""
from __future__ import annotations

import json
import mimetypes
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import profile as profile_mod
from .coordinator import approve, run_job
from .latex import LatexUnavailable, PROJECT_ROOT, find_tectonic
from .storage import StorageError, TransitionError

ASSET_DIR = Path(__file__).with_name("web_assets")
MAX_BODY = 2 * 1024 * 1024
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def _latest(rows: list[dict], key: str, value: str) -> dict | None:
    matches = [row for row in rows if row.get(key) == value]
    return matches[-1] if matches else None


def dashboard_data(store, root: Path = PROJECT_ROOT) -> dict:
    """Build the read model consumed by the browser."""
    profiles = store.list_records("CandidateProfile")
    profile = profiles[0] if len(profiles) == 1 else None
    jobs = {row["id"]: row for row in store.list_records("JobPosting")}
    assessments = store.list_records("JobAssessment")
    reviews = store.list_records("ReviewReport")
    artifacts = {row["id"]: row for row in store.list_records("DocumentArtifact")}
    applications = []
    for app in reversed(store.list_records("Application")):
        job = jobs.get(app["job_id"])
        review = _latest(reviews, "application_id", app["id"])
        assessment = _latest(assessments, "job_id", app["job_id"])
        docs = []
        for artifact_id in review["artifact_ids"] if review else []:
            artifact = artifacts.get(artifact_id)
            if artifact:
                docs.append({"id": artifact["id"], "kind": artifact["kind"],
                             "version": artifact["version"],
                             "url": f"/documents/{artifact['id']}"})
        applications.append({
            "id": app["id"], "state": app["state"], "revision_count": app["revision_count"],
            "updated_at": app["updated_at"], "job": job,
            "assessment": assessment and {
                "score": assessment["score"], "recommendation": assessment["recommendation"],
                "missing_information": assessment["missing_information"],
                "met_requirements": assessment["met_requirements"],
                "unmet_requirements": assessment["unmet_requirements"],
            },
            "review": review and {
                "score": review["score"], "approved": review["approved"],
                "critical_issues": review["critical_issues"], "corrections": review["corrections"],
            },
            "documents": docs,
            "events": [{k: event.get(k) for k in (
                "event_type", "previous_state", "next_state", "agent", "created_at", "evidence")}
                       for event in store.events(app["id"])],
            "actions": {
                "resume": app["state"] == "NEEDS_INFORMATION",
                "approve": app["state"] == "READY_FOR_APPROVAL",
                "withdraw": app["state"] in {
                    "DISCOVERED", "SCREENED", "SHORTLISTED", "CV_GENERATING", "CV_REVIEW",
                    "CV_APPROVED", "READY_FOR_APPROVAL", "REVISION_REQUIRED", "USER_APPROVED"},
            },
        })
    counts = {state: sum(app["state"] == state for app in applications)
              for state in {app["state"] for app in applications}}
    return {
        "profile": profile and {
            "id": profile["id"], "name": profile["name"], "location": profile["location"],
            "preferences": profile["preferences"], "availability": profile["availability"],
            "unconfirmed": profile["unconfirmed"],
            "facts": [store.get("VerifiedFact", fact_id) for fact_id in profile["fact_ids"]],
        },
        "applications": applications,
        "stats": {"applications": len(applications), "jobs": len(jobs),
                  "ready": counts.get("READY_FOR_APPROVAL", 0),
                  "approved": counts.get("USER_APPROVED", 0),
                  "needs_information": counts.get("NEEDS_INFORMATION", 0)},
        "system": _system_status(root),
    }


def _system_status(root: Path) -> dict:
    try:
        tectonic = str(find_tectonic(root))
    except LatexUnavailable:
        tectonic = None
    return {"python": sys.version.split()[0], "tectonic": tectonic,
            "database": "SQLite local", "submission_enabled": False,
            "scraping_enabled": False}


class JobHunterHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, *, store, root: Path):
        super().__init__(address, handler)
        self.store = store
        self.root = Path(root).resolve()


class Handler(BaseHTTPRequestHandler):
    server_version = "JobHunterLocal/0.1"

    def log_message(self, fmt, *args):
        print(f"[web] {self.address_string()} - {fmt % args}")

    def _headers(self, content_type: str, length: int, status=HTTPStatus.OK):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; object-src 'self'; frame-ancestors 'none'")
        self.end_headers()

    def _bytes(self, data: bytes, content_type: str, status=HTTPStatus.OK):
        self._headers(content_type, len(data), status)
        try:
            self.wfile.write(data)
        except ConnectionError:
            pass  # Browser closed a speculative or headless request early.

    def _json(self, payload, status=HTTPStatus.OK):
        self._bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8", status)

    def _error(self, status, message):
        self._json({"error": message}, status)

    def _body(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > MAX_BODY:
            raise ValueError("request body is empty or too large")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlsplit(origin)
            return (parsed.scheme == "http" and parsed.hostname in LOOPBACK
                    and parsed.port == self.server.server_port)
        except ValueError:
            return False

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        try:
            if path == "/api/dashboard":
                self._json(dashboard_data(self.server.store, self.server.root))
                return
            if path.startswith("/documents/"):
                self._document(path.removeprefix("/documents/"))
                return
            asset = "index.html" if path in {"/", "/index.html"} else path.removeprefix("/")
            if asset in {"index.html", "app.js", "styles.css"}:
                file_path = ASSET_DIR / asset
                content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
                if content_type.startswith("text/") or content_type == "application/javascript":
                    content_type += "; charset=utf-8"
                self._bytes(file_path.read_bytes(), content_type)
                return
            self._error(HTTPStatus.NOT_FOUND, "not found")
        except (OSError, StorageError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    def do_POST(self):
        if not self._same_origin():
            self._error(HTTPStatus.FORBIDDEN, "origin not allowed")
            return
        path = unquote(urlsplit(self.path).path)
        try:
            body = self._body()
            if path == "/api/profile/init":
                result = profile_mod.create_profile(self.server.store, body)
            elif path == "/api/profile/contact":
                result = profile_mod.set_contact(self.server.store, self._profile_id(),
                                                 email=body.get("email"), phone=body.get("phone"))
            elif path == "/api/profile/availability":
                if body.get("confirmed") is not True:
                    raise ValueError("explicit confirmation is required")
                result = profile_mod.confirm_availability(self.server.store, self._profile_id(),
                                                          windows=body.get("windows"))
            elif path == "/api/jobs/run":
                job = body.get("job")
                if not isinstance(job, dict):
                    raise ValueError("job must be an object")
                result = run_job(self.server.store, self._profile_id(), job,
                                 root=self.server.root, resume=body.get("resume") is True)
            elif path.startswith("/api/applications/"):
                result = self._application_action(path, body)
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
                return
            self._json({"result": result, "dashboard": dashboard_data(
                self.server.store, self.server.root)})
        except (json.JSONDecodeError, UnicodeDecodeError, StorageError, LatexUnavailable,
                TransitionError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    def _profile_id(self) -> str:
        profiles = self.server.store.list_records("CandidateProfile")
        if len(profiles) != 1:
            raise ValueError("initialize exactly one profile first")
        return profiles[0]["id"]

    def _application_action(self, path: str, body: dict):
        parts = path.strip("/").split("/")
        if len(parts) != 4:
            raise ValueError("invalid application action")
        _, _, application_id, action = parts
        app = self.server.store.get("Application", application_id)
        if app is None:
            raise ValueError("application missing")
        if action == "approve":
            if body.get("confirmed") is not True:
                raise ValueError("explicit approval is required")
            return approve(self.server.store, application_id, root=self.server.root)
        if action == "resume":
            job = self.server.store.get("JobPosting", app["job_id"])
            return run_job(self.server.store, app["profile_id"], job,
                           root=self.server.root, resume=True)
        if action == "withdraw":
            actor = "user" if app["state"] == "USER_APPROVED" else "coordinator"
            return self.server.store.transition(
                application_id, "WITHDRAWN", expected_version=app["version"],
                idempotency_key=f"web:{application_id}:v{app['version']}:WITHDRAWN",
                evidence=["withdrawn from local web interface"], next_action="archive", actor=actor)
        raise ValueError("unknown application action")

    def _document(self, artifact_id: str):
        artifact = self.server.store.get("DocumentArtifact", artifact_id)
        if artifact is None:
            self._error(HTTPStatus.NOT_FOUND, "document not found")
            return
        file_path = (self.server.root / artifact["pdf_path"]).resolve()
        artifact_root = (self.server.root / "artifacts").resolve()
        if not file_path.is_relative_to(artifact_root) or not file_path.is_file():
            raise ValueError("document path is invalid or missing")
        data = file_path.read_bytes()
        self._headers("application/pdf", len(data))
        try:
            self.wfile.write(data)
        except ConnectionError:
            pass


def create_server(store, *, root: Path = PROJECT_ROOT, host="127.0.0.1", port=8765):
    if host not in LOOPBACK:
        raise ValueError("the local interface may only bind to loopback")
    return JobHunterHTTPServer((host, port), Handler, store=store, root=root)


def serve(store, *, root: Path = PROJECT_ROOT, host="127.0.0.1", port=8765,
          open_browser=True):
    server = create_server(store, root=root, host=host, port=port)
    url = f"http://{host}:{server.server_port}"
    print(f"Job Hunter: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.25, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Job Hunter.")
    finally:
        server.server_close()

