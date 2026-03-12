"""Tests for assembler post-processing fixes."""

from swagger_agent.infra.assembler import (
    AssemblyResult,
    _break_ref_cycles,
    _build_ref,
    _deduplicate_operation_ids,
    _fix_ref_siblings,
    assemble_spec,
)
from swagger_agent.models import (
    DiscoveryManifest,
    Endpoint,
    EndpointDescriptor,
    RefHint,
    RequestBody,
    Response,
)


def _make_manifest() -> DiscoveryManifest:
    return DiscoveryManifest(
        framework="test",
        language="test",
        servers=["http://localhost:3000"],
        base_path="/api",
    )


# --- Fix 1: Deduplicate operationIds ---


class TestDeduplicateOperationIds:
    def test_no_collision_unchanged(self):
        spec = {
            "paths": {
                "/users": {"get": {"operationId": "ListUsers", "tags": ["Users"]}},
                "/posts": {"get": {"operationId": "ListPosts", "tags": ["Posts"]}},
            }
        }
        _deduplicate_operation_ids(spec)
        assert spec["paths"]["/users"]["get"]["operationId"] == "ListUsers"
        assert spec["paths"]["/posts"]["get"]["operationId"] == "ListPosts"

    def test_collision_disambiguated_with_tag(self):
        spec = {
            "paths": {
                "/articles": {"get": {"operationId": "Get", "tags": ["Articles"]}},
                "/comments": {"get": {"operationId": "Get", "tags": ["Comments"]}},
            }
        }
        _deduplicate_operation_ids(spec)
        ids = {
            spec["paths"]["/articles"]["get"]["operationId"],
            spec["paths"]["/comments"]["get"]["operationId"],
        }
        assert "Articles_Get" in ids
        assert "Comments_Get" in ids

    def test_collision_without_tags_uses_path(self):
        spec = {
            "paths": {
                "/a": {"get": {"operationId": "Get"}},
                "/b": {"get": {"operationId": "Get"}},
            }
        }
        _deduplicate_operation_ids(spec)
        id_a = spec["paths"]["/a"]["get"]["operationId"]
        id_b = spec["paths"]["/b"]["get"]["operationId"]
        assert id_a != id_b
        assert "Get" in id_a
        assert "Get" in id_b

    def test_secondary_collision_gets_hash(self):
        """Same tag + same operationId on different paths → hash suffix."""
        spec = {
            "paths": {
                "/v1/items": {"get": {"operationId": "Get", "tags": ["Items"]}},
                "/v2/items": {"get": {"operationId": "Get", "tags": ["Items"]}},
            }
        }
        _deduplicate_operation_ids(spec)
        id1 = spec["paths"]["/v1/items"]["get"]["operationId"]
        id2 = spec["paths"]["/v2/items"]["get"]["operationId"]
        assert id1 != id2
        # One should be Items_Get, the other Items_Get_<hash>
        assert id1.startswith("Items_Get")
        assert id2.startswith("Items_Get")


# --- Fix 2: Wrap $ref + nullable with allOf ---


class TestFixRefSiblings:
    def test_ref_with_nullable_wrapped(self):
        schema = {"$ref": "#/components/schemas/Person", "nullable": True}
        result = _fix_ref_siblings(schema)
        assert "allOf" in result
        assert result["allOf"] == [{"$ref": "#/components/schemas/Person"}]
        assert result["nullable"] is True
        assert "$ref" not in result

    def test_bare_ref_untouched(self):
        schema = {"$ref": "#/components/schemas/Person"}
        result = _fix_ref_siblings(schema)
        assert result == {"$ref": "#/components/schemas/Person"}

    def test_nested_ref_siblings_fixed(self):
        schema = {
            "type": "object",
            "properties": {
                "author": {"$ref": "#/components/schemas/User", "nullable": True},
                "name": {"type": "string"},
            },
        }
        _fix_ref_siblings(schema)
        author = schema["properties"]["author"]
        assert "allOf" in author
        assert author["nullable"] is True
        assert "$ref" not in author

    def test_ref_in_array_items_with_siblings(self):
        schema = {
            "type": "array",
            "items": {"$ref": "#/components/schemas/Tag", "description": "A tag"},
        }
        _fix_ref_siblings(schema)
        items = schema["items"]
        assert "allOf" in items
        assert items["description"] == "A tag"


# --- Fix 3: Break circular $ref cycles ---


