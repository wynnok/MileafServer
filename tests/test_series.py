"""Series sampling behavior against real position and charge samples."""

import pytest

from mileaf.errors import APIError
from mileaf.queries.series import fetch_series

POSITIONS = dict(
    table="positions",
    time_column="date",
    columns={"speed": "speed", "battery": "battery_level", "lat": "latitude", "lon": "longitude"},
    required_fields=("lat", "lon"),
    where="drive_id=$1",
)


async def test_short_sparse_empty_and_missing_samples_are_preserved(db):
    for drive_id, count in [(1, 121), (2, 3), (13, 0)]:
        result = await fetch_series(db, **POSITIONS, args=(drive_id,), fields="speed,battery")
        assert len(result.series) == count
        assert result.resolution == ("server_sampled" if count else "empty")
        if count:
            assert all(p["lat"] is not None and p["lon"] is not None for p in result.series)
    await db.execute("UPDATE positions SET speed=NULL WHERE id=2001")
    result = await fetch_series(db, **POSITIONS, args=(2,), fields="speed")
    assert [p["speed"] for p in result.series] == [60, None, 60]
    assert "battery" not in result.series[0]


async def test_dense_series_keeps_endpoints_order_and_peak(db):
    await db.execute("UPDATE positions SET speed=150 WHERE id=1060")
    result = await fetch_series(db, **POSITIONS, args=(1,), fields="speed", points=24)
    raw = await db.fetch(
        "SELECT date AT TIME ZONE 'UTC' AS t FROM positions WHERE drive_id=1 ORDER BY date,id"
    )
    assert len(result.series) == 24
    assert result.series[0]["t"] == raw[0]["t"]
    assert result.series[-1]["t"] == raw[-1]["t"]
    assert [p["t"] for p in result.series] == sorted(p["t"] for p in result.series)
    assert max(p["speed"] for p in result.series) == 150
    two = await fetch_series(db, **POSITIONS, args=(1,), fields="speed", points=2)
    assert [p["t"] for p in two.series] == [raw[0]["t"], raw[-1]["t"]]


async def test_charge_steps_are_selected_without_interpolation(db):
    await db.execute(
        "UPDATE charges SET charger_power=CASE WHEN id<2020 THEN 120 "
        "WHEN id<2040 THEN 60 ELSE 20 END WHERE charging_process_id=2"
    )
    result = await fetch_series(
        db,
        table="charges",
        time_column="date",
        columns={"power": "charger_power", "soc": "battery_level"},
        where="charging_process_id=$1",
        args=(2,),
        points=10,
    )
    assert len(result.series) == 10
    powers = [p["power"] for p in result.series]
    assert set(powers) <= {120, 60, 20}
    assert powers == sorted(powers, reverse=True)


@pytest.mark.parametrize(
    "points,fields",
    [(0, "speed"), (1, "speed"), (1001, "speed"), ("abc", "speed"), (200, "unknown"), (200, "")],
)
async def test_invalid_sampling_parameters(db, points, fields):
    with pytest.raises(APIError) as error:
        await fetch_series(db, **POSITIONS, args=(1,), points=points, fields=fields)
    assert error.value.status_code == 400
