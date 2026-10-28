"""Range route authentication before accessing a database."""

import httpx

from mileaf.app import create_app
from mileaf.config import Settings


async def test_range_observations_require_bearer_before_database(tmp_path):
    app = create_app(
        Settings(
            database_dsn="postgresql://reader@127.0.0.1:1/fixture", state_file=tmp_path / "state"
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/vehicles/1/range-observations", params={"months": 6})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["x-request-id"]
