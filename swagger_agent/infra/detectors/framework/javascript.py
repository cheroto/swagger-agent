"""JavaScript/TypeScript framework detection from package.json."""

from __future__ import annotations

import json
import os

from swagger_agent.infra.detectors._utils import read_file_safe
from swagger_agent.infra.detectors.framework._base import FrameworkDetector

# (dependency_name, canonical_framework, canonical_language)
# (dependency_name, canonical_framework, always_typescript)
_JS_FRAMEWORKS: list[tuple[str, str, bool]] = [
    ("express", "express", False),
    ("@nestjs/core", "nestjs", True),  # NestJS is always TypeScript
    ("fastify", "fastify", False),
    ("koa", "koa", False),
    ("@hapi/hapi", "hapi", False),
    ("hapi", "hapi", False),
    ("hono", "hono", False),
    ("@hono/node-server", "hono", False),
    ("restify", "restify", False),
    ("@adonisjs/core", "adonis", False),
]


class JavaScriptDetector(FrameworkDetector):
    def detect(self, target_dir: str) -> tuple[str | None, str | None, list[str]]:
        path = os.path.join(target_dir, "package.json")
        if not os.path.isfile(path):
            return None, None, []

        content = read_file_safe(path)
        if not content:
            return None, None, []

        try:
            pkg = json.loads(content)
        except json.JSONDecodeError:
            return None, None, ["Found package.json but failed to parse"]

        deps: dict = {}
        deps.update(pkg.get("dependencies", {}))
        deps.update(pkg.get("devDependencies", {}))

        has_tsconfig = os.path.isfile(os.path.join(target_dir, "tsconfig.json"))

        for dep_name, fw, always_ts in _JS_FRAMEWORKS:
            if dep_name in deps:
                lang = "typescript" if (always_ts or has_tsconfig) else "javascript"
                return fw, lang, [f"Found {dep_name} in package.json dependencies"]

        return None, None, ["Found package.json but no recognized web framework"]
