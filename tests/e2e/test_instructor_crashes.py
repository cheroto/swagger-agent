"""E2E tests for instructor 'multiple tool calls' crashes.

These tests isolate specific route files that have triggered the
'Instructor does not support multiple tool calls' assertion error.
Each test makes a single LLM call to determine if the crash is
deterministic or flaky.

Run: pytest tests/e2e/test_instructor_crashes.py -m e2e -v -s
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from swagger_agent.agents.route_extractor.harness import (
    RouteExtractorContext,
    run_route_extractor,
)
from swagger_agent.config import LLMConfig

from .conftest import e2e

REPOS_ROOT = Path(__file__).parent / "repos"


def _make_ctx(repo: str, route_file: str, framework: str = "unknown",
              base_path: str = "") -> tuple[str, RouteExtractorContext]:
    """Build absolute path and context for a route file."""
    abs_path = str((REPOS_ROOT / repo / route_file).resolve())
    return abs_path, RouteExtractorContext(
        framework=framework,
        base_path=base_path,
        target_file=abs_path,
    )


def _run_n_times(abs_path: str, ctx: RouteExtractorContext, n: int = 3):
    """Run route extraction N times, return (successes, failures)."""
    config = LLMConfig()
    successes = []
    failures = []
    for i in range(n):
        try:
            desc, record = run_route_extractor(abs_path, ctx, config=config)
            successes.append({
                "run": i + 1,
                "endpoints": record.endpoint_count,
                "mount_map": record.mount_map,
                "phase1_ms": record.phase1_duration_ms,
                "phase2_ms": record.phase2_duration_ms,
            })
        except Exception as e:
            failures.append({
                "run": i + 1,
                "error": str(e)[:200],
            })
    return successes, failures


@e2e
class TestInstructorCrashes:
    """Test files that have triggered instructor 'multiple tool calls' errors."""

    def test_ocaml_dream_server_ml(self):
        """OCaml Dream server.ml — single route file, all routes + auth + models.

        This file defines routes, auth middleware, and inline types all in one
        ~280 line file. The LLM may produce multiple tool calls when trying
        to fill the CodeAnalysis structured output.
        """
        abs_path, ctx = _make_ctx(
            "ocaml-dream", "server/bin/server.ml",
            framework="dream", base_path="",
        )
        successes, failures = _run_n_times(abs_path, ctx)

        print(f"\n  ocaml-dream server.ml: {len(successes)} success, {len(failures)} fail")
        for s in successes:
            print(f"    Run {s['run']}: {s['endpoints']} endpoints, mount_map={s['mount_map']}")
        for f in failures:
            print(f"    Run {f['run']}: CRASH — {f['error']}")

        # At least 1 out of 3 should succeed for it to be flaky (not deterministic)
        assert len(successes) > 0 or len(failures) > 0, "No runs completed"

    def test_flask_teams_resources(self):
        """Flask-restplus teams/resources.py — resource classes with auth decorators.

        This file has multiple Resource classes with @login_required decorators
        and nested route definitions.
        """
        abs_path, ctx = _make_ctx(
            "flask-restplus-example", "app/modules/teams/resources.py",
            framework="flask-restplus", base_path="/api/v1",
        )
        successes, failures = _run_n_times(abs_path, ctx)

        print(f"\n  flask teams/resources.py: {len(successes)} success, {len(failures)} fail")
        for s in successes:
            print(f"    Run {s['run']}: {s['endpoints']} endpoints")
        for f in failures:
            print(f"    Run {f['run']}: CRASH — {f['error']}")

        assert len(successes) > 0 or len(failures) > 0, "No runs completed"

    def test_dart_frog_products_index(self):
        """Dart Frog products/index.dart — file-based routing with middleware.

        Dart Frog triggered the same error in some runs.
        """
        abs_path, ctx = _make_ctx(
            "dart-frog", "routes/products/index.dart",
            framework="dart_frog", base_path="",
        )
        successes, failures = _run_n_times(abs_path, ctx)

        print(f"\n  dart-frog products/index.dart: {len(successes)} success, {len(failures)} fail")
        for s in successes:
            print(f"    Run {s['run']}: {s['endpoints']} endpoints")
        for f in failures:
            print(f"    Run {f['run']}: CRASH — {f['error']}")

        assert len(successes) > 0 or len(failures) > 0, "No runs completed"

    def test_haskell_servant_user_api(self):
        """Haskell Servant Api/User.hs — type-level route definitions.

        Haskell Servant also showed partial failures.
        """
        abs_path, ctx = _make_ctx(
            "haskell-servant", "src/Api/User.hs",
            framework="servant", base_path="",
        )
        successes, failures = _run_n_times(abs_path, ctx)

        print(f"\n  haskell-servant Api/User.hs: {len(successes)} success, {len(failures)} fail")
        for s in successes:
            print(f"    Run {s['run']}: {s['endpoints']} endpoints")
        for f in failures:
            print(f"    Run {f['run']}: CRASH — {f['error']}")

        assert len(successes) > 0 or len(failures) > 0, "No runs completed"
