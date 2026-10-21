from __future__ import annotations

import asyncio
from typing import Any

import httpx


class MileafApiError(Exception):
    """Raised when Mileaf Cloud rejects an agent request."""


class AgentAuthError(MileafApiError):
    """Raised when the agent token is missing, revoked, or invalid."""


class MileafClient:
    def __init__(self, base_url: str, timeout_seconds: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={"User-Agent": "mileaf-agent/0.1"},
        )
        self._agent_token: str | None = None

    async def __aenter__(self) -> MileafClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.aclose()

    def set_agent_token(self, token: str | None) -> None:
        self._agent_token = token

    async def pair(
        self,
        *,
        code: str,
        name: str,
        source_version: str | None,
        schema_version: str | None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/agent/pair",
            json={
                "code": code,
                "name": name,
                "source_version": source_version,
                "schema_version": schema_version,
            },
        )

    async def sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/agent/sync", json=payload, authenticated=True)

    async def heartbeat(
        self, *, source_version: str | None, schema_version: str | None
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/agent/heartbeat",
            json={"source_version": source_version, "schema_version": schema_version},
            authenticated=True,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any],
        authenticated: bool = False,
    ) -> dict[str, Any]:
        headers = {}
        if authenticated:
            if not self._agent_token:
                raise AgentAuthError("agent token is not configured")
            headers["Authorization"] = f"Bearer {self._agent_token}"

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._client.request(method, path, json=json, headers=headers)
            except httpx.TransportError as exc:
                last_error = exc
                await asyncio.sleep(2**attempt)
                continue

            if response.status_code == 401:
                raise AgentAuthError("agent token was rejected")
            if response.status_code >= 500:
                last_error = MileafApiError(f"server error {response.status_code}")
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code >= 400:
                raise MileafApiError(
                    f"request failed with {response.status_code}: {response.text[:500]}"
                )
            return response.json()

        raise MileafApiError(f"request failed after retries: {last_error}")
