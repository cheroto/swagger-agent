"""Tests for assembler post-processing fixes."""

from swagger_agent.infra.assembler import (
    AssemblyResult,
    _break_ref_cycles,
    _build_ref,
    _coerce_to_schema,
    _deduplicate_operation_ids,
    _fix_non_schema_properties,
    _fix_ref_siblings,
    _normalize_path,
    _normalize_schema_case,
    assemble_spec,
    extract_path_params,
)
from swagger_agent.models import (
    DiscoveryManifest,
    Endpoint,
    EndpointDescriptor,
    Parameter,
    RefHint,
    RequestBody,
    Response,
    SecurityRequirement,
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
                                    import_line="", file_namespace="",
                                    resolution="unresolvable",
                                ),
                            )
                        ],
                    )
                ],
            )
        ]
        result = assemble_spec(manifest, descriptors, {})
        # An empty ref_hint should produce no content block (bodyless response)
        resp = result.spec["paths"]["/api/test"]["get"]["responses"]["200"]
        assert "content" not in resp, (
            "Empty ref_hint should not produce a content block in the response"
        )


# --- Fix: RefHint.is_array produces array schema ---


class TestRefHintIsArray:
    def test_is_array_produces_array_schema(self):
        """RefHint with is_array=True should produce array wrapper around $ref."""
        from swagger_agent.infra.assembler import _build_schema_for_ref

        ref = RefHint(ref_hint="Article", resolution="import", import_line="from app import Article", file_namespace="", is_array=True)
        result = _build_schema_for_ref(ref)
        assert result["type"] == "array"
        assert result["items"] == {"$ref": "#/components/schemas/Article"}

    def test_is_nullable_adds_nullable(self):
        """RefHint with is_nullable=True should add nullable: true."""
        from swagger_agent.infra.assembler import _build_schema_for_ref

        ref = RefHint(ref_hint="User", resolution="import", import_line="from app import User", file_namespace="", is_nullable=True)
        result = _build_schema_for_ref(ref)
        assert result["$ref"] == "#/components/schemas/User"
        assert result["nullable"] is True

    def test_is_array_and_nullable_combined(self):
        """Both is_array and is_nullable should produce nullable array."""
        from swagger_agent.infra.assembler import _build_schema_for_ref

        ref = RefHint(ref_hint="Tag", resolution="import", import_line="from app import Tag", file_namespace="", is_array=True, is_nullable=True)
        result = _build_schema_for_ref(ref)
        assert result["type"] == "array"
        assert result["items"] == {"$ref": "#/components/schemas/Tag"}
        assert result["nullable"] is True

    def test_defaults_false_no_change(self):
        """RefHint with defaults (is_array=False) should use _build_ref as before."""
        from swagger_agent.infra.assembler import _build_schema_for_ref

        ref = RefHint(ref_hint="User", resolution="import", import_line="from app import User", file_namespace="")
        result = _build_schema_for_ref(ref)
        assert result == {"$ref": "#/components/schemas/User"}


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
                                     schema_ref=RefHint(ref_hint="Article", import_line="from app.models import Article", file_namespace="", resolution="import"))
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
                                     schema_ref=RefHint(ref_hint="Comment", import_line="from app.models import Comment", file_namespace="", resolution="import"))
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


# --- extract_path_params ---


class TestExtractPathParams:
    def test_single_param(self):
        assert extract_path_params("/users/{id}") == ["id"]

    def test_multiple_params(self):
        assert extract_path_params("/users/{id}/posts/{postId}") == ["id", "postId"]

    def test_no_params(self):
        assert extract_path_params("/health") == []

    def test_constraint_stripped(self):
        assert extract_path_params("/tasks/{taskId:guid}/complete") == ["taskId"]

    def test_multiple_constraints(self):
        assert extract_path_params("/v{version:apiVersion}/{resource:int}/items") == ["version", "resource"]

    def test_root_path(self):
        assert extract_path_params("/") == []


# --- Path template normalization ---


