"""Type hint decomposition for schema resolution.

LLMs emit ref_hints as raw type annotations from the source language:
  List[User], Optional[str], Union[CreditCard, BankTransfer], Dict[str, Any]

These must be decomposed into individual resolvable type names before
queuing for schema resolution. The decomposition is language-agnostic —
all languages use some form of Generic[T] or Generic<T> syntax.
"""

from __future__ import annotations

import re


# Types that map directly to JSON Schema primitives — never resolve these.
_BUILTIN_TYPES = frozenset({
    # Language primitives
    "str", "int", "float", "bool", "bytes", "None", "NoneType",
    "dict", "list", "set", "tuple", "Any", "object",
    "string", "String", "integer", "Integer", "long", "Long",
    "double", "Double", "number", "Number", "boolean", "Boolean",
    "void", "Void", "byte", "Byte", "char", "short",
    "Object", "Map", "HashMap", "Array", "List", "Set",
    "any", "unknown", "undefined", "null", "never", "dynamic",
    # Framework HTTP/response infrastructure — no user-defined schema
    "IActionResult", "ActionResult", "IHttpActionResult",
    "HttpResponseMessage", "HttpResponse", "HttpRequestMessage",
    "IFormFile", "FormFile", "CancellationToken",
    "HealthCheckResult", "FileResult", "JsonResult", "ViewResult",
    "ContentResult", "StatusCodeResult", "ObjectResult",
    "ResponseEntity", "HttpServletRequest", "HttpServletResponse",
    "ModelAndView", "RedirectView",
    "Request", "Response", "NextFunction",
})

# Wrappers that contain a single inner type (unwrap → resolve inner).
_PASSTHROUGH_WRAPPERS = frozenset({
    # Collections
    "List", "list", "Sequence", "Set", "set", "FrozenSet", "frozenset",
    "Tuple", "tuple", "Iterable", "Iterator", "Generator",
    "Optional", "Type", "ClassVar",
    "Array", "Vec", "vector", "IEnumerable", "IList", "ICollection",
    "Collection", "Deque", "deque", "Queue",
    # Response/async wrappers — unwrap to the inner type
    "ActionResult", "Task", "ValueTask", "ResponseEntity",
    "Result", "IResult", "Observable", "Future", "Promise",
    "Mono", "Flux", "Single", "Maybe", "Completable",
})

# Regex: Wrapper[InnerContent] or Wrapper<InnerContent>
_GENERIC_RE = re.compile(r"^(\w+)\s*[\[<](.+)[\]>]$")


def _decompose_type_hint(name: str) -> list[str]:
    """Decompose a type hint into individual resolvable type names.

    Returns a list of type names to queue for resolution. Skips builtins.

    Examples:
        "User"                          → ["User"]
        "List[User]"                    → ["User"]
        "Optional[User]"                → ["User"]
        "Union[CreditCard, BankTransfer]" → ["CreditCard", "BankTransfer"]
        "Dict[str, Any]"                → []  (all builtins)
        "str"                           → []  (builtin)
        "dict[str, User]"               → ["User"]
    """
    name = name.strip()

    if name in _BUILTIN_TYPES:
        return []

    # Space-suffixed collection types: "User list", "User array", "User option"
    # Common in ML-family languages (OCaml, Haskell, F#).
    _SPACE_SUFFIXES = {"list", "array", "option", "seq", "set", "ref"}
    parts = name.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].lower() in _SPACE_SUFFIXES:
        return _decompose_type_hint(parts[0])

    # Bare comma-separated types: "String, dynamic" (leaked Map<K,V> contents).
    # Split on commas and decompose each — if all are builtins, return [].
    if "," in name and "[" not in name and "<" not in name:
        parts = [p.strip() for p in name.split(",") if p.strip()]
        result = []
        for part in parts:
            result.extend(_decompose_type_hint(part))
        return result

    m = _GENERIC_RE.match(name)
    if not m:
        return [name]

    wrapper = m.group(1)
    inner_raw = m.group(2)

    if wrapper in ("Union", "union"):
        parts = _split_generic_args(inner_raw)
        result = []
        for part in parts:
            result.extend(_decompose_type_hint(part))
        return result

    if wrapper in ("Dict", "dict", "Map", "HashMap", "map",
                    "Mapping", "OrderedDict", "defaultdict"):
        parts = _split_generic_args(inner_raw)
        if len(parts) >= 2:
            return _decompose_type_hint(parts[-1])
        return []

    if wrapper in _PASSTHROUGH_WRAPPERS:
        parts = _split_generic_args(inner_raw)
        result = []
        for part in parts:
            result.extend(_decompose_type_hint(part))
        return result

    # Unknown wrapper — try inner types
    parts = _split_generic_args(inner_raw)
    result = []
    for part in parts:
        result.extend(_decompose_type_hint(part))
    return result if result else [name]


def _split_generic_args(s: str) -> list[str]:
    """Split generic type arguments on commas, respecting nested brackets.

    "A, B, C"                     → ["A", "B", "C"]
    "str, List[int]"              → ["str", "List[int]"]
    "Dict[str, Any], User"        → ["Dict[str, Any]", "User"]
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in s:
        if ch in ("[", "<"):
            depth += 1
            current.append(ch)
        elif ch in ("]", ">"):
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    remainder = "".join(current).strip()
    if remainder:
        parts.append(remainder)
    return parts
