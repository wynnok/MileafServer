from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mileaf.api import agent, auth, health, vehicles
from mileaf.config import Settings, get_settings
from mileaf.db import Base, build_engine, build_sessionmaker


def create_app(
    settings: Settings | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.sessionmaker is None:
            engine = build_engine(app_settings.database_url)
            app.state.engine = engine
            app.state.sessionmaker = build_sessionmaker(engine)
            if app_settings.auto_create_tables:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
        yield
        runtime_engine = getattr(app.state, "engine", None)
        if runtime_engine is not None:
            await runtime_engine.dispose()

    app = FastAPI(
        title="Mileaf API",
        version="0.1.0",
        description="Multi-tenant Tesla data API for the Mileaf mobile app.",
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.sessionmaker = sessionmaker

    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origin_list,
        allow_credentials="*" not in app_settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(vehicles.router)
    app.include_router(agent.router)
    return app


app = create_app()
