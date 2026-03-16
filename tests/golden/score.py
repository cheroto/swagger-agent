#!/usr/bin/env python3
"""Compute F1 scores for pipeline output against golden test data.

Usage:
    python tests/golden/score.py /tmp/swagger-test/
    python tests/golden/score.py /tmp/swagger-test/rest-api-node.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


GOLDEN_DIR = Path(__file__).parent


def normalize_path(path: str) -> str:
    """Normalize path for comparison: collapse param names to {_}."""
    return re.sub(r"\{[^}]+\}", "{_}", path.rstrip("/").lower() or "/")


def extract_endpoints_from_spec(spec: dict) -> set[tuple[str, str]]:
    """Extract (METHOD, normalized_path) tuples from assembled spec."""
    endpoints = set()
    for path, methods in spec.get("paths", {}).items():
        for method in methods:
            if method.lower() in ("get", "post", "put", "patch", "delete", "head", "options"):
                endpoints.add((method.upper(), normalize_path(path)))
    return endpoints


def extract_schemas_from_spec(spec: dict) -> set[str]:
    """Extract schema names from assembled spec (excluding unresolved placeholders)."""
    schemas = set()
    for name, schema in spec.get("components", {}).get("schemas", {}).items():
        if not schema.get("x-unresolved"):
            schemas.add(name.lower())
    return schemas


def load_golden(repo_name: str) -> dict | None:
    """Load golden test data for a repo."""
    golden_path = GOLDEN_DIR / f"{repo_name}.json"
    if not golden_path.exists():
        return None
    return json.loads(golden_path.read_text())


def f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def score_endpoints(spec: dict, golden: dict) -> dict:
    """Score endpoint extraction against golden data."""
    actual = extract_endpoints_from_spec(spec)
    expected = {(e["method"].upper(), normalize_path(e["path"])) for e in golden["endpoints"]}

    tp = len(actual & expected)
    fp = len(actual - expected)
    fn = len(expected - actual)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0

    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1(precision, recall), 3),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "missing": sorted(f"{m} {p}" for m, p in (expected - actual)),
        "extra": sorted(f"{m} {p}" for m, p in (actual - expected)),
    }


def score_schemas(spec: dict, golden: dict) -> dict:
    """Score schema extraction against golden data."""
    actual = extract_schemas_from_spec(spec)
    expected = {s.lower() for s in golden["schemas"]}

    tp = len(actual & expected)
    fp = len(actual - expected)
    fn = len(expected - actual)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0

    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1(precision, recall), 3),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "missing": sorted(expected - actual),
        "extra": sorted(actual - expected),
    }


def score_one(result_path: Path) -> dict | None:
    """Score a single pipeline result JSON against its golden file."""
    repo_name = result_path.stem
    golden = load_golden(repo_name)
    if golden is None:
        return None

    data = json.loads(result_path.read_text())
    spec = data.get("spec", {})

    return {
        "repo": repo_name,
        "endpoints": score_endpoints(spec, golden),
        "schemas": score_schemas(spec, golden),
    }


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/swagger-test/")

    if target.is_file():
        result_files = [target]
    elif target.is_dir():
        result_files = sorted(target.glob("*.json"))
    else:
        print(f"Error: {target} not found", file=sys.stderr)
        sys.exit(1)

    all_scores: list[dict] = []
    for path in result_files:
        score = score_one(path)
        if score is not None:
            all_scores.append(score)

    if not all_scores:
        print("No matching golden files found.", file=sys.stderr)
        sys.exit(1)

    # Print table
    print(f"{'Repo':<30s} {'EP-P':>5s} {'EP-R':>5s} {'EP-F1':>6s} {'SC-P':>5s} {'SC-R':>5s} {'SC-F1':>6s}")
    print("-" * 68)

    ep_f1_sum = 0.0
    sc_f1_sum = 0.0

    for s in all_scores:
        ep = s["endpoints"]
        sc = s["schemas"]
        ep_f1_sum += ep["f1"]
        sc_f1_sum += sc["f1"]
        print(
            f"{s['repo']:<30s} "
            f"{ep['precision']:>5.2f} {ep['recall']:>5.2f} {ep['f1']:>6.3f} "
            f"{sc['precision']:>5.2f} {sc['recall']:>5.2f} {sc['f1']:>6.3f}"
        )

    n = len(all_scores)
    print("-" * 68)
    print(
        f"{'AVERAGE':<30s} "
        f"{'':>5s} {'':>5s} {ep_f1_sum / n:>6.3f} "
        f"{'':>5s} {'':>5s} {sc_f1_sum / n:>6.3f}"
    )

    # Print details for repos with issues
    print("\n--- Details ---")
    for s in all_scores:
        ep = s["endpoints"]
        sc = s["schemas"]
        issues = []
        if ep["missing"]:
            issues.append(f"  Missing endpoints: {', '.join(ep['missing'])}")
        if ep["extra"]:
            issues.append(f"  Extra endpoints: {', '.join(ep['extra'])}")
        if sc["missing"]:
            issues.append(f"  Missing schemas: {', '.join(sc['missing'])}")
        if sc["extra"]:
            issues.append(f"  Extra schemas: {', '.join(sc['extra'])}")
        if issues:
            print(f"\n{s['repo']}:")
            for issue in issues:
                print(issue)


if __name__ == "__main__":
    main()
