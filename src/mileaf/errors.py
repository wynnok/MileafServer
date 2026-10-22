"""Stable public errors; upstream exception text is never exposed."""

from enum import StrEnum


class ErrorCode(StrEnum):
    DB_UNAVAILABLE = "db_unavailable"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    INTERNAL_ERROR = "internal_error"
    UNAUTHORIZED = "unauthorized"
    ALREADY_INITIALIZED = "already_initialized"
    RATE_LIMITED = "rate_limited"


class APIError(Exception):
    def __init__(self, status_code: int, code: ErrorCode, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
