"""Webhook server — accepts a repo URL, runs the pipeline, returns the spec.

All generation is async: POST /generate returns a job ID immediately,
GET /jobs/{id} polls for the result with live progress.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from enum import Enum

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from swagger_agent.config import LLMConfig
from swagger_agent.pipeline import run_pipeline

# Configure structured logging for all swagger_agent modules
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("swagger_agent.server")

app = FastAPI(title="Swagger Agent", description="Generate OpenAPI specs from code repositories")


# ── Models ────────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    repo_url: str = Field(description="Git repository URL (HTTPS or SSH)")
    branch: str = Field(default="", description="Branch name to checkout. Empty = default branch. Mutually exclusive with tag and commit.")
    tag: str = Field(default="", description="Tag to checkout (e.g. 'v1.2.3'). Mutually exclusive with branch and commit.")
    commit: str = Field(default="", description="Full commit SHA to checkout. Requires a full clone (slower). Mutually exclusive with branch and tag.")
    token: str = Field(default="", description="Git auth token for private repos. Injected into HTTPS clone URL as oauth2 credentials.")


class JobStatus(str, Enum):
    PENDING = "pending"
    CLONING = "cloning"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class JobProgress(BaseModel):
    phase: str = ""
    phase_number: int = 0
    routes_total: int = 0
    routes_done: int = 0
    routes_failed: int = 0
    endpoints_found: int = 0
    schemas_resolved: int = 0
    schemas_unresolved: int = 0
    log: list[str] = Field(default_factory=list)


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    error: str = ""
    progress: JobProgress = Field(default_factory=JobProgress)
    spec: dict | None = None
    yaml: str | None = None
    timings: dict | None = None


# ── Progress collector (implements dashboard interface) ────────────────────

class _ProgressCollector:
    """Lightweight dashboard substitute that collects pipeline events on a job.

    Implements the subset of dashboard/event_handler methods the pipeline
    and Scout harness call. Unknown method calls are silently ignored.
    """

    def __init__(self, job: _Job):
        self._job = job
        self._lock = threading.Lock()

    def __getattr__(self, name):
        """Silently ignore any dashboard/event_handler method we don't implement."""
        return lambda *args, **kwargs: None

    def _log(self, msg: str) -> None:
        with self._lock:
            self._job.progress_log.append(msg)
            # Cap log size
            if len(self._job.progress_log) > 100:
                self._job.progress_log = self._job.progress_log[-80:]

    def phase_start(self, phase: int, name: str) -> None:
        with self._lock:
            self._job.phase = name
            self._job.phase_number = phase
        self._log(f"Phase {phase}: {name}")

    def phase_complete(self, phase: int, summary: str) -> None:
        self._log(f"Phase {phase} done: {summary}")

    def route_start(self, file: str, index: int, total: int) -> None:
        short = file.rsplit("/", 1)[-1]
        with self._lock:
            self._job.routes_total = total
        self._log(f"Extracting {short} ({index}/{total})")

    def route_complete(self, file: str, endpoints: int, duration_ms: float) -> None:
        short = file.rsplit("/", 1)[-1]
        with self._lock:
            self._job.routes_done += 1
            self._job.endpoints_found += endpoints
        self._log(f"{short}: {endpoints} endpoint(s) ({duration_ms:.0f}ms)")

    def route_failed(self, file: str, error: str) -> None:
        short = file.rsplit("/", 1)[-1]
        with self._lock:
            self._job.routes_failed += 1
        self._log(f"{short}: FAILED — {error}")

    def route_endpoints_discovered(self, descriptor) -> None:
        pass  # Already counted in route_complete

    def schema_event(self, event: str, **kwargs) -> None:
        if event == "extracted":
            count = kwargs.get("count", 0)
            file = str(kwargs.get("file", "?")).rsplit("/", 1)[-1]
            with self._lock:
                self._job.schemas_resolved += count
            self._log(f"Schema: {file} → {count} schema(s)")
        elif event == "resolving" and not kwargs.get("file"):
            name = kwargs.get("name", "?")
            with self._lock:
                self._job.schemas_unresolved += 1
            self._log(f"Schema: {name} — unresolved")
        elif event == "extract_failed":
            name = kwargs.get("name", "?")
            with self._lock:
                self._job.schemas_unresolved += 1
            self._log(f"Schema: {name} — extraction failed")

    def assembly_complete(self, paths: int, schemas: int) -> None:
        self._log(f"Assembled: {paths} path(s), {schemas} schema(s)")

    def validation_complete(self, errors: int, warnings: int) -> None:
        if errors:
            self._log(f"Validation: {errors} error(s), {warnings} warning(s)")
        else:
            self._log(f"Validation: clean ({warnings} warning(s))")


# ── Job store (in-memory) ─────────────────────────────────────────────────

class _Job:
    __slots__ = (
        "id", "status", "error", "spec", "yaml", "timings", "created_at",
        "phase", "phase_number", "routes_total", "routes_done", "routes_failed",
        "endpoints_found", "schemas_resolved", "schemas_unresolved", "progress_log",
    )

    def __init__(self, job_id: str):
        self.id = job_id
        self.status = JobStatus.PENDING
        self.error = ""
        self.spec: dict | None = None
        self.yaml: str | None = None
        self.timings: dict | None = None
        self.created_at = time.monotonic()
        # Progress fields
        self.phase = ""
        self.phase_number = 0
        self.routes_total = 0
        self.routes_done = 0
        self.routes_failed = 0
        self.endpoints_found = 0
        self.schemas_resolved = 0
        self.schemas_unresolved = 0
        self.progress_log: list[str] = []

    def to_progress(self) -> JobProgress:
        return JobProgress(
            phase=self.phase,
            phase_number=self.phase_number,
            routes_total=self.routes_total,
            routes_done=self.routes_done,
            routes_failed=self.routes_failed,
            endpoints_found=self.endpoints_found,
            schemas_resolved=self.schemas_resolved,
            schemas_unresolved=self.schemas_unresolved,
            log=list(self.progress_log),
        )