class TestNormalizePath:
    def test_base_path_deduplication(self):
        """When endpoint path already contains base_path, don't double it."""
        result = _normalize_path("/api/v1", "/api/v1/users")
        assert result == "/api/v1/users"

    def test_base_path_prepended(self):
        result = _normalize_path("/api", "/users")
        assert result == "/api/users"

    def test_constraint_stripped(self):
        """Route constraints like {id:guid} are stripped to {id}."""
        result = _normalize_path("", "/tasks/{taskId:guid}/complete")
        assert result == "/tasks/{taskId}/complete"

    def test_trailing_slash_removed(self):
        result = _normalize_path("", "/users/")
        assert result == "/users"

    def test_root_path_preserved(self):
        result = _normalize_path("", "/")
        assert result == "/"

    def test_double_slashes_collapsed(self):
        result = _normalize_path("/api/", "/users")
        assert result == "/api/users"

    def test_optional_marker_stripped(self):
        result = _normalize_path("", "/users/{id?}")
        assert result == "/users/{id}"

    def test_catch_all_stripped(self):
        result = _normalize_path("", "/files/{*path}")
        assert result == "/files/{path}"

    def test_default_value_stripped(self):
        result = _normalize_path("", "/api/{version=v1}/users")
        assert result == "/api/{version}/users"


# --- Infra-generated path params in assembly ---


class TestAssemblyPathParams:
    def test_path_params_auto_generated(self):
        """Path params are derived from path template, not from LLM output."""
        manifest = _make_manifest()
        manifest.base_path = ""
        descriptors = [
            EndpointDescriptor(
                source_file="ctrl.py",
                endpoints=[
                    Endpoint(
                        method="GET",
                        path="/users/{id}/posts/{postId}",
                        operation_id="GetPost",
                        # No parameters — infra generates path params
                    ),
                ],
            )
        ]
        result = assemble_spec(manifest, descriptors, {})
        params = result.spec["paths"]["/users/{id}/posts/{postId}"]["get"]["parameters"]
        path_params = [p for p in params if p["in"] == "path"]
        assert len(path_params) == 2
        names = {p["name"] for p in path_params}
        assert names == {"id", "postId"}
        # All path params must be required with type string
        for p in path_params:
            assert p["required"] is True
            assert p["schema"] == {"type": "string"}

    def test_query_params_preserved_alongside_path(self):
        """LLM query params coexist with infra-generated path params."""
        manifest = _make_manifest()
        manifest.base_path = ""
        descriptors = [
            EndpointDescriptor(
                source_file="ctrl.py",
                endpoints=[
                    Endpoint(
                        method="GET",
                        path="/users/{id}",
                        operation_id="GetUser",
                        parameters=[
                            Parameter(name="fields", in_="query"),
                        ],
                    ),
                ],
            )
        ]
        result = assemble_spec(manifest, descriptors, {})
        params = result.spec["paths"]["/users/{id}"]["get"]["parameters"]
        assert len(params) == 2
        path_params = [p for p in params if p["in"] == "path"]
        query_params = [p for p in params if p["in"] == "query"]
        assert len(path_params) == 1
        assert path_params[0]["name"] == "id"
        assert len(query_params) == 1
        assert query_params[0]["name"] == "fields"

    def test_no_path_params_on_static_path(self):
        """Paths without {param} segments get no path parameters."""
        manifest = _make_manifest()
        manifest.base_path = ""
        descriptors = [
            EndpointDescriptor(
                source_file="ctrl.py",
                endpoints=[
                    Endpoint(
                        method="GET",
                        path="/health",
                        operation_id="Health",
                    ),
                ],
            )
        ]
        result = assemble_spec(manifest, descriptors, {})
        op = result.spec["paths"]["/health"]["get"]
        assert "parameters" not in op

    def test_constraint_in_path_produces_correct_param(self):
        """Constraints are stripped from path, param name is correct."""
        manifest = _make_manifest()
        manifest.base_path = ""
        descriptors = [
            EndpointDescriptor(
                source_file="ctrl.cs",
                endpoints=[
                    Endpoint(
                        method="PATCH",
                        path="/tasks/{taskId:guid}/complete",
                        operation_id="Complete",
                    ),
                ],
            )
        ]
        result = assemble_spec(manifest, descriptors, {})
        assert "/tasks/{taskId}/complete" in result.spec["paths"]
        params = result.spec["paths"]["/tasks/{taskId}/complete"]["patch"]["parameters"]
        path_params = [p for p in params if p["in"] == "path"]
        assert len(path_params) == 1
        assert path_params[0]["name"] == "taskId"


# --- Fix: Non-schema property values coerced ---


class TestCoerceToSchema:
    def test_bare_type_string(self):
        assert _coerce_to_schema("string") == {"type": "string"}
        assert _coerce_to_schema("integer") == {"type": "integer"}
        assert _coerce_to_schema("Boolean") == {"type": "boolean"}

    def test_ref_string(self):
        result = _coerce_to_schema("#/components/schemas/Foo")
        assert result == {"$ref": "#/components/schemas/Foo"}

    def test_pascal_case_type_name(self):
        result = _coerce_to_schema("UserProfile")
        assert result == {"$ref": "#/components/schemas/UserProfile"}

    def test_json_string(self):
        result = _coerce_to_schema('{"type": "string", "format": "email"}')
        assert result == {"type": "string", "format": "email"}

    def test_none(self):
        result = _coerce_to_schema(None)
        assert result == {"type": "string", "nullable": True}

    def test_number(self):
        result = _coerce_to_schema(42)
        assert result == {"type": "number"}

    def test_bool(self):
        result = _coerce_to_schema(True)
        assert result == {"type": "boolean"}


