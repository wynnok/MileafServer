from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from mileaf.deps import CurrentUser, DbSession, SettingsDep
from mileaf.models import Membership, Tenant, User
from mileaf.schemas import (
    AuthResponse,
    LoginIn,
    RefreshIn,
    RegisterIn,
    TenantMembershipOut,
    TokenPair,
    UserOut,
)
from mileaf.security import (
    InvalidTokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterIn, session: DbSession, settings: SettingsDep) -> AuthResponse:
    email = payload.email.lower()
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(status_code=409, detail="email already registered")

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
    )
    tenant = Tenant(name=payload.tenant_name or f"{payload.display_name or email} 的空间")
    session.add_all([user, tenant])
    await session.flush()
    session.add(Membership(user_id=user.id, tenant_id=tenant.id, role="owner"))

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="email already registered") from exc

    return await _auth_response(session, user, settings)


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginIn, session: DbSession, settings: SettingsDep) -> AuthResponse:
    user = await session.scalar(select(User).where(User.email == payload.email.lower()))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    return await _auth_response(session, user, settings)


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshIn, session: DbSession, settings: SettingsDep) -> TokenPair:
    try:
        subject = decode_token(payload.refresh_token, "refresh", settings)
        user_id = uuid.UUID(subject)
    except (InvalidTokenError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="invalid refresh token") from exc

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid refresh token")
    return _token_pair(user, settings)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/tenants", response_model=list[TenantMembershipOut])
async def my_tenants(user: CurrentUser, session: DbSession) -> list[TenantMembershipOut]:
    memberships = await _memberships(session, user.id)
    return [
        TenantMembershipOut(
            id=membership.tenant.id,
            name=membership.tenant.name,
            created_at=membership.tenant.created_at,
            role=membership.role,
        )
        for membership in memberships
    ]


async def _auth_response(session: DbSession, user: User, settings: SettingsDep) -> AuthResponse:
    memberships = await _memberships(session, user.id)
    tokens = _token_pair(user, settings)
    return AuthResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        user=UserOut.model_validate(user),
        tenants=[
            TenantMembershipOut(
                id=membership.tenant.id,
                name=membership.tenant.name,
                created_at=membership.tenant.created_at,
                role=membership.role,
            )
            for membership in memberships
        ],
    )


def _token_pair(user: User, settings: SettingsDep) -> TokenPair:
    subject = str(user.id)
    return TokenPair(
        access_token=create_access_token(subject, settings),
        refresh_token=create_refresh_token(subject, settings),
    )


async def _memberships(session: DbSession, user_id: uuid.UUID) -> list[Membership]:
    result = await session.scalars(
        select(Membership)
        .options(selectinload(Membership.tenant))
        .where(Membership.user_id == user_id)
        .order_by(Membership.created_at)
    )
    return list(result)