_jobs: dict[str, _Job] = {}
_jobs_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────

def _ref_label(req: GenerateRequest) -> str:
    """Human-readable label for the requested git ref."""
    if req.commit:
        return f"commit={req.commit[:12]}"
    if req.tag:
        return f"tag={req.tag}"
    if req.branch:
        return f"branch={req.branch}"
    return "default branch"


def _inject_token(url: str, token: str) -> str:
    """Inject an oauth2 token into an HTTPS git URL for private repo access."""
    if not token or not url.startswith("https://"):
        return url
    return url.replace("https://", f"https://oauth2:{token}@", 1)


def _clone_repo(req: GenerateRequest, job: _Job) -> str:
    """Clone a repo to a temp directory at the requested ref. Returns the path."""
    job.status = JobStatus.CLONING
    effective_token = req.token or os.environ.get("GIT_TOKEN", "")
    clone_url = _inject_token(req.repo_url, effective_token)
    tmp_dir = os.path.join(tempfile.gettempdir(), f"swagger-agent-{job.id}")

    ref = req.branch or req.tag
    needs_full_clone = bool(req.commit)

    if needs_full_clone:
        cmd = ["git", "clone", clone_url, tmp_dir]
    else:
        cmd = ["git", "clone", "--depth", "1"]
        if ref:
            cmd.extend(["--branch", ref])
        cmd.extend([clone_url, tmp_dir])

    t0 = time.monotonic()
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
        if req.commit:
            subprocess.run(
                ["git", "checkout", req.commit],
                cwd=tmp_dir, check=True, capture_output=True, text=True, timeout=30,
            )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr
        if effective_token:
            stderr = stderr.replace(effective_token, "***")
        logger.error("[%s] Clone failed: %s", job.id, stderr.strip())
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"Git clone/checkout failed: {stderr.strip()}")
    except subprocess.TimeoutExpired:
        logger.error("[%s] Clone timed out after 120s", job.id)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("Git clone timed out")

    clone_ms = (time.monotonic() - t0) * 1000
    logger.info("[%s] Cloned in %.0fms → %s", job.id, clone_ms, tmp_dir)
    job.progress_log.append(f"Cloned in {clone_ms:.0f}ms")
    return tmp_dir


def _run_job(req: GenerateRequest, job: _Job) -> None:
    """Run the full pipeline in a background thread."""
    tmp_dir = None
    try:
        tmp_dir = _clone_repo(req, job)
        job.status = JobStatus.RUNNING
        collector = _ProgressCollector(job)
        t0 = time.monotonic()
        result = run_pipeline(
            target_dir=tmp_dir, config=LLMConfig(),
            skip_scout=False, dashboard=collector,
        )
        pipeline_ms = (time.monotonic() - t0) * 1000

        job.spec = result.spec
        job.yaml = result.yaml_str
        job.timings = result.timings
        job.status = JobStatus.DONE

        ep_count = sum(len(d.endpoints) for d in result.descriptors)
        logger.info(
            "[%s] Done: %d endpoints, %d schemas, %.1fs",
            job.id, ep_count, len(result.schemas), pipeline_ms / 1000,
        )
    except Exception as e:
        job.status = JobStatus.FAILED
        job.error = str(e)
        logger.exception("[%s] Failed: %s", job.id, e)
    finally:
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Endpoints ─────────────────────────────────────────────────────────────

@app.post("/generate", status_code=202)
def generate(req: GenerateRequest) -> JobResponse:
    """Submit a spec generation job. Returns immediately with a job ID."""
    refs = [r for r in (req.branch, req.tag, req.commit) if r]
    if len(refs) > 1:
        raise HTTPException(status_code=422, detail="Only one of branch, tag, or commit may be specified.")

    job = _Job(uuid.uuid4().hex[:8])
    with _jobs_lock:
        _jobs[job.id] = job

    logger.info("[%s] Job submitted: %s (%s)", job.id, req.repo_url, _ref_label(req))
    threading.Thread(target=_run_job, args=(req, job), daemon=True).start()

    return JobResponse(job_id=job.id, status=job.status)


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> JobResponse:
    """Poll for job status, progress, and results."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobResponse(
        job_id=job.id,
        status=job.status,
        error=job.error,
        progress=job.to_progress(),
        spec=job.spec if job.status == JobStatus.DONE else None,
        yaml=job.yaml if job.status == JobStatus.DONE else None,
        timings=job.timings if job.status == JobStatus.DONE else None,
    )


@app.get("/jobs/{job_id}/yaml")
def get_job_yaml(job_id: str):
    """Download the generated spec as gzipped YAML. Only available when job is done."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.DONE:
        raise HTTPException(status_code=409, detail=f"Job is {job.status.value}, not done yet")

    compressed = gzip.compress(job.yaml.encode("utf-8"))
    return Response(
        content=compressed,
        media_type="application/x-yaml",
        headers={
            "Content-Encoding": "gzip",
            "Content-Disposition": "attachment; filename=openapi.yaml",
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}
