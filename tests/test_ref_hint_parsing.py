"""Tests for ref_hint parsing and type decomposition.

Confirms that framework wrapper types (ActionResult<T>, Task<T>,
ResponseEntity<T>) are unwrapped to their inner type — not treated
as arrays — and that infrastructure types (IActionResult,
HttpResponseMessage) are recognized as builtins.
"""

import pytest

from swagger_agent.infra.assembler_pkg.assemble import (
    _build_ref,
    _parse_ref_hint,
    _parse_union_ref_hint,
)
from swagger_agent.infra.schema_loop_pkg.type_hints import _decompose_type_hint


# ── Collections: must produce (True, inner) ──────────────────────────────

@pytest.mark.parametrize("hint, expected_inner", [
    ("List<User>", "User"),
    ("List[User]", "User"),
    ("IEnumerable<Product>", "Product"),
    ("[]User", "User"),
    ("User[]", "User"),
    ("Sequence[Item]", "Item"),
    ("Set<Tag>", "Tag"),
    ("Vec<Article>", "Article"),
    ("Array[Order]", "Order"),
    # ML-family space suffix
    ("Reading.t list", "Reading.t"),
    ("User array", "User"),
])
def test_collections_are_arrays(hint, expected_inner):
    is_array, inner = _parse_ref_hint(hint)
    assert is_array is True, f"{hint} should be detected as array"
    assert inner == expected_inner


# ── Response wrappers: must produce (False, inner) — NOT arrays ──────────

@pytest.mark.parametrize("hint, expected_inner", [
    ("ActionResult<User>", "User"),
    ("ActionResult<List<User>>", "List<User>"),
    ("Task<UserDto>", "UserDto"),
    ("ResponseEntity<PagedResponse>", "PagedResponse"),
    ("ValueTask<OrderDto>", "OrderDto"),
    ("IResult<ItemDto>", "ItemDto"),
    ("Result<ProfileDto>", "ProfileDto"),
    ("Observable<EventDto>", "EventDto"),
    ("Future<UserModel>", "UserModel"),
    ("Promise<DataDto>", "DataDto"),
])
def test_response_wrappers_are_not_arrays(hint, expected_inner):
    is_array, inner = _parse_ref_hint(hint)
    assert is_array is False, f"{hint} should NOT be an array"
    assert inner == expected_inner


# ── Map/Dict types: must produce (False, "") ─────────────────────────────

@pytest.mark.parametrize("hint", [
    "Map<String, dynamic>",
    "Dict[str, Any]",
    "HashMap<String, Object>",
    "dict[str, User]",
])
def test_map_types_are_objects(hint):
    is_array, inner = _parse_ref_hint(hint)
    assert is_array is False
    assert inner == ""  # empty → renders as {type: object}


# ── Plain type names: pass through ───────────────────────────────────────

@pytest.mark.parametrize("hint, expected", [
    ("User", "User"),
    ("Create.Command", "Create.Command"),
    ("ArticleEnvelope", "ArticleEnvelope"),
])
def test_plain_types_pass_through(hint, expected):
    is_array, inner = _parse_ref_hint(hint)
    assert is_array is False
    assert inner == expected


# ── _build_ref integration ───────────────────────────────────────────────

def test_build_ref_collection():
    result = _build_ref("List<User>")
    assert result == {"type": "array", "items": {"$ref": "#/components/schemas/User"}}


def test_build_ref_response_wrapper():
    """ActionResult<User> should produce a direct $ref, not array."""
    result = _build_ref("ActionResult<User>")
    assert result == {"$ref": "#/components/schemas/User"}


def test_build_ref_map_type():
    """Map<String, dynamic> should produce a plain object."""
    result = _build_ref("Map<String, dynamic>")
    assert result["type"] == "object"


# ── Framework infrastructure types as builtins ───────────────────────────

@pytest.mark.parametrize("type_name", [
    "IActionResult",
    "ActionResult",
    "HttpResponseMessage",
    "HttpResponse",
    "IHttpActionResult",
    "IFormFile",
    "CancellationToken",
    "HealthCheckResult",
    "FileResult",
    "JsonResult",
    "ViewResult",
    "ContentResult",
    "StatusCodeResult",
    # Java/Spring
    "ResponseEntity",
    "HttpServletRequest",
    "HttpServletResponse",
    "ModelAndView",
    # Node/Express
    "Request",
    "Response",
    "NextFunction",
])
def test_framework_types_are_builtins(type_name):
    result = _decompose_type_hint(type_name)
    assert result == [], f"{type_name} should decompose to [] (builtin)"


# ── Decompose still works for real types ─────────────────────────────────

@pytest.mark.parametrize("hint, expected", [
    ("User", ["User"]),
    ("List[User]", ["User"]),
    ("Optional[Article]", ["Article"]),
    ("Union[CreditCard, BankTransfer]", ["CreditCard", "BankTransfer"]),
    ("Dict[str, Any]", []),
    ("str", []),
    ("int", []),
    ("String, dynamic", []),  # bare comma-separated builtins
    ("ActionResult<UserDto>", ["UserDto"]),  # unwrap response wrapper
    ("Task<List<Item>>", ["Item"]),  # nested: Task wraps List wraps Item
])
def test_decompose_type_hint(hint, expected):
    assert _decompose_type_hint(hint) == expected


# ── Bare comma-separated types ───────────────────────────────────────────

def test_bare_comma_with_real_types():
    """UserDto, ErrorDto should decompose to both types."""
    result = _decompose_type_hint("UserDto, ErrorDto")
    assert "UserDto" in result
    assert "ErrorDto" in result
