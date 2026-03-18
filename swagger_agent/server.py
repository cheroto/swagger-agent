"""Webhook server — accepts a repo URL, runs the pipeline, returns the spec.

All generation is async: POST /generate returns a job ID immediately,
GET /jobs/{id} polls for the result.
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


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    error: str = ""
    spec: dict | None = None
    yaml: str | None = None
    timings: dict | None = None


# ── Job store (in-memory) ─────────────────────────────────────────────────

class _Job:
    __slots__ = ("id", "status", "error", "spec", "yaml", "timings", "created_at")

    def __init__(self, job_id: str):
        self.id = job_id
        self.status = JobStatus.PENDING
        self.error = ""
        self.spec: dict | None = None
        self.yaml: str | None = None
        self.timings: dict | None = None
        self.created_at = time.monotonic()


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
    return tmp_dir


def _run_job(req: GenerateRequest, job: _Job) -> None:
    """Run the full pipeline in a background thread."""
    tmp_dir = None
    try:
        tmp_dir = _clone_repo(req, job)
        job.status = JobStatus.RUNNING
        t0 = time.monotonic()
        result = run_pipeline(target_dir=tmp_dir, config=LLMConfig(), skip_scout=False)
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
    """Poll for job status and results."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobResponse(
        job_id=job.id,
        status=job.status,
        error=job.error,
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
