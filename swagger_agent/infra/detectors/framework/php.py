"""PHP framework detection from composer.json."""

from __future__ import annotations

import json
import os

from swagger_agent.infra.detectors._utils import read_file_safe
from swagger_agent.infra.detectors.framework._base import FrameworkDetector

_PHP_FRAMEWORKS: list[tuple[str, str]] = [
    ("laravel/framework", "laravel"),
    ("slim/slim", "slim"),
    ("symfony/framework-bundle", "symfony"),
    ("laravel/lumen-framework", "lumen"),
]


class PHPDetector(FrameworkDetector):
    def detect(self, target_dir: str) -> tuple[str | None, str | None, list[str]]:
        path = os.path.join(target_dir, "composer.json")
        if not os.path.isfile(path):
            return None, None, []

        content = read_file_safe(path)
        try:
            pkg = json.loads(content)
        except json.JSONDecodeError:
            return None, None, ["Found composer.json but failed to parse"]

        require = pkg.get("require", {})
        for dep_name, fw in _PHP_FRAMEWORKS:
            if dep_name in require:
                return fw, "php", [f"Found {dep_name} in composer.json"]

        return None, None, []
