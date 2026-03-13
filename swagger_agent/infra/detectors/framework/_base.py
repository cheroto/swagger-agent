"""Base class for framework detectors."""

from __future__ import annotations

from abc import ABC, abstractmethod


class FrameworkDetector(ABC):
    """Detects a web framework from a project's config/manifest files.

    Subclass this and implement `detect()` for each language ecosystem.
    """

    @abstractmethod
    def detect(self, target_dir: str) -> tuple[str | None, str | None, list[str]]:
        """Attempt to detect a framework in target_dir.

        Returns:
            (framework, language, notes) — framework/language are None if
            this detector found nothing.
        """
        ...
