"""Ruby framework detection from Gemfile."""

from __future__ import annotations

import os
import re

from swagger_agent.infra.detectors._utils import read_file_safe
from swagger_agent.infra.detectors.framework._base import FrameworkDetector

_RUBY_FRAMEWORKS: list[tuple[str, str]] = [
    ("rails", "rails"),
    ("sinatra", "sinatra"),
    ("grape", "grape"),
    ("roda", "roda"),
]


class RubyDetector(FrameworkDetector):
    def detect(self, target_dir: str) -> tuple[str | None, str | None, list[str]]:
        path = os.path.join(target_dir, "Gemfile")
        if not os.path.isfile(path):
            return None, None, []

        content = read_file_safe(path)
        for dep_name, fw in _RUBY_FRAMEWORKS:
            if re.search(rf"""gem\s+['"]({re.escape(dep_name)})['"]""", content):
                return fw, "ruby", [f"Found {dep_name} in Gemfile"]

        return None, None, []
