"""Go framework detection from go.mod."""

from __future__ import annotations

import os

from swagger_agent.infra.detectors._utils import read_file_safe
from swagger_agent.infra.detectors.framework._base import FrameworkDetector

_GO_FRAMEWORKS: list[tuple[str, str]] = [
    ("github.com/gin-gonic/gin", "gin"),
    ("github.com/labstack/echo", "echo"),
    ("github.com/gofiber/fiber", "fiber"),
    ("github.com/go-chi/chi", "chi"),
    ("github.com/gorilla/mux", "gorilla"),
]


class GoDetector(FrameworkDetector):
    def detect(self, target_dir: str) -> tuple[str | None, str | None, list[str]]:
        path = os.path.join(target_dir, "go.mod")
        if not os.path.isfile(path):
            return None, None, []

        content = read_file_safe(path)
        for dep_path, fw in _GO_FRAMEWORKS:
            if dep_path in content:
                return fw, "go", [f"Found {dep_path} in go.mod"]

        if "net/http" in content or "module " in content:
            return "net/http", "go", ["Found go.mod, assuming net/http stdlib"]

        return None, None, []
