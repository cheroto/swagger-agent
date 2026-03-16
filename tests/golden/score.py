#!/usr/bin/env python3
"""Compute F1 scores for pipeline output against golden test data.

Schema scoring uses composite matching (token-based name similarity + property
Dice coefficient) with optimal (Hungarian) assignment, instead of exact name
equality. Match quality is reflected in weighted TP counts so F1 captures
extraction accuracy, not just detection.

Also scores security schemes (set match) and per-endpoint auth correctness.

Usage:
    python tests/golden/score.py /tmp/swagger-test/
    python tests/golden/score.py /tmp/swagger-test/rest-api-node.json
    python tests/golden/score.py /tmp/swagger-test/ --verbose
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment


GOLDEN_DIR = Path(__file__).parent

# Composite matching weights
NAME_WEIGHT = 0.3
PROP_WEIGHT = 0.7

# Minimum composite score to count as a true positive
MATCH_THRESHOLD = 0.4

# Regex for splitting camelCase/PascalCase/snake_case into word tokens
_TOKEN_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]|\d+")


def normalize_path(path: str) -> str:
    """Normalize path for comparison: collapse param names to {_}."""
    return re.sub(r"\{[^}]+\}", "{_}", path.rstrip("/").lower() or "/")


# ---------------------------------------------------------------------------
# Endpoint helpers
# ---------------------------------------------------------------------------

def extract_endpoints_from_spec(spec: dict) -> set[tuple[str, str]]:
    """Extract (METHOD, normalized_path) tuples from assembled spec."""
    endpoints = set()
    for path, methods in spec.get("paths", {}).items():
        for method in methods:
            if method.lower() in ("get", "post", "put", "patch", "delete", "head", "options"):
                endpoints.add((method.upper(), normalize_path(path)))
    return endpoints


def extract_endpoint_auth_from_spec(spec: dict) -> dict[tuple[str, str], bool]:
    """Extract {(METHOD, normalized_path): has_auth} from spec.

    An endpoint has auth if it has a non-empty `security` list, or if
    there's a top-level `security` and the endpoint doesn't override with [].
    """
    top_level_security = bool(spec.get("security"))
    result = {}
    for path, methods in spec.get("paths", {}).items():
        for method, details in methods.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete", "head", "options"):
                continue
            if not isinstance(details, dict):
                continue
            key = (method.upper(), normalize_path(path))
            if "security" in details:
                result[key] = bool(details["security"])
            else:
                result[key] = top_level_security
    return result


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
        "tp": tp, "fp": fp, "fn": fn,
        "missing": sorted(f"{m} {p}" for m, p in (expected - actual)),
        "extra": sorted(f"{m} {p}" for m, p in (actual - expected)),
    }


# ---------------------------------------------------------------------------
# Auth correctness scoring
# ---------------------------------------------------------------------------

def score_auth(spec: dict, golden: dict) -> dict:
    """Score auth correctness on matched endpoints.

    For endpoints present in both actual and golden, check whether the
    auth declaration (has_auth true/false) matches. Returns accuracy and
    a list of mismatches.
    """
    actual_eps = extract_endpoints_from_spec(spec)
    actual_auth = extract_endpoint_auth_from_spec(spec)

    golden_auth = {}
    golden_eps = set()
    for e in golden["endpoints"]:
        key = (e["method"].upper(), normalize_path(e["path"]))
        golden_eps.add(key)
        golden_auth[key] = e.get("has_auth", False)

    matched = actual_eps & golden_eps
    if not matched:
        return {"accuracy": 0.0, "total": 0, "correct": 0, "mismatches": []}

    correct = 0
    mismatches = []
    for key in sorted(matched):
        actual_has = actual_auth.get(key, False)
        expected_has = golden_auth[key]
        if actual_has == expected_has:
            correct += 1
        else:
            label = "missing auth" if expected_has else "spurious auth"
            mismatches.append(f"{key[0]} {key[1]} ({label})")

    return {
        "accuracy": round(correct / len(matched), 3),
        "total": len(matched),
        "correct": correct,
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# Security scheme scoring
# ---------------------------------------------------------------------------

def extract_security_schemes_from_spec(spec: dict) -> set[tuple[str, str]]:
    """Extract {(name, type)} from spec's securitySchemes."""
    result = set()
    schemes = spec.get("components", {}).get("securitySchemes", {})
    for name, details in schemes.items():
        scheme_type = details.get("type", "unknown").lower()
        result.add((name.lower(), scheme_type))
    return result


def parse_golden_security_schemes(golden: dict) -> set[tuple[str, str]]:
    """Parse golden security schemes into {(name, type)} tuples."""
    result = set()
    for entry in golden.get("security_schemes", []):
        result.add((entry["name"].lower(), entry["type"].lower()))
    return result


