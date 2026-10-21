from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mileaf.config import Settings
from mileaf.db import get_db
from mileaf.models import DataSource, Membership, Tenant, User
from mileaf.security import InvalidTokenError, decode_token, hash_agent_token

bearer_scheme = HTTPBearer(auto_error=False)


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


DbSession = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


@dataclass(frozen=True)
class TenantContext:
    user: User
    tenant: Tenant
    role: str


async def get_current_user(
    credentials: Credentials,
    session: DbSession,
    settings: SettingsDep,
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        subject = decode_token(credentials.credentials, "access", settings)
        user_id = uuid.UUID(subject)
    except (InvalidTokenError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_tenant_context(
    user: CurrentUser,
    session: DbSession,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
) -> TenantContext:
    stmt = (
        select(Membership)
        .options(selectinload(Membership.tenant))
        .where(Membership.user_id == user.id)
    )

    if x_tenant_id:
        try:
            tenant_id = uuid.UUID(x_tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid X-Tenant-ID header") from exc
        stmt = stmt.where(Membership.tenant_id == tenant_id)
    else:
        stmt = stmt.order_by(Membership.created_at).limit(1)

    membership = await session.scalar(stmt)
    if membership is None:
        raise HTTPException(status_code=403, detail="no access to tenant")
    return TenantContext(user=user, tenant=membership.tenant, role=membership.role)


TenantCtx = Annotated[TenantContext, Depends(get_tenant_context)]


async def get_current_data_source(
    credentials: Credentials,
    session: DbSession,
    settings: SettingsDep,
) -> DataSource:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing agent token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_hash = hash_agent_token(credentials.credentials, settings.agent_token_pepper)
    source = await session.scalar(select(DataSource).where(DataSource.token_hash == token_hash))
    if source is None or source.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid agent token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return source


CurrentDataSource = Annotated[DataSource, Depends(get_current_data_source)]
