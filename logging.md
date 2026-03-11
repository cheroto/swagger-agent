# Logging & Narration

Two layers: **Narrator** (user-facing, Rich) and **standard logging** (debug, for developers). They serve different audiences and never substitute for each other.

## Narrator

A single `Narrator` instance is created at startup and passed to infrastructure modules. Agents never call the Narrator directly — infrastructure wraps agent invocations and narrates before/after.

### Design principles

1. **Semantic, not generic.** Methods are named after pipeline events (`phase_change`, `route_progress`, `schema_extracted`), not log levels. This keeps call sites readable and output consistent.
2. **Infrastructure narrates, agents don't.** The Narrator lives in the infrastructure layer. When infrastructure calls an agent, it calls `narrator.agent_start()` before and `narrator.agent_done()` after. Agents produce artifacts; infrastructure tells the story.
3. **Progressive disclosure.** Default output shows phase transitions, progress, and results. `--verbose` adds ref resolution details and debug-level logs.
4. **Tables for structured data.** Discovery results, completeness checklists, and validation errors use Rich tables — scannable at a glance.

### API

```python
class Narrator:
    def __init__(self, verbose: bool = False):
        self.console = Console()
        self.verbose = verbose
```

#### Phase transitions

```python
def phase_change(self, phase: str, detail: str = ""):
```

Prints a horizontal rule with a phase label and optional detail. Called when `StateSummary.phase` changes.

Terminal output:
```
──────────────── 🔍 Scout ─────────────────
```

#### Agent lifecycle

```python
def agent_start(self, agent: str, task: str):
def agent_done(self, agent: str, summary: str):
def agent_error(self, agent: str, error: str):
```

Called by infrastructure around every agent invocation. `summary` is a one-line description of what the agent produced (e.g. "Found FastAPI project with 5 route files", "Extracted 4 endpoints").

Terminal output:
```
  ▶ Scout → Analyzing project structure
  ✓ Scout — Found FastAPI project with 5 route files
```

On error:
```
  ✗ Route Extractor — Failed to parse app/api/routes/users.py: context window exceeded
```

#### Discovery results

```python
def discovery_summary(self, manifest: DiscoveryManifest):
```

Renders a compact table after the Scout completes. Shows framework, file counts, security schemes, and servers.

Terminal output:
```
  ┌─────────────────────────────┐
  │ Discovery Results           │
  ├──────────────┬──────────────┤
  │ Framework    │ fastapi      │
  │ Route files  │ 5            │
  │ Model files  │ 8            │
  │ Security     │ BearerAuth   │
  │ Servers      │ localhost:8000│
  └──────────────┴──────────────┘
```

#### Route extraction progress

```python
def route_progress(self, extracted: int, total: int, file: str):
```

One line per extracted route file. Shows progress counter and filename.

Terminal output:
```
  [3/5] Extracted app/api/routes/comments.py
```

#### Schema resolution

```python
def schema_resolving(self, ref_name: str, file: str):
def schema_extracted(self, file: str, count: int):
def schema_unresolvable(self, ref_name: str, reason: str):
```

`schema_resolving` only prints in verbose mode. `schema_extracted` and `schema_unresolvable` always print.

Terminal output:
```
  📐 app/models/user.py → 2 schema(s)
  ⚠ Unresolvable: PaginatedResponse — external package
```

Verbose:
```
  Resolving UserResponse → app/schemas/user.py
  Resolving ArticleResponse → app/schemas/article.py
  📐 app/schemas/user.py → 2 schema(s)
```

#### Completeness report

```python
def completeness_report(self, checklist: CompletenessChecklist):
```

Renders the completeness checklist as a table with pass/fail icons. Called after the final assembly.

Terminal output:
```
  ┌─ Completeness ────────────┐
  │ Has Endpoints          ✓  │
  │ Has Security Schemes   ✓  │
  │ Endpoints Have Auth    ✓  │
  │ Has Error Responses    ✓  │
  │ Has Request Bodies     ✓  │
  │ Has Schemas            ✓  │
  │ No Unresolved Refs     ✗  │
  │ Has Servers            ✓  │
  │ Route Coverage        100% │
  └───────────────────────────┘
```

#### Validation errors

```python
def validation_errors(self, errors: list[str]):
```

Shows up to 5 errors with a count of remaining. Empty list prints a success message.

Terminal output (errors):
```
  3 validation error(s):
    • Missing $ref: #/components/schemas/CommentResponse
    • Duplicate operationId: getArticle
    • Path /api/articles/{slug}/comments has no 401 response
```

Terminal output (clean):
```
  Spec is valid.
```

#### Final output

```python
def spec_written(self, path: str, endpoint_count: int):
```

Green bordered panel with output path and endpoint count.

Terminal output:
```
  ╭──────────────────────────────────╮
  │ OpenAPI spec written to out.yaml │
  │ 23 endpoints documented          │
  ╰──────────────────────────────────╯
```

## Standard Logging

For developer diagnostics. Not user-facing.

```python
import logging

logging.basicConfig(
    level=logging.DEBUG if verbose else logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
```

Each infrastructure module gets its own logger:

```python
logger = logging.getLogger("swagger_agent.ref_resolver")
logger = logging.getLogger("swagger_agent.assembler")
logger = logging.getLogger("swagger_agent.validator")
```

Standard logging handles:
- Artifact store reads/writes with paths
- Ref resolution steps and fallbacks
- Assembly decisions (which refs mapped where)
- Validation rule details
- LLM call metadata (token counts, latency)

The Narrator and standard logging are independent. The Narrator prints to `rich.Console` (stderr by default). Standard logging goes to stderr via the logging module. They can coexist without interference.

## Integration Pattern

Infrastructure modules receive the Narrator at construction:

```python
class RefResolver:
    def __init__(self, store: ArtifactStore, narrator: Narrator):
        self.store = store
        self.narrator = narrator
        self.logger = logging.getLogger("swagger_agent.ref_resolver")

    def resolve_all(self):
        for ref in self.pending_refs():
            self.logger.debug("Resolving %s via %s", ref.ref_hint, ref.resolution)
            self.narrator.schema_resolving(ref.ref_hint, resolved_file)
            # ...
            self.narrator.schema_extracted(file, count)
```

The orchestrator loop narrates phase changes:

```python
while state.phase != Phase.DONE:
    narrator.phase_change(state.phase.value)

    if state.phase == Phase.INIT:
        narrator.agent_start("Scout", "Analyzing project structure")
        manifest = run_scout(target_dir)
        narrator.agent_done("Scout", f"Found {manifest.framework} with {len(manifest.route_files)} route files")
        narrator.discovery_summary(manifest)

    elif state.phase == Phase.EXTRACTING_ROUTES:
        for i, file in enumerate(state.routes.pending):
            narrator.agent_start("Route Extractor", file)
            descriptor = run_route_extractor(file, context)
            narrator.agent_done("Route Extractor", f"{len(descriptor.endpoints)} endpoints")
            narrator.route_progress(i + 1, state.routes.total, file)

    # ... schema phase is infrastructure-driven, narrates internally

narrator.completeness_report(state.completeness)
narrator.validation_errors(validation_result.errors)
narrator.spec_written(output_path, endpoint_count)
```

## CLI Flags

| Flag | Effect |
|------|--------|
| `--verbose` / `-v` | Enables `schema_resolving` output + sets standard logging to DEBUG |
| `--quiet` / `-q` | Suppresses all Narrator output. Standard logging still active at WARNING. |
| `--no-color` | Passes `no_color=True` to `rich.Console`. Also respects `NO_COLOR` env var. |
