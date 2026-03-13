"""Python framework detection from pip/poetry/pipenv config files."""

from __future__ import annotations

import os
import re

from swagger_agent.infra.detectors._utils import read_file_safe
from swagger_agent.infra.detectors.framework._base import FrameworkDetector

_PY_FRAMEWORKS: list[tuple[str, str]] = [
    ("fastapi", "fastapi"),
    ("flask", "flask"),
    ("django", "django"),
    ("starlette", "starlette"),
    ("tornado", "tornado"),
    ("falcon", "falcon"),
    ("sanic", "sanic"),
    ("bottle", "bottle"),
]


class PythonDetector(FrameworkDetector):
    def detect(self, target_dir: str) -> tuple[str | None, str | None, list[str]]:
        candidates = [
            "pyproject.toml",
            "requirements.txt",
            "setup.py",
            "setup.cfg",
            "Pipfile",
        ]

        for cfg_file in candidates:
            path = os.path.join(target_dir, cfg_file)
            if not os.path.isfile(path):
                continue

            content = read_file_safe(path).lower()
            for dep_name, fw in _PY_FRAMEWORKS:
                if re.search(rf"\b{re.escape(dep_name)}\b", content):
                    return fw, "python", [f"Found {dep_name} in {cfg_file}"]

        return None, None, []
