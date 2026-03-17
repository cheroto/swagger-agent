"""Synthetic LLM test: verify the model schema change works with grammar-constrained decoding.

Sends a minimal fake route file to the route extractor and checks:
1. LLM produces valid EndpointDescriptor (Pydantic validation passes)
2. No parameters have in_="path" (impossible with new schema)
3. Path uses {param} syntax
4. Infra-generated path params appear correctly after assembly
"""

from swagger_agent.config import LLMConfig, make_client
from swagger_agent.models import (
    EndpointDescriptor,
    Endpoint,
    DiscoveryManifest,
)
from swagger_agent.infra.assembler import assemble_spec, extract_path_params


SYNTHETIC_ROUTE_FILE = """\
const express = require('express');
const router = express.Router();
const { authenticate } = require('../middleware/auth');

// GET /api/users/:id - Get user by ID
router.get('/:id', authenticate, async (req, res) => {
    const user = await User.findById(req.params.id);
    if (!user) return res.status(404).json({ error: 'Not found' });
    res.json(user);
});

// PUT /api/users/:id - Update user
router.put('/:id', authenticate, async (req, res) => {
    const { name, email } = req.body;
    const user = await User.findByIdAndUpdate(req.params.id, { name, email });
    res.json(user);
});

// GET /api/users/:userId/posts/:postId - Get specific post
router.get('/:userId/posts/:postId', async (req, res) => {
    const post = await Post.findOne({
        user: req.params.userId,
        _id: req.params.postId
    });
    res.json(post);
});

// GET /api/users - List all users (query param: ?page=1&limit=10)
router.get('/', async (req, res) => {
    const { page = 1, limit = 10 } = req.query;
    const users = await User.find().skip((page - 1) * limit).limit(limit);
    res.json(users);
});

module.exports = router;
"""

SYSTEM_PROMPT = """\
You are the Route Extractor agent. Extract every HTTP endpoint from this route file into structured format.

## Code Observations
- Routing style: method chaining on express.Router()
- Path parameter syntax: :param
- Base prefix: /api/users
- Request bodies: req.body destructuring
- Error handling: inline try/catch with res.status().json()

## Auth Patterns Observed
- `authenticate` (middleware in handler chain), scheme: BearerAuth/bearer, applies_to: per-endpoint

## Strategy
- Combine base_path + router-level prefix + endpoint-level path. Convert path parameters to OpenAPI {param} syntax.
- Path parameters are derived automatically from {param} segments in the path — do NOT add them to the parameters list.
- Each endpoint's parameters (query/header/cookie only) are independent — do not share between endpoints.
- Extract ALL endpoints in the file. Do not skip any.
- source_file is set by the harness — ignore it.

All field semantics, valid values, and decision rules are defined in the schema descriptions provided by the tool definition. Follow them precisely.
"""


def test_llm_produces_valid_schema():
    """Direct LLM call: verify grammar-constrained output has no path params."""
    config = LLMConfig()
    client, model = make_client(config, "route_extractor")

    print(f"\n  LLM: {config.llm_base_url} / {model}")
    print(f"  Mode: {config.instructor_mode}")

    response = client.chat.completions.create(
        model=model,
        response_model=EndpointDescriptor,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": SYNTHETIC_ROUTE_FILE},
        ],
        temperature=config.llm_temperature,
        max_tokens=config.llm_max_tokens,
        **config.extra_create_kwargs(),
    )

    print(f"  Endpoints extracted: {len(response.endpoints)}")
    for ep in response.endpoints:
        param_summary = ", ".join(f"{p.name}({p.in_})" for p in ep.parameters) or "(none)"
        print(f"    {ep.method} {ep.path} — params: {param_summary}")

    # --- Assertions ---

    # 1. Got endpoints
    assert len(response.endpoints) >= 3, f"Expected >= 3 endpoints, got {len(response.endpoints)}"

    # 2. No parameter has in_="path" (impossible with new Literal, but verify)
    for ep in response.endpoints:
        for p in ep.parameters:
            assert p.in_ != "path", f"LLM output path param '{p.name}' on {ep.method} {ep.path} — should be impossible"

    # 3. Paths with params use {param} syntax
    paths_with_params = [ep.path for ep in response.endpoints if "{" in ep.path]
    assert len(paths_with_params) >= 2, f"Expected >= 2 paths with {{params}}, got {paths_with_params}"
    for path in paths_with_params:
        # No framework-specific syntax should remain
        assert ":" not in path.split("{")[0].split("}")[-1] if "}" in path else True, \
            f"Path still has :param syntax: {path}"

    # 4. extract_path_params works on LLM output
    for ep in response.endpoints:
        params = extract_path_params(ep.path)
        if "{" in ep.path:
            assert len(params) > 0, f"Path {ep.path} has braces but extract_path_params returned nothing"

    # 5. Assembly produces correct path params
    manifest = DiscoveryManifest(
        framework="express", language="javascript",
        servers=["http://localhost:3000"], base_path="/api/users",
    )
    descriptor = EndpointDescriptor(source_file="users.js", endpoints=response.endpoints)
    result = assemble_spec(manifest, [descriptor], {})

    # Check that assembled spec has path params where expected
    for path_key, methods in result.spec.get("paths", {}).items():
        expected_params = extract_path_params(path_key)
        for method, op in methods.items():
            if not isinstance(op, dict):
                continue
            op_params = op.get("parameters", [])
            actual_path_params = [p for p in op_params if p.get("in") == "path"]
            actual_names = {p["name"] for p in actual_path_params}
            expected_names = set(expected_params)
            assert actual_names == expected_names, \
                f"{method.upper()} {path_key}: expected path params {expected_names}, got {actual_names}"
            # All path params must be required
            for p in actual_path_params:
                assert p["required"] is True, f"Path param {p['name']} not required"

    print(f"\n  Assembly: {len(result.spec.get('paths', {}))} paths — all path params correct")
    print("  PASS")


if __name__ == "__main__":
    test_llm_produces_valid_schema()
