"""Typed, user-safe parsing errors."""

from __future__ import annotations

from typing import Any


class DocumentParseError(Exception):
    """A known parsing failure that must not publish a half-ready document."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "failed",
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }

