"""Authentication contracts through HTTP and the persisted state boundary."""

import asyncio
import base64
import json
from datetime import timedelta

import httpx
import pytest

from mileaf.app import create_app
from mileaf.config import Settings
from mileaf.dependencies import get_now
from tests.conftest import FIXED_NOW


@pytest.fixture
async def auth_client(tmp_path):
    app = create_app(
        Settings(
            database_dsn="postgresql://reader@localhost/test", state_file=tmp_path / "state.json"
        )
    )
    app.dependency_overrides[get_now] = lambda: FIXED_NOW

    @app.get("/v1/protected")
    def protected():
        return {"status": "ok"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, app


async def test_setup_login_and_bearer_access(auth_client, tmp_path):
    client, _ = auth_client
    password = "a long test password"
    assert (await client.post("/v1/auth/setup", json={"password": password})).status_code == 200
    response = await client.post("/v1/auth/login", json={"password": password})
    assert response.status_code == 200
    token = response.json()["token"]
    assert response.json()["expires_at"] == "2026-04-14T12:00:00Z"
    assert len(token) == 43
    assert (
        await client.get("/v1/protected", headers={"Authorization": f"Bearer {token}"})
    ).json() == {"status": "ok"}
    state_file = tmp_path / "state.json"
    assert state_file.stat().st_mode & 0o777 == 0o600
    assert password not in state_file.read_text()
    assert token not in state_file.read_text()
    assert "$argon2id$" in state_file.read_text()
    assert len(base64.urlsafe_b64decode(token + "=")) == 32


async def test_setup_validation_and_only_one_concurrent_winner(auth_client):
    client, _ = auth_client
    assert (await client.post("/v1/auth/setup", json={"password": "123456789"})).status_code == 400
    results = await asyncio.gather(
        *[
            client.post("/v1/auth/setup", json={"password": password})
            for password in ("first password", "second password")
        ]
    )
    assert sorted(response.status_code for response in results) == [200, 409]
    loser = next(response for response in results if response.status_code == 409)
    assert loser.json()["error"]["code"] == "already_initialized"
    winner = "first password" if results[0].status_code == 200 else "second password"
    assert (await client.post("/v1/auth/login", json={"password": winner})).status_code == 200


async def test_missing_malformed_wrong_and_expired_tokens_share_error(auth_client):
    client, app = auth_client
    await client.post("/v1/auth/setup", json={"password": "a long password"})
    login = await client.post("/v1/auth/login", json={"password": "a long password"})
    token = login.json()["token"]
    expected = {"error": {"code": "unauthorized", "message": "Invalid or expired credentials"}}
    for authorization in ("", "Basic abc", "Bearer", "Bearer unknown", "Bearer a b"):
        response = await client.get("/v1/protected", headers={"Authorization": authorization})
        assert response.status_code == 401
        assert response.json() == expected
    app.dependency_overrides[get_now] = lambda: FIXED_NOW + timedelta(days=30)
    expired = await client.get("/v1/protected", headers={"Authorization": f"Bearer {token}"})
    assert expired.status_code == 401
    assert expired.json() == expected


async def test_login_rate_limit_uses_failure_window_and_peer_ip(auth_client):
    client, app = auth_client
    await client.post("/v1/auth/setup", json={"password": "a long password"})
    for _ in range(5):
        response = await client.post("/v1/auth/login", json={"password": "wrong"})
        assert response.status_code == 401
    response = await client.post("/v1/auth/login", json={"password": "a long password"})
    assert response.status_code == 429
    # Arbitrary forwarded headers must not let a caller escape its IP's limit.
    response = await client.post(
        "/v1/auth/login", json={"password": "wrong"}, headers={"X-Forwarded-For": "192.0.2.1"}
    )
    assert response.status_code == 429
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.2", 123)), base_url="http://test"
    ) as other:
        assert (
            await other.post("/v1/auth/login", json={"password": "a long password"})
        ).status_code == 200
    app.dependency_overrides[get_now] = lambda: FIXED_NOW + timedelta(minutes=1)
    assert (
        await client.post("/v1/auth/login", json={"password": "a long password"})
    ).status_code == 200


async def test_token_cap_and_persistence_across_app_instances(auth_client, tmp_path):
    client, _ = auth_client
    await client.post("/v1/auth/setup", json={"password": "a long password"})
    tokens = []
    for _ in range(11):
        response = await client.post("/v1/auth/login", json={"password": "a long password"})
        assert response.status_code == 200
        tokens.append(response.json()["token"])
    assert len(json.loads((tmp_path / "state.json").read_text())["tokens"]) == 10
    assert (
        await client.get("/v1/protected", headers={"Authorization": f"Bearer {tokens[0]}"})
    ).status_code == 401
    app = create_app(
        Settings(
            database_dsn="postgresql://reader@localhost/test", state_file=tmp_path / "state.json"
        )
    )
    app.dependency_overrides[get_now] = lambda: FIXED_NOW

    @app.get("/v1/protected")
    def protected():
        return {"status": "ok"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as restarted:
        for token in tokens[1:]:
            response = await restarted.get(
                "/v1/protected", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 200
        assert (
            await restarted.post("/v1/auth/setup", json={"password": "a long password"})
        ).status_code == 409


async def test_openapi_exposes_bearer_and_allows_anonymous_auth_and_health(auth_client):
    client, _ = auth_client
    schema = (await client.get("/openapi.json")).json()
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert schema["paths"]["/v1/protected"]["get"]["security"] == [{"HTTPBearer": []}]
    for path, method in (
        ("/v1/health", "get"),
        ("/v1/auth/setup", "post"),
        ("/v1/auth/login", "post"),
    ):
        assert {} in schema["paths"][path][method]["security"]


async def test_separate_app_instances_do_not_overwrite_setup_or_issued_tokens(
    auth_client, tmp_path
):
    first, _ = auth_client
    other_app = create_app(
        Settings(
            database_dsn="postgresql://reader@localhost/test", state_file=tmp_path / "state.json"
        )
    )
    other_app.dependency_overrides[get_now] = lambda: FIXED_NOW

    @other_app.get("/v1/protected")
    def protected():
        return {"status": "ok"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=other_app), base_url="http://test"
    ) as second:
        setup = await asyncio.gather(
            *[
                client.post("/v1/auth/setup", json={"password": "a long password"})
                for client in (first, second)
            ]
        )
        assert sorted(response.status_code for response in setup) == [200, 409]
        logins = await asyncio.gather(
            *[
                client.post("/v1/auth/login", json={"password": "a long password"})
                for client in (first, second)
            ]
        )
        for response in logins:
            assert response.status_code == 200
            token = response.json()["token"]
            assert (
                await first.get("/v1/protected", headers={"Authorization": f"Bearer {token}"})
            ).status_code == 200
            assert (
                await second.get("/v1/protected", headers={"Authorization": f"Bearer {token}"})
            ).status_code == 200
    assert sorted(path.name for path in tmp_path.iterdir()) == ["state.json", "state.json.lock"]