class TestBreakRefCycles:
    def test_simple_cycle_broken(self):
        spec = {
            "components": {
                "schemas": {
                    "A": {
                        "type": "object",
                        "properties": {
                            "b": {"$ref": "#/components/schemas/B"},
                        },
                    },
                    "B": {
                        "type": "object",
                        "properties": {
                            "a": {"$ref": "#/components/schemas/A"},
                        },
                    },
                }
            }
        }
        _break_ref_cycles(spec)
        schemas = spec["components"]["schemas"]
        # At least one direction should be broken
        a_refs_b = "$ref" in (schemas["A"].get("properties", {}).get("b", {}))
        b_refs_a = "$ref" in (schemas["B"].get("properties", {}).get("a", {}))
        # One must be broken (replaced with x-circular-ref)
        assert not (a_refs_b and b_refs_a), "Cycle not broken"

        # Find the broken one
        for s_name in ("A", "B"):
            for prop in schemas[s_name].get("properties", {}).values():
                if "x-circular-ref" in prop:
                    assert prop["type"] == "object"
                    assert "Circular reference" in prop["description"]

    def test_array_edge_preferred_for_cut(self):
        spec = {
            "components": {
                "schemas": {
                    "Article": {
                        "type": "object",
                        "properties": {
                            "favorites": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/Favorite"},
                            }
                        },
                    },
                    "Favorite": {
                        "type": "object",
                        "properties": {
                            "article": {"$ref": "#/components/schemas/Article"},
                        },
                    },
                }
            }
        }
        _break_ref_cycles(spec)
        schemas = spec["components"]["schemas"]
        # The array edge (Article→Favorite) should be cut preferentially
        fav_items = schemas["Article"]["properties"]["favorites"]["items"]
        assert "x-circular-ref" in fav_items or "$ref" not in fav_items

    def test_no_cycle_untouched(self):
        spec = {
            "components": {
                "schemas": {
                    "A": {
                        "type": "object",
                        "properties": {"b": {"$ref": "#/components/schemas/B"}},
                    },
                    "B": {"type": "object", "properties": {"name": {"type": "string"}}},
                }
            }
        }
        _break_ref_cycles(spec)
        assert spec["components"]["schemas"]["A"]["properties"]["b"]["$ref"] == "#/components/schemas/B"


# --- Fix 4: Empty $ref guard ---


class TestEmptyRefGuard:
    def test_empty_ref_hint_returns_placeholder(self):
        result = _build_ref("")
        assert result["type"] == "object"
        assert result["x-unresolved"] is True

    def test_whitespace_ref_hint_returns_placeholder(self):
        result = _build_ref("   ")
        assert result["type"] == "object"
        assert result["x-unresolved"] is True

    def test_valid_ref_hint_works(self):
        result = _build_ref("UserResponse")
        assert result == {"$ref": "#/components/schemas/UserResponse"}

    def test_empty_ref_not_added_to_schemas(self):
        """End-to-end: empty ref_hint shouldn't produce broken $ref in spec."""
        manifest = _make_manifest()
        descriptors = [
            EndpointDescriptor(
                source_file="routes.py",
                endpoints=[
                    Endpoint(
                        method="GET",
                        path="/test",
                        operation_id="GetTest",
                        responses=[
                            Response(
                                status_code="200",
                                description="OK",
                                schema_ref=RefHint(
                                    ref_hint="",
                                    resolution="unresolvable",
                                ),
                            )
                        ],
                    )
                ],
            )
        ]
        result = assemble_spec(manifest, descriptors, {})
        # The response schema should be the inline placeholder, not a broken $ref
        resp_schema = result.spec["paths"]["/api/test"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert "$ref" not in resp_schema
        assert resp_schema["x-unresolved"] is True


# --- Integration: all fixes together ---


class TestAssembleSpecIntegration:
    def test_full_assembly_with_all_fixes(self):
        manifest = _make_manifest()
        descriptors = [
            EndpointDescriptor(
                source_file="articles.py",
                endpoints=[
                    Endpoint(
                        method="GET",
                        path="/items",
                        operation_id="Get",
                        tags=["Articles"],
                        responses=[
                            Response(status_code="200", description="OK",
                                     schema_ref=RefHint(ref_hint="Article", resolution="import"))
                        ],
                    ),
                ],
            ),
            EndpointDescriptor(
                source_file="comments.py",
                endpoints=[
                    Endpoint(
                        method="GET",
                        path="/comments",
                        operation_id="Get",
                        tags=["Comments"],
                        responses=[
                            Response(status_code="200", description="OK",
                                     schema_ref=RefHint(ref_hint="Comment", resolution="import"))
                        ],
                    ),
                ],
            ),
        ]
        schemas = {
            "Article": {
                "type": "object",
                "properties": {
                    "comments": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/Comment"},
                    },
                },
            },
            "Comment": {
                "type": "object",
                "properties": {
                    "article": {"$ref": "#/components/schemas/Article", "nullable": True},
                },
            },
        }
        result = assemble_spec(manifest, descriptors, schemas)

        # Fix 1: operationIds deduplicated
        op_ids = set()
        for methods in result.spec["paths"].values():
            for op in methods.values():
                op_ids.add(op["operationId"])
        assert len(op_ids) == 2  # no collision

        # Fix 2: nullable $ref wrapped in allOf
        comment_schema = result.spec["components"]["schemas"]["Comment"]
        article_prop = comment_schema["properties"]["article"]
        assert "allOf" in article_prop
        assert article_prop.get("nullable") is True

        # Fix 3: cycle broken
        # At least one direction should have x-circular-ref
        has_circular = False
        for schema in result.spec["components"]["schemas"].values():
            for prop in schema.get("properties", {}).values():
                if "x-circular-ref" in prop:
                    has_circular = True
                items = prop.get("items", {})
                if isinstance(items, dict) and "x-circular-ref" in items:
                    has_circular = True
        assert has_circular
