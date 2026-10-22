"""Structured places shared by drive endpoints and charging sessions."""

import asyncio

import pytest

from mileaf.queries.locations import LocationRef, fetch_locations, location_sql


async def test_location_fallbacks_and_missing_coordinates(db):
    await db.execute("UPDATE addresses SET name='命名地址' WHERE id=1")
    refs = [LocationRef(1000, 1, 1), LocationRef(None, 1), LocationRef(None, 2), LocationRef()]
    places = await fetch_locations(db, refs)
    assert [place.display_name for place in places] == ["家", "命名地址", "上海", None]
    assert places[0].lat == pytest.approx(31.23)
    assert places[0].lon == pytest.approx(121.47)
    assert places[1].lat is None
    assert places[1].geofence is None
    assert places[1].address == "命名地址"
    assert places[3].model_dump() == dict(
        display_name=None, geofence=None, address=None, lat=None, lon=None
    )
    await db.execute("UPDATE addresses SET name='  ' WHERE id=1")
    assert (await fetch_locations(db, [LocationRef(None, 1)]))[0].display_name == "测试路 10"
    await db.execute("UPDATE addresses SET city='',neighbourhood='测试街区' WHERE id=2")
    assert (await fetch_locations(db, [LocationRef(None, 2)]))[0].display_name == "测试街区"


async def test_inline_and_batch_agree_for_drives_and_charging(db):
    expression = location_sql()
    drive = await db.fetchval(
        f"SELECT {expression} FROM drives d LEFT JOIN positions p ON p.id=d.start_position_id "
        "LEFT JOIN addresses a ON a.id=d.start_address_id "
        "LEFT JOIN geofences g ON g.id=d.start_geofence_id WHERE d.id=10"
    )
    charging = await db.fetchval(
        f"SELECT {expression} FROM charging_processes c "
        "LEFT JOIN positions p ON p.id=c.position_id "
        "LEFT JOIN addresses a ON a.id=c.address_id "
        "LEFT JOIN geofences g ON g.id=c.geofence_id WHERE c.id=1"
    )
    import json

    assert json.loads(drive)["display_name"] == json.loads(charging)["display_name"] == "家"
    batch = await fetch_locations(db, [LocationRef(10000, None, 1)])
    assert json.loads(drive) == batch[0].model_dump(mode="json")


async def test_one_page_uses_one_location_query(db):
    rows = await db.fetch(
        "SELECT start_position_id,end_position_id,start_address_id,end_address_id,"
        "start_geofence_id,end_geofence_id FROM drives ORDER BY id LIMIT 20"
    )
    refs = [
        LocationRef(
            row[f"{side}_position_id"], row[f"{side}_address_id"], row[f"{side}_geofence_id"]
        )
        for row in rows
        for side in ("start", "end")
    ]
    # Initialize asyncpg's array codecs before counting page-level data queries.
    await fetch_locations(db, [LocationRef()])
    queries = []
    with db.query_logger(lambda query: queries.append(query.query)):
        places = await fetch_locations(db, refs)
        await asyncio.sleep(0)
    selects = [query for query in queries if query.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert len(selects) == 1
    assert len(places) == 40
    assert await fetch_locations(db, []) == []
