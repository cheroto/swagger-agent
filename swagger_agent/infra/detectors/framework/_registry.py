"""Framework detector registry.

Chains all language-specific detectors and returns the first match.
To add a new language: create a new module with a FrameworkDetector
subclass and add it to _DETECTORS below.
"""

from __future__ import annotations

import logging

from swagger_agent.infra.detectors.framework._base import FrameworkDetector
from swagger_agent.infra.detectors.framework.javascript import JavaScriptDetector
from swagger_agent.infra.detectors.framework.python import PythonDetector
from swagger_agent.infra.detectors.framework.java import JavaDetector
from swagger_agent.infra.detectors.framework.go import GoDetector
from swagger_agent.infra.detectors.framework.ruby import RubyDetector
from swagger_agent.infra.detectors.framework.rust import RustDetector
from swagger_agent.infra.detectors.framework.php import PHPDetector
from swagger_agent.infra.detectors.framework.csharp import CSharpDetector

logger = logging.getLogger("swagger_agent.prescan")

# Order matters: first match wins. Most common ecosystems first.
_DETECTORS: list[FrameworkDetector] = [
    JavaScriptDetector(),
    PythonDetector(),
    JavaDetector(),
    GoDetector(),
    RubyDetector(),
    RustDetector(),
    PHPDetector(),
    CSharpDetector(),
]


def detect_framework(target_dir: str) -> tuple[str | None, str | None, list[str]]:
    """Detect framework and language from config files.

    Returns (framework, language, notes).
    First match wins — if multiple config files exist (monorepo), a warning
    is emitted listing all detected frameworks.
    """
    all_notes: list[str] = []
    hits: list[tuple[str, str]] = []  # (framework, language)
    lang_only: str | None = None  # language detected without framework

    for detector in _DETECTORS:
        fw, lang, notes = detector.detect(target_dir)
        all_notes.extend(notes)
        if fw is not None:
            hits.append((fw, lang or "unknown"))
        elif lang is not None and lang_only is None:
            lang_only = lang

    if not hits:
        # No framework found, but maybe we identified a language
        if lang_only:
            return None, lang_only, all_notes
        return None, None, all_notes or ["No recognized config files found"]

    if len(hits) > 1:
        others = ", ".join(f"{fw}/{lang}" for fw, lang in hits[1:])
        logger.warning(
            "Multiple frameworks detected: using %s/%s (also found: %s)",
            hits[0][0], hits[0][1], others,
        )
        all_notes.append(
            f"Warning: multiple frameworks detected ({others}) — using {hits[0][0]}/{hits[0][1]}"
        )

    return hits[0][0], hits[0][1], all_notes
