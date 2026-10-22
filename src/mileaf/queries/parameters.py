"""Shared HTTP validation for paginated and sampled resources."""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import AfterValidator
from starlette.responses import Response

from mileaf.errors import APIError, ErrorCode
from mileaf.queries.pagination import bounded_integer, decode_cursor


def resource_identifier(value: int) -> int:
    """TeslaMate drive/session keys are int4; larger IDs cannot exist here."""
    if not 0 < value <= 2147483647:
        raise APIError(404, ErrorCode.NOT_FOUND, "Resource not found")
    return value


ResourceID = Annotated[int, AfterValidator(resource_identifier)]


class ResourceRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def validate(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise APIError(
                    400, ErrorCode.INVALID_REQUEST, "Invalid resource parameters"
                ) from None

        return validate


@dataclass(frozen=True)
class ListParameters:
    from_: datetime | None
    to: datetime | None
    cursor: str | None
    limit: int


def list_parameters(
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    cursor: str | None = None,
    limit: str = "20",
) -> ListParameters:
    try:
        start, end = (datetime.fromisoformat(value) if value else None for value in (from_, to))
        if (from_ is not None and start is None) or (to is not None and end is None):
            raise ValueError
        if any(value is not None and value.tzinfo is None for value in (start, end)):
            raise ValueError
        if start is not None and end is not None and start >= end:
            raise ValueError
    except (ValueError, OverflowError):
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid time range") from None
    size = bounded_integer(limit, default_name="limit", minimum=1, maximum=100)
    if cursor is not None:
        decode_cursor(cursor)
    return ListParameters(
        start.astimezone(UTC) if start else None, end.astimezone(UTC) if end else None, cursor, size
    )
