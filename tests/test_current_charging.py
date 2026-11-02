"""Current state must not resurrect historical incomplete charging sessions."""

import pytest


@pytest.mark.parametrize("path", ["/v1/vehicles", "/v1/vehicles/1/summary"])
@pytest.mark.parametrize("evidence", ["completed_charge", "drive", "offline", "asleep"])
async def test_superseded_charge_is_not_current(db, client, path, evidence):
    if evidence == "completed_charge":
        await db.execute(
            "UPDATE charging_processes SET start_date='2026-03-15 11:10', "
            "end_date='2026-03-15 11:30' WHERE id=9"
        )
    elif evidence == "drive":
        await db.execute("UPDATE drives SET start_date='2026-03-15 11:10' WHERE id=7")
    else:
        await db.execute(
            "UPDATE states SET state=$1, start_date='2026-03-15 11:10' WHERE id=4",
            evidence,
        )
    response = await client.get(path)
    assert response.status_code == 200
    body = response.json()
    vehicle = body[0] if isinstance(body, list) else body
    expected = evidence if evidence in {"offline", "asleep"} else "online"
    assert vehicle["state"]["value"] == expected
    assert vehicle["state"]["since"] == (
        "2026-03-15T11:10:00Z" if expected != "online" else "2026-03-14T23:00:00Z"
    )
    if path.endswith("summary"):
        assert vehicle["charging_power_kw"] is None


@pytest.mark.parametrize("path", ["/v1/vehicles", "/v1/vehicles/1/summary"])
@pytest.mark.parametrize("scenario", ["new_charge", "online", "other_vehicle", "no_samples"])
async def test_current_charge_is_preserved(db, client, path, scenario):
    if scenario == "new_charge":
        await db.execute(
            "UPDATE charging_processes SET start_date='2026-03-15 11:30', end_date=NULL WHERE id=9"
        )
    elif scenario == "online":
        await db.execute("UPDATE states SET start_date='2026-03-15 11:10' WHERE id=4")
    elif scenario == "other_vehicle":
        await db.execute(
            "INSERT INTO states(id,car_id,state,start_date) "
            "VALUES (100,2,'offline','2026-03-15 11:10')"
        )
    else:
        await db.execute("DELETE FROM charges WHERE charging_process_id=8")
    response = await client.get(path)
    assert response.status_code == 200
    body = response.json()
    vehicle = body[0] if isinstance(body, list) else body
    assert vehicle["state"] == {
        "value": "charging",
        "since": "2026-03-15T11:30:00Z" if scenario == "new_charge" else "2026-03-15T11:00:00Z",
    }
    if path.endswith("summary") and scenario in {"new_charge", "no_samples"}:
        assert vehicle["charging_power_kw"] is None


@pytest.mark.parametrize("path", ["/v1/vehicles", "/v1/vehicles/1/summary"])
@pytest.mark.parametrize("state", ["online", "offline", "asleep"])
async def test_simultaneous_state_transition(db, client, path, state):
    # Seed drive #7 also starts at 11:00: equal drive/charge timestamps alone
    # cannot establish which event followed the other.
    await db.execute("UPDATE states SET state=$1,start_date='2026-03-15 11:00' WHERE id=4", state)
    response = await client.get(path)
    assert response.status_code == 200
    body = response.json()
    vehicle = body[0] if isinstance(body, list) else body
    assert vehicle["state"] == {
        "value": "charging" if state == "online" else state,
        "since": "2026-03-15T11:00:00Z",
    }
    if path.endswith("summary"):
        assert vehicle["charging_power_kw"] == (11 if state == "online" else None)