def score_security_schemes(spec: dict, golden: dict) -> dict:
    """Score security scheme extraction — simple set match on (name, type)."""
    actual = extract_security_schemes_from_spec(spec)
    expected = parse_golden_security_schemes(golden)

    # If golden expects 0 security schemes and we produce 0, that's perfect
    if not actual and not expected:
        return {
            "precision": 1.0, "recall": 1.0, "f1": 1.0,
            "tp": 0, "fp": 0, "fn": 0,
            "missing": [], "extra": [],
        }

    tp = len(actual & expected)
    fp = len(actual - expected)
    fn = len(expected - actual)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0

    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1(precision, recall), 3),
        "tp": tp, "fp": fp, "fn": fn,
        "missing": sorted(f"{n} ({t})" for n, t in (expected - actual)),
        "extra": sorted(f"{n} ({t})" for n, t in (actual - expected)),
    }


# ---------------------------------------------------------------------------
# Schema helpers — composite matching with Hungarian assignment
# ---------------------------------------------------------------------------

def extract_property_names(schema: dict, all_schemas: dict[str, dict] | None = None,
                           _visited: set[str] | None = None) -> set[str]:
    """Extract property names from an OpenAPI schema object.

    Handles allOf composition by collecting properties from all sub-schemas,
    including resolving $ref targets transitively.
    """
    if _visited is None:
        _visited = set()
    all_schemas = all_schemas or {}

    props = set(schema.get("properties", {}).keys())
    for sub in schema.get("allOf", []):
        if isinstance(sub, dict):
            ref = sub.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                ref_name = ref[len("#/components/schemas/"):]
                if ref_name not in _visited and ref_name in all_schemas:
                    _visited.add(ref_name)
                    props |= extract_property_names(all_schemas[ref_name], all_schemas, _visited)
            else:
                props |= set(sub.get("properties", {}).keys())
    return props


def extract_schemas_from_spec(spec: dict) -> dict[str, set[str]]:
    """Extract {name: property_names} from spec, excluding unresolved placeholders."""
    all_schemas = spec.get("components", {}).get("schemas", {})
    result = {}
    for name, schema in all_schemas.items():
        if not schema.get("x-unresolved"):
            result[name.lower()] = {p.lower() for p in extract_property_names(schema, all_schemas)}
    return result


def parse_golden_schemas(golden: dict) -> dict[str, set[str]]:
    """Parse golden schemas — supports both old format (list of strings) and
    new format (list of objects with name + properties)."""
    result = {}
    for entry in golden.get("schemas", []):
        if isinstance(entry, str):
            # Legacy format: just a name, no properties
            result[entry.lower()] = set()
        else:
            name = entry["name"].lower()
            result[name] = {p.lower() for p in entry.get("properties", [])}
    return result


def tokenize_name(name: str) -> set[str]:
    """Split a schema name into lowercase word tokens.

    Handles camelCase, PascalCase, snake_case, and mixed.
    E.g. "CreateUserRequest" -> {"create", "user", "request"}
         "user_response_dto" -> {"user", "response", "dto"}
    """
    return {t.lower() for t in _TOKEN_RE.findall(name)}


def token_name_similarity(a: str, b: str) -> float:
    """Jaccard similarity over word tokens of two schema names."""
    tokens_a = tokenize_name(a)
    tokens_b = tokenize_name(b)
    if not tokens_a and not tokens_b:
        return 0.0
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return intersection / union if union > 0 else 0.0


def dice_coefficient(a: set[str], b: set[str]) -> float:
    """Dice coefficient: 2|A∩B| / (|A|+|B|). Returns 0 if both empty."""
    if not a and not b:
        return 0.0
    total = len(a) + len(b)
    if total == 0:
        return 0.0
    return 2 * len(a & b) / total


def composite_score(
    name_a: str, props_a: set[str],
    name_b: str, props_b: set[str],
) -> float:
    """Composite matching score: weighted token name similarity + property Dice.

    When the golden side has no properties (legacy format), falls back to
    pure name similarity — we can't penalize property mismatch when the
    golden data doesn't specify properties.
    """
    ns = token_name_similarity(name_a, name_b)

    # If golden (b) has no properties, fall back to name-only matching.
    if not props_b:
        return ns

    dc = dice_coefficient(props_a, props_b)
    return NAME_WEIGHT * ns + PROP_WEIGHT * dc


def optimal_match(
    actual: dict[str, set[str]],
    expected: dict[str, set[str]],
) -> list[dict]:
    """Optimal assignment between actual and expected schemas using the
    Hungarian algorithm. Returns match records above MATCH_THRESHOLD.

    Each match record includes the composite score, which is used as
    a weighted TP contribution (not binary).
    """
    if not actual or not expected:
        return []

    a_names = list(actual.keys())
    e_names = list(expected.keys())

    # Build cost matrix (Hungarian minimizes, so use negative scores)
    n_a = len(a_names)
    n_e = len(e_names)
    cost = np.zeros((n_a, n_e))
    for i, a_name in enumerate(a_names):
        for j, e_name in enumerate(e_names):
            cost[i, j] = -composite_score(a_name, actual[a_name], e_name, expected[e_name])

    row_ind, col_ind = linear_sum_assignment(cost)

    matches = []
    for i, j in zip(row_ind, col_ind):
        score = -cost[i, j]
        if score >= MATCH_THRESHOLD:
            matches.append({
                "actual": a_names[i],
                "expected": e_names[j],
                "score": round(score, 3),
            })

    return matches


