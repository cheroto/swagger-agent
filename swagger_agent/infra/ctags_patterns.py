"""Custom ctags patterns for framework-specific model/schema definitions.

Universal-ctags natively indexes class, interface, struct, enum, type, and
trait definitions.  These patterns catch the remaining cases where models are
registered via function calls, factory methods, or DSL macros that ctags
doesn't recognise out of the box.

Each entry is a tuple of (kinddef_flag | None, regex_flag).
  - kinddef_flag defines a new kind letter+name for the target language
    (only needed once per language — subsequent patterns reuse it via None).
  - regex_flag is a POSIX ERE pattern passed to --regex-<LANG>.

All patterns use the custom kind letter 'd' with kind name 'model'.

To add a new pattern:
  1. Write the --regex-<LANG> flag using POSIX ERE (not PCRE).
  2. Test it:  ctags --output-format=json --fields=+n <flags> <test_file>
  3. Ensure the capture group extracts the model/schema NAME, not a variable.
  4. Prefer extracting the registered name (e.g. 'User' in mongoose.model('User'))
     over the variable name, since the registered name matches ref_hint lookups.
"""

from __future__ import annotations

# Quote chars in ctags regex: use \x22 for double-quote since the flags are
# passed as Python strings → shell args.  Single-quotes are literal.
_DQ = '"'
_SQ = "'"
_Q = rf"[{_SQ}{_DQ}]"  # character class matching either quote


def _kinddef(lang: str) -> str:
    """First-use kind definition for a language."""
    return f"--kinddef-{lang}=d,model,model definitions"


# ─── JavaScript ──────────────────────────────────────────────────────────

_JS_KINDDEF = _kinddef("JavaScript")

_JS_PATTERNS: list[tuple[str | None, str]] = [
    # Mongoose: mongoose.model('User', schema) — require comma after name to
    # match only the 2-arg registration form, not the 1-arg retrieval form
    # (mongoose.model('User') without comma).
    (
        _JS_KINDDEF,
        rf"--regex-JavaScript=/mongoose\.model\({_Q}([^{_SQ}{_DQ}]+){_Q}[[:space:]]*,/\1/d/",
    ),
    # Sequelize: sequelize.define('user', { ... })
    (
        None,
        rf"--regex-JavaScript=/\.define\({_Q}([^{_SQ}{_DQ}]+){_Q}/\1/d/",
    ),
    # Drizzle ORM: pgTable('users', ...), mysqlTable('users', ...), sqliteTable('users', ...)
    (
        None,
        rf"--regex-JavaScript=/\b(pgTable|mysqlTable|sqliteTable)\({_Q}([^{_SQ}{_DQ}]+){_Q}/\2/d/",
    ),
    # Bookshelf.js: bookshelf.Model.extend({ tableName: '...' })
    (
        None,
        r"--regex-JavaScript=/^[[:space:]]*(const|let|var)[[:space:]]+([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*bookshelf\.Model\.extend/\2/d/",
    ),
    # Joi: const userSchema = Joi.object({ ... })
    (
        None,
        r"--regex-JavaScript=/^[[:space:]]*(const|let|var)[[:space:]]+([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*Joi\.object/\2/d/",
    ),
    # Knex: .createTable('users', ...)
    (
        None,
        rf"--regex-JavaScript=/\.createTable\({_Q}([^{_SQ}{_DQ}]+){_Q}/\1/d/",
    ),
]


# ─── TypeScript ──────────────────────────────────────────────────────────

_TS_KINDDEF = _kinddef("TypeScript")

_TS_PATTERNS: list[tuple[str | None, str]] = [
    # Mongoose: mongoose.model('User', schema) — require comma (see JS comment)
    (
        _TS_KINDDEF,
        rf"--regex-TypeScript=/mongoose\.model\({_Q}([^{_SQ}{_DQ}]+){_Q}[[:space:]]*,/\1/d/",
    ),
    # Sequelize: sequelize.define('user', { ... })
    (
        None,
        rf"--regex-TypeScript=/\.define\({_Q}([^{_SQ}{_DQ}]+){_Q}/\1/d/",
    ),
    # Drizzle ORM: pgTable / mysqlTable / sqliteTable
    (
        None,
        rf"--regex-TypeScript=/\b(pgTable|mysqlTable|sqliteTable)\({_Q}([^{_SQ}{_DQ}]+){_Q}/\2/d/",
    ),
    # TypeORM EntitySchema: new EntitySchema({ name: 'Category' })
    (
        None,
        rf"--regex-TypeScript=/new[[:space:]]+EntitySchema[^[{{]]*[{{][^[}}]]*name:[[:space:]]*{_Q}([^{_SQ}{_DQ}]+){_Q}/\1/d/",
    ),
    # MikroORM defineEntity: defineEntity({ name: 'Product' })
    (
        None,
        rf"--regex-TypeScript=/defineEntity\([[:space:]]*[{{][^[}}]]*name:[[:space:]]*{_Q}([^{_SQ}{_DQ}]+){_Q}/\1/d/",
    ),
    # Zod: const UserSchema = z.object({ ... })
    (
        None,
        r"--regex-TypeScript=/^[[:space:]]*(export[[:space:]]+)?(const|let|var)[[:space:]]+([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*z\.object/\3/d/",
    ),
    # Joi: const userSchema = Joi.object({ ... })
    (
        None,
        r"--regex-TypeScript=/^[[:space:]]*(const|let|var)[[:space:]]+([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*Joi\.object/\2/d/",
    ),
    # Knex: .createTable('users', ...)
    (
        None,
        rf"--regex-TypeScript=/\.createTable\({_Q}([^{_SQ}{_DQ}]+){_Q}/\1/d/",
    ),
]


# ─── Python ──────────────────────────────────────────────────────────────

_PY_KINDDEF = _kinddef("Python")

_PY_PATTERNS: list[tuple[str | None, str]] = [
    # SQLAlchemy classical: user_table = Table('user', metadata, ...)
    (
        _PY_KINDDEF,
        r"--regex-Python=/^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*Table\(/\1/d/",
    ),
    # SQLAlchemy mapper: mapper(User, user_table)
    (
        None,
        r"--regex-Python=/^mapper\([[:space:]]*([A-Za-z_][A-Za-z0-9_]*)/\1/d/",
    ),
    # SQLAlchemy 2.0: registry.map_imperatively(User, ...)
    (
        None,
        r"--regex-Python=/map_imperatively\([[:space:]]*([A-Za-z_][A-Za-z0-9_]*)/\1/d/",
    ),
    # Pydantic dynamic: UserModel = create_model('UserModel', ...)
    (
        None,
        r"--regex-Python=/^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*create_model\(/\1/d/",
    ),
    # Marshmallow: UserSchema = Schema.from_dict({ ... })
    (
        None,
        r"--regex-Python=/^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*Schema\.from_dict\(/\1/d/",
    ),
]


# ─── Aggregate ───────────────────────────────────────────────────────────

CUSTOM_CTAGS_PATTERNS: list[tuple[str | None, str]] = [
    *_JS_PATTERNS,
    *_TS_PATTERNS,
    *_PY_PATTERNS,
]
