from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from mileaf.deps import CurrentDataSource, CurrentUser, DbSession, SettingsDep, TenantCtx
from mileaf.models import DataSource, PairingCode
from mileaf.schemas import (
    AgentHeartbeatIn,
    AgentPairIn,
    AgentPairingCodeOut,
    AgentPairOut,
    AgentSyncIn,
    AgentSyncOut,
    DataSourceOut,
)
from mileaf.security import (
    generate_agent_token,
    generate_pairing_code,
    hash_agent_token,
    hash_pairing_code,
)
from mileaf.services.ingest import IngestError, ingest_batch

router = APIRouter(prefix="/v1/agent", tags=["agent"])


@router.post("/pairing-codes", response_model=AgentPairingCodeOut)
async def create_pairing_code(
    ctx: TenantCtx, user: CurrentUser, session: DbSession, settings: SettingsDep
) -> AgentPairingCodeOut:
    code = generate_pairing_code()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.pairing_code_minutes)
    session.add(
        PairingCode(
            tenant_id=ctx.tenant.id,
            code_hash=hash_pairing_code(code, settings.agent_token_pepper),
            created_by_user_id=user.id,
            expires_at=expires_at,
        )
    )
    await session.commit()
    return AgentPairingCodeOut(code=code, expires_at=expires_at)


@router.post("/pair", response_model=AgentPairOut)
async def pair_agent(
    payload: AgentPairIn, session: DbSession, settings: SettingsDep
) -> AgentPairOut:
    code_hash = hash_pairing_code(payload.code, settings.agent_token_pepper)
    pairing = await session.scalar(
        select(PairingCode)
        .where(
            PairingCode.code_hash == code_hash,
            PairingCode.used_at.is_(None),
        )
        .with_for_update()
    )
    now = datetime.now(UTC)
    if pairing is None or pairing.expires_at < now:
        raise HTTPException(status_code=400, detail="pairing code is invalid or expired")

    agent_token = generate_agent_token()
    source = DataSource(
        tenant_id=pairing.tenant_id,
        name=payload.name,
        kind="teslamate_agent",
        token_hash=hash_agent_token(agent_token, settings.agent_token_pepper),
        source_version=payload.source_version,
        schema_version=payload.schema_version,
        last_seen_at=now,
    )
    session.add(source)
    pairing.used_at = now
    await session.commit()
    return AgentPairOut(
        agent_token=agent_token,
        tenant_id=source.tenant_id,
        data_source_id=source.id,
    )


@router.post("/sync", response_model=AgentSyncOut)
async def sync(
    payload: AgentSyncIn,
    source: CurrentDataSource,
    session: DbSession,
) -> AgentSyncOut:
    try:
        return await ingest_batch(session, source, payload)
    except IngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/heartbeat")
async def heartbeat(
    payload: AgentHeartbeatIn, source: CurrentDataSource, session: DbSession
) -> dict:
    source.last_seen_at = datetime.now(UTC)
    if payload.source_version:
        source.source_version = payload.source_version
    if payload.schema_version:
        source.schema_version = payload.schema_version
    await session.commit()
    return {"ok": True}


@router.get("/sources", response_model=list[DataSourceOut])
async def list_sources(ctx: TenantCtx, session: DbSession) -> list[DataSourceOut]:
    result = await session.scalars(
        select(DataSource)
        .where(DataSource.tenant_id == ctx.tenant.id)
        .order_by(DataSource.created_at.desc())
    )
    return [DataSourceOut.model_validate(source) for source in result]


@router.delete("/sources/{source_id}", status_code=204)
async def revoke_source(source_id: uuid.UUID, ctx: TenantCtx, session: DbSession) -> None:
    source = await session.scalar(
        select(DataSource).where(
            DataSource.id == source_id,
            DataSource.tenant_id == ctx.tenant.id,
        )
    )
    if source is None:
        raise HTTPException(status_code=404, detail="data source not found")
    source.status = "revoked"
    await session.commit()
