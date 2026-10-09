"""Errors the review app can show to a person."""

from __future__ import annotations


class ServiceError(Exception):
    """A failure with a sentence that can go on screen.

    status is an HTTP status when the local page reports it. The message is
    the whole explanation. It must not include a key or a traceback.
    """

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
