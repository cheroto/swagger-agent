"""Rust framework detection from Cargo.toml."""

from __future__ import annotations

import os

from swagger_agent.infra.detectors._utils import read_file_safe
from swagger_agent.infra.detectors.framework._base import FrameworkDetector

_RUST_FRAMEWORKS: list[tuple[str, str]] = [
    ("actix-web", "actix-web"),
    ("axum", "axum"),
    ("rocket", "rocket"),
    ("warp", "warp"),
]


class RustDetector(FrameworkDetector):
    def detect(self, target_dir: str) -> tuple[str | None, str | None, list[str]]:
        path = os.path.join(target_dir, "Cargo.toml")
        if not os.path.isfile(path):
            return None, None, []

        content = read_file_safe(path)
        for dep_name, fw in _RUST_FRAMEWORKS:
            if dep_name in content:
                return fw, "rust", [f"Found {dep_name} in Cargo.toml"]

        return None, None, []
