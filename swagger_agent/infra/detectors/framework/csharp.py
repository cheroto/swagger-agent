"""C# / ASP.NET framework detection from .csproj files."""

from __future__ import annotations

import os

from swagger_agent.infra.detectors._utils import SKIP_DIRS, read_file_safe
from swagger_agent.infra.detectors.framework._base import FrameworkDetector

_CSHARP_FRAMEWORKS: list[tuple[str, str]] = [
    ("Microsoft.AspNetCore", "aspnetcore"),
    ("Microsoft.NET.Sdk.Web", "aspnetcore"),
]


class CSharpDetector(FrameworkDetector):
    def detect(self, target_dir: str) -> tuple[str | None, str | None, list[str]]:
        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".") and d not in SKIP_DIRS
                and d not in ("bin", "obj")
            ]
            for fname in files:
                if fname.endswith(".csproj"):
                    content = read_file_safe(os.path.join(root, fname))
                    for dep_name, fw in _CSHARP_FRAMEWORKS:
                        if dep_name in content:
                            rel = os.path.relpath(os.path.join(root, fname), target_dir)
                            return fw, "csharp", [f"Found {dep_name} in {rel}"]
        return None, None, []
