async def test_cost_opt_in(client):
    response = await client.patch("/v1/charging-sessions/3/cost", json={"unit_price": "1.25"})
    assert response.status_code == 403


async def test_fill_cost_and_preserve_existing(client, app, db, database_dsn):
    from urllib.parse import urlsplit

    role = urlsplit(database_dsn).username
    await db.execute(f'GRANT UPDATE(cost) ON charging_processes TO "{role}"')
    app.state.settings.charging_cost_writes_enabled = True
    await db.execute(
        "UPDATE charging_processes SET cost=NULL, charge_energy_added=20, charge_energy_used=22 "
        "WHERE id=1"
    )
    url = "/v1/charging-sessions/1/cost"
    response = await client.patch(url, json={"unit_price": "1.25"})
    assert response.status_code == 200
    assert response.json()["cost"] == "27.50"
    assert (await client.get("/v1/charging-sessions/1")).json()["cost"] == 27.5
    assert (await client.patch(url, json={"unit_price": "2"})).status_code == 409
    assert (await client.patch(url, json={"unit_price": "-1"})).status_code in (400, 422)
    assert (
        await client.patch(url, json={"unit_price": "1"}, headers={"Authorization": ""})
    ).status_code == 401


async def test_concurrent_cost_and_permissions(client, app, db, database_dsn):
    import asyncio
    from urllib.parse import urlsplit

    app.state.settings.charging_cost_writes_enabled = True
    url = "/v1/charging-sessions/1/cost"
    assert (await client.patch(url, json={"unit_price": "1"})).status_code == 503
    role = urlsplit(database_dsn).username
    await db.execute(f'GRANT UPDATE(cost) ON charging_processes TO "{role}"')
    await db.execute("UPDATE charging_processes SET cost=NULL WHERE id=1")
    responses = await asyncio.gather(
        *(client.patch(url, json={"unit_price": "1"}) for _ in range(2))
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert (
        await client.patch("/v1/charging-sessions/8/cost", json={"unit_price": "0"})
    ).status_code == 409
    assert (
        await client.patch("/v1/charging-sessions/999/cost", json={"unit_price": "0"})
    ).status_code == 404


def test_price_validation():
    import pytest
    from pydantic import ValidationError

    from mileaf.charging_cost import ChargingCostInput

    assert str(ChargingCostInput(unit_price="0").unit_price) == "0"
    for value in ["-1", "NaN", "Infinity", "1000000", "0.0000001"]:
        with pytest.raises(ValidationError):
            ChargingCostInput(unit_price=value)
