from fastapi import APIRouter
from sqlalchemy import text

from mileaf.deps import DbSession

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(session: DbSession) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    return {"status": "ready"}
