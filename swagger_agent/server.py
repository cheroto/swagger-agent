"""Webhook server — accepts a repo URL, runs the pipeline, returns the spec."""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import subprocess
import tempfile
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from swagger_agent.config import LLMConfig
from swagger_agent.pipeline import run_pipeline

logger = logging.getLogger("swagger_agent.server")

app = FastAPI(title="Swagger Agent", description="Generate OpenAPI specs from code repositories")


class GenerateRequest(BaseModel):
    repo_url: str = Field(description="Git repository URL (HTTPS or SSH)")
    branch: str = Field(default="", description="Branch name to checkout. Empty = default branch. Mutually exclusive with tag and commit.")
    tag: str = Field(default="", description="Tag to checkout (e.g. 'v1.2.3'). Mutually exclusive with branch and commit.")
    commit: str = Field(default="", description="Full commit SHA to checkout. Requires a full clone (slower). Mutually exclusive with branch and tag.")
    token: str = Field(default="", description="Git auth token for private repos. Injected into HTTPS clone URL as oauth2 credentials.")


class GenerateResponse(BaseModel):
    spec: dict = Field(description="The assembled OpenAPI 3.0 spec as JSON")
    yaml: str = Field(description="The assembled OpenAPI 3.0 spec as YAML")
    timings: dict = Field(default_factory=dict)


def _inject_token(url: str, token: str) -> str:
    """Inject an oauth2 token into an HTTPS git URL for private repo access."""
    if not token or not url.startswith("https://"):
        return url
    return url.replace("https://", f"https://oauth2:{token}@", 1)


def _clone_repo(req: GenerateRequest) -> str:
    """Clone a repo to a temp directory at the requested ref. Returns the path.

    - branch/tag: shallow clone with --branch (fast)
    - commit: full clone + checkout (slower, needed for arbitrary SHAs)
    - none: shallow clone of default branch
    """
    effective_token = req.token or os.environ.get("GIT_TOKEN", "")
    clone_url = _inject_token(req.repo_url, effective_token)
    tmp_dir = os.path.join(tempfile.gettempdir(), f"swagger-agent-{uuid.uuid4().hex[:12]}")

    ref = req.branch or req.tag
    needs_full_clone = bool(req.commit)

    if needs_full_clone:
        cmd = ["git", "clone", clone_url, tmp_dir]
    else:
        cmd = ["git", "clone", "--depth", "1"]
        if ref:
            cmd.extend(["--branch", ref])
        cmd.extend([clone_url, tmp_dir])

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
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Git clone/checkout failed: {stderr.strip()}")
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=408, detail="Git clone timed out")

    return tmp_dir


def _validate_and_run(req: GenerateRequest):
    """Validate the request, clone, run pipeline, clean up. Returns PipelineResult."""
    refs = [r for r in (req.branch, req.tag, req.commit) if r]
    if len(refs) > 1:
        raise HTTPException(status_code=422, detail="Only one of branch, tag, or commit may be specified.")

    tmp_dir = None
    try:
        tmp_dir = _clone_repo(req)
        return run_pipeline(target_dir=tmp_dir, config=LLMConfig(), skip_scout=False)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Pipeline failed for %s", req.repo_url)
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {e}")
    finally:
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    """Clone a repo and generate an OpenAPI spec from it."""
    result = _validate_and_run(req)
    return GenerateResponse(spec=result.spec, yaml=result.yaml_str, timings=result.timings)


@app.post("/generate/yaml")
def generate_yaml(req: GenerateRequest):
    """Clone a repo and return the OpenAPI spec as gzipped YAML."""
    result = _validate_and_run(req)
    compressed = gzip.compress(result.yaml_str.encode("utf-8"))
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
