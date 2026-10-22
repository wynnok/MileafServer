"""Application factory and the sole route registration list."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

from mileaf.config import Settings
from mileaf.database import create_pool
from mileaf.errors import APIError, ErrorCode
from mileaf.health import router as health_router

ROUTERS = [health_router]
logger = logging.getLogger("mileaf")


def error_response(status: int, code: ErrorCode, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def create_app(settings: Settings | None = None) -> FastAPI:
    configuration = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.pool = await create_pool(configuration)
        try:
            yield
        finally:
            await app.state.pool.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = configuration
    for router in ROUTERS:
        app.include_router(router)

    @app.exception_handler(APIError)
    async def api_error(request: Request, exc: APIError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, ErrorCode.INVALID_REQUEST, "Request validation failed")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.INVALID_REQUEST
        return error_response(
            exc.status_code,
            code,
            "Resource not found" if exc.status_code == 404 else "Request failed",
        )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid4().hex
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except (TimeoutError, asyncpg.QueryCanceledError):
            response = error_response(503, ErrorCode.UPSTREAM_TIMEOUT, "Database query timed out")
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
            response = error_response(503, ErrorCode.DB_UNAVAILABLE, "Database is unavailable")
        except Exception:
            response = error_response(500, ErrorCode.INTERNAL_ERROR, "Internal server error")
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_id=%s method=%s status=%s", request_id, request.method, response.status_code
        )
        return response

    return app
