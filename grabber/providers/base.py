"""Base provider interface for document grabbing.

To add support for a new system (e.g. Scribd, Issuu), create a new module in
this package that subclasses ``BaseProvider`` and register it in
``grabber/providers/__init__.py``.
"""

from __future__ import annotations

import abc
from pathlib import Path


class BaseProvider(abc.ABC):
    """Abstract base for all document-download providers."""

    @staticmethod
    @abc.abstractmethod
    def can_handle(url: str) -> bool:
        """Return True if this provider knows how to handle *url*."""

    @abc.abstractmethod
    def fetch(
        self,
        url: str,
        output: Path,
        *,
        email: str | None = None,
        headless: bool = True,
    ) -> Path:
        """Download the document at *url* and write a PDF to *output*.

        Parameters
        ----------
        url:
            The viewer URL (e.g. a DocSend link).
        output:
            Destination file path for the resulting PDF.
        email:
            Optional email to bypass an access gate.
        headless:
            Whether to run the browser in headless mode.

        Returns
        -------
        Path
            The path to the written PDF.
        """
