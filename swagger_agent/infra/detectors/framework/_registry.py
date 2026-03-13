"""Framework detector registry.

Chains all language-specific detectors and returns the first match.
To add a new language: create a new module with a FrameworkDetector
subclass and add it to _DETECTORS below.
"""

from __future__ import annotations

from swagger_agent.infra.detectors.framework._base import FrameworkDetector
from swagger_agent.infra.detectors.framework.javascript import JavaScriptDetector
from swagger_agent.infra.detectors.framework.python import PythonDetector
from swagger_agent.infra.detectors.framework.java import JavaDetector
from swagger_agent.infra.detectors.framework.go import GoDetector
from swagger_agent.infra.detectors.framework.ruby import RubyDetector
from swagger_agent.infra.detectors.framework.rust import RustDetector
from swagger_agent.infra.detectors.framework.php import PHPDetector
from swagger_agent.infra.detectors.framework.csharp import CSharpDetector

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
    """
    all_notes: list[str] = []
    for detector in _DETECTORS:
        fw, lang, notes = detector.detect(target_dir)
        all_notes.extend(notes)
        if fw is not None:
            return fw, lang, all_notes

    return None, None, all_notes or ["No recognized config files found"]
