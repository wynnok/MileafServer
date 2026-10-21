from __future__ import annotations

import httpx

from tests.conftest import auth_headers, register


async def test_register_login_me_and_refresh(client: httpx.AsyncClient) -> None:
    auth = await register(client)

    assert auth["token_type"] == "bearer"
    assert auth["user"]["email"] == "owner@example.com"
    assert len(auth["tenants"]) == 1
    assert auth["tenants"][0]["role"] == "owner"

    me = await client.get("/v1/auth/me", headers=auth_headers(auth))
    assert me.status_code == 200
    assert me.json()["email"] == "owner@example.com"

    login = await client.post(
        "/v1/auth/login",
        json={"email": "OWNER@example.com", "password": "correct-horse"},
    )
    assert login.status_code == 200

    refreshed = await client.post("/v1/auth/refresh", json={"refresh_token": auth["refresh_token"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"]


async def test_register_rejects_duplicate_email(client: httpx.AsyncClient) -> None:
    await register(client)
    response = await client.post(
        "/v1/auth/register",
        json={"email": "owner@example.com", "password": "another-password"},
    )
    assert response.status_code == 409


async def test_login_rejects_wrong_password(client: httpx.AsyncClient) -> None:
    await register(client)
    response = await client.post(
        "/v1/auth/login",
        json={"email": "owner@example.com", "password": "wrong-password"},
    )
    assert response.status_code == 401


async def test_endpoints_require_access_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/vehicles")
    assert response.status_code == 401