class TestFixNonSchemaProperties:
    def test_bare_string_property_fixed(self):
        schema = {
            "type": "object",
            "properties": {
                "name": "string",
                "age": "integer",
                "valid": {"type": "boolean"},
            },
        }
        _fix_non_schema_properties(schema)
        assert schema["properties"]["name"] == {"type": "string"}
        assert schema["properties"]["age"] == {"type": "integer"}
        assert schema["properties"]["valid"] == {"type": "boolean"}

    def test_nested_properties_fixed(self):
        schema = {
            "type": "object",
            "properties": {
                "inner": {
                    "type": "object",
                    "properties": {
                        "bad": "number",
                    },
                },
            },
        }
        _fix_non_schema_properties(schema)
        assert schema["properties"]["inner"]["properties"]["bad"] == {"type": "number"}

    def test_assembly_fixes_bad_properties(self):
        """End-to-end: bare string properties fixed during assembly."""
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
                            Response(status_code="200", description="OK",
                                     schema_ref=RefHint(ref_hint="Bad", import_line="", file_namespace="", resolution="import"))
                        ],
                    ),
                ],
            )
        ]
        schemas = {
            "Bad": {
                "type": "object",
                "properties": {
                    "name": "string",
                    "ref": "UserProfile",
                },
            },
        }
        result = assemble_spec(manifest, descriptors, schemas)
        bad_schema = result.spec["components"]["schemas"]["Bad"]
        assert bad_schema["properties"]["name"] == {"type": "string"}
        assert bad_schema["properties"]["ref"] == {"$ref": "#/components/schemas/UserProfile"}


# --- Fix: Case normalization ---


class TestNormalizeSchemaCase:
    def test_case_mismatch_fixed(self):
        spec = {
            "paths": {
                "/test": {
                    "get": {
                        "operationId": "GetTest",
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/AffiliateModelViewModel"}
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "components": {
                "schemas": {
                    "affiliatemodelviewmodel": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    }
                }
            },
        }
        _normalize_schema_case(spec)
        # Schema key should now match the $ref target
        assert "AffiliateModelViewModel" in spec["components"]["schemas"]
        assert "affiliatemodelviewmodel" not in spec["components"]["schemas"]

    def test_internal_refs_updated(self):
        spec = {
            "paths": {
                "/test": {
                    "get": {
                        "operationId": "GetTest",
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/Parent"}
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "components": {
                "schemas": {
                    "parent": {
                        "type": "object",
                        "properties": {
                            "child": {"$ref": "#/components/schemas/child"},
                        },
                    },
                    "child": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    },
                }
            },
        }
        _normalize_schema_case(spec)
        assert "Parent" in spec["components"]["schemas"]
        # Internal $ref to "child" should be unchanged (it matches the key)
        parent = spec["components"]["schemas"]["Parent"]
        assert parent["properties"]["child"]["$ref"] == "#/components/schemas/child"

    def test_no_mismatch_unchanged(self):
        spec = {
            "paths": {},
            "components": {
                "schemas": {
                    "User": {"type": "object"},
                }
            },
        }
        _normalize_schema_case(spec)
        assert "User" in spec["components"]["schemas"]

    def test_assembly_case_normalization(self):
        """End-to-end: case-mismatched schemas found during assembly."""
        manifest = _make_manifest()
        descriptors = [
            EndpointDescriptor(
                source_file="routes.cs",
                endpoints=[
                    Endpoint(
                        method="GET",
                        path="/items",
                        operation_id="GetItems",
                        responses=[
                            Response(status_code="200", description="OK",
                                     schema_ref=RefHint(ref_hint="ItemViewModel", import_line="", file_namespace="", resolution="import"))
                        ],
                    ),
                ],
            )
        ]
        # Schema stored under wrong case (as LLM might produce)
        schemas = {
            "itemviewmodel": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
        }
        result = assemble_spec(manifest, descriptors, schemas)
        # Should be found and stored under the $ref-matching name
        assert "ItemViewModel" in result.spec["components"]["schemas"]
        assert not result.spec["components"]["schemas"]["ItemViewModel"].get("x-unresolved")