def score_schemas(spec: dict, golden: dict) -> dict:
    """Score schema extraction using composite matching with weighted TPs.

    Instead of binary TP (each match = 1), each match contributes its
    composite score. This makes F1 reflect extraction quality, not just
    detection.
    """
    actual = extract_schemas_from_spec(spec)
    expected = parse_golden_schemas(golden)

    matches = optimal_match(actual, expected)

    # Weighted TP: sum of match scores instead of count
    tp_weighted = sum(m["score"] for m in matches)
    n_matched = len(matches)
    fp = len(actual) - n_matched
    fn = len(expected) - n_matched

    precision = tp_weighted / (tp_weighted + fp) if (tp_weighted + fp) > 0 else 0.0
    recall = tp_weighted / (tp_weighted + fn) if (tp_weighted + fn) > 0 else 0.0

    matched_actual = {m["actual"] for m in matches}
    matched_expected = {m["expected"] for m in matches}

    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1(precision, recall), 3),
        "tp": round(tp_weighted, 3), "fp": fp, "fn": fn,
        "matches": matches,
        "missing": sorted(expected.keys() - matched_expected),
        "extra": sorted(actual.keys() - matched_actual),
    }


# ---------------------------------------------------------------------------
# Top-level scoring
# ---------------------------------------------------------------------------

def f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def load_golden(repo_name: str) -> dict | None:
    golden_path = GOLDEN_DIR / f"{repo_name}.json"
    if not golden_path.exists():
        return None
    return json.loads(golden_path.read_text())


def score_one(result_path: Path) -> dict | None:
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
        "security": score_security_schemes(spec, golden),
        "auth": score_auth(spec, golden),
    }


def main() -> None:
    verbose = "--verbose" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    target = Path(args[0]) if args else Path("/tmp/swagger-test/")

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
    print(
        f"{'Repo':<30s} "
        f"{'EP-P':>5s} {'EP-R':>5s} {'EP-F1':>6s} "
        f"{'SC-P':>5s} {'SC-R':>5s} {'SC-F1':>6s} "
        f"{'SEC-F1':>6s} {'AUTH':>5s}"
    )
    print("-" * 82)

    ep_f1_sum = 0.0
    sc_f1_sum = 0.0
    sec_f1_sum = 0.0
    auth_acc_sum = 0.0

    for s in all_scores:
        ep = s["endpoints"]
        sc = s["schemas"]
        sec = s["security"]
        auth = s["auth"]
        ep_f1_sum += ep["f1"]
        sc_f1_sum += sc["f1"]
        sec_f1_sum += sec["f1"]
        auth_acc_sum += auth["accuracy"]
        print(
            f"{s['repo']:<30s} "
            f"{ep['precision']:>5.2f} {ep['recall']:>5.2f} {ep['f1']:>6.3f} "
            f"{sc['precision']:>5.2f} {sc['recall']:>5.2f} {sc['f1']:>6.3f} "
            f"{sec['f1']:>6.3f} {auth['accuracy']:>5.2f}"
        )

    n = len(all_scores)
    print("-" * 82)
    print(
        f"{'AVERAGE':<30s} "
        f"{'':>5s} {'':>5s} {ep_f1_sum / n:>6.3f} "
        f"{'':>5s} {'':>5s} {sc_f1_sum / n:>6.3f} "
        f"{sec_f1_sum / n:>6.3f} {auth_acc_sum / n:>5.2f}"
    )

    # Print details for repos with issues
    print("\n--- Details ---")
    for s in all_scores:
        ep = s["endpoints"]
        sc = s["schemas"]
        sec = s["security"]
        auth = s["auth"]
        issues = []
        if ep["missing"]:
            issues.append(f"  Missing endpoints: {', '.join(ep['missing'])}")
        if ep["extra"]:
            issues.append(f"  Extra endpoints: {', '.join(ep['extra'])}")
        if sc["missing"]:
            issues.append(f"  Missing schemas: {', '.join(sc['missing'])}")
        if sc["extra"]:
            issues.append(f"  Extra schemas: {', '.join(sc['extra'])}")
        if sec["missing"]:
            issues.append(f"  Missing security schemes: {', '.join(sec['missing'])}")
        if sec["extra"]:
            issues.append(f"  Extra security schemes: {', '.join(sec['extra'])}")
        if auth["mismatches"]:
            issues.append(f"  Auth mismatches: {', '.join(auth['mismatches'])}")
        if verbose and sc.get("matches"):
            match_lines = []
            for m in sc["matches"]:
                if m["actual"] != m["expected"]:
                    match_lines.append(f"    {m['expected']} ~ {m['actual']} ({m['score']})")
            if match_lines:
                issues.append("  Fuzzy matches:\n" + "\n".join(match_lines))
        if issues:
            print(f"\n{s['repo']}:")
            for issue in issues:
                print(issue)


if __name__ == "__main__":
    main()
