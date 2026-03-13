"""Java/Kotlin framework detection from Maven/Gradle build files."""

from __future__ import annotations

import os

from swagger_agent.infra.detectors._utils import read_file_safe
from swagger_agent.infra.detectors.framework._base import FrameworkDetector

_JAVA_FRAMEWORKS: list[tuple[str, str]] = [
    ("spring-boot-starter-web", "spring"),
    ("spring-boot-starter-webflux", "spring"),
    ("spring-webmvc", "spring"),
    ("jersey-server", "jersey"),
    ("resteasy", "resteasy"),
    ("quarkus-resteasy", "quarkus"),
    ("micronaut-http-server", "micronaut"),
]


class JavaDetector(FrameworkDetector):
    def detect(self, target_dir: str) -> tuple[str | None, str | None, list[str]]:
        for cfg_file in ("pom.xml", "build.gradle", "build.gradle.kts"):
            path = os.path.join(target_dir, cfg_file)
            if not os.path.isfile(path):
                continue

            content = read_file_safe(path)
            lang = "kotlin" if cfg_file.endswith(".kts") else "java"

            for dep_name, fw in _JAVA_FRAMEWORKS:
                if dep_name in content:
                    return fw, lang, [f"Found {dep_name} in {cfg_file}"]

        return None, None, []
