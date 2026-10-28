"""Statistics capability contract through the public OpenAPI HTTP boundary."""

import httpx

from mileaf.app import create_app
from mileaf.config import Settings


async def test_statistics_capability_has_an_independent_version(tmp_path):
    app = create_app(
        Settings(
            database_dsn="postgresql://fixture@127.0.0.1:1/unused", state_file=tmp_path / "state"
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/openapi.json")
    assert response.status_code == 200
    schemas = response.json()["components"]["schemas"]
    assert "statistics_insights" in schemas["Meta"]["properties"]
    capability = schemas["StatisticsCapabilities"]["properties"]
    assert capability["contract_version"]["const"] == "statistics-insights-v1"
    assert capability["minimum_server_version"]["const"] == "0.3.0"
