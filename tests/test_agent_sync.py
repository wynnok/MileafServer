from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mileaf.agent.config import AgentSettings
from mileaf.agent.state import AgentState
from mileaf.agent.sync import sync_once


class FakeSource:
    def __init__(self) -> None:
        self.since_calls: list[datetime | None] = []
        self.drives: list[dict[str, Any]] = []
        self.charges: list[dict[str, Any]] = []

    async def schema_version(self) -> str:
        return "20260101000000"

    async def fetch_vehicles(self) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "name": "Blue Thunder",
                "marketing_name": "Model 3",
                "model": "3",
                "vin": "VIN",
                "trim_badging": None,
                "exterior_color": None,
                "wheel_type": None,
                "efficiency": 0.153,
                "display_priority": 1,
            }
        ]

    async def fetch_statuses(self) -> list[dict[str, Any]]:
        return [
            {
                "car_id": 1,
                "captured_at": datetime(2026, 10, 20, 10, 0, 0),
                "battery_level": 80,
                "usable_battery_level": 78,
                "est_battery_range_km": 350.5,
                "rated_battery_range_km": 400.0,
                "odometer": 12345.6,
                "latitude": 31.2,
                "longitude": 121.4,
                "speed": 0,
                "power": 0,
                "outside_temp": 22.5,
                "inside_temp": 23.0,
                "state": "online",
                "charging_process_id": None,
                "charger_power_kw": None,
            }
        ]

    async def fetch_drives(self, since: datetime | None = None) -> list[dict[str, Any]]:
        self.since_calls.append(since)
        return self.drives

    async def fetch_charges(self, since: datetime | None = None) -> list[dict[str, Any]]:
        self.since_calls.append(since)
        return self.charges


class FakeClient:
    def __init__(self) -> None:
        self.sync_calls: list[dict[str, Any]] = []
        self.heartbeat_calls: list[tuple[str | None, str | None]] = []
        self.pair_calls: list[dict[str, Any]] = []
        self.token: str | None = None

    def set_agent_token(self, token: str | None) -> None:
        self.token = token

    async def pair(self, **kwargs: Any) -> dict[str, Any]:
        self.pair_calls.append(kwargs)
        return {
            "agent_token": "mlf_agent_paired",
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "data_source_id": "00000000-0000-0000-0000-000000000002",
        }

    async def sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sync_calls.append(payload)
        return {"ok": True}

    async def heartbeat(
        self, *, source_version: str | None, schema_version: str | None
    ) -> dict[str, Any]:
        self.heartbeat_calls.append((source_version, schema_version))
        return {"ok": True}


def drive_row(drive_id: int, start: datetime) -> dict[str, Any]:
    return {
        "id": drive_id,
        "car_id": 1,
        "start_date": start,
        "end_date": start,
        "distance": 10.0,
        "duration_min": 10,
        "outside_temp_avg": 20.0,
        "inside_temp_avg": 21.0,
        "speed_max": 80,
        "power_max": 100000,
        "power_min": -10000,
        "start_ideal_range_km": 300.0,
        "end_ideal_range_km": 290.0,
        "start_rated_range_km": 290.0,
        "end_rated_range_km": 280.0,
        "start_km": 0.0,
        "end_km": 10.0,
        "ascent": 1,
        "descent": 1,
        "start_geofence": None,
        "end_geofence": None,
        "start_address": "A",
        "end_address": "B",
        "start_latitude": 1.0,
        "start_longitude": 2.0,
        "end_latitude": 3.0,
        "end_longitude": 4.0,
        "start_battery_level": 80,
        "end_battery_level": 75,
    }


def charge_row(charge_id: int, start: datetime) -> dict[str, Any]:
    return {
        "id": charge_id,
        "car_id": 1,
        "start_date": start,
        "end_date": start,
        "duration_min": 30,
        "charge_energy_added": 5.0,
        "charge_energy_used": 5.2,
        "start_battery_level": 50,
        "end_battery_level": 60,
        "start_ideal_range_km": 200.0,
        "end_ideal_range_km": 240.0,
        "start_rated_range_km": 190.0,
        "end_rated_range_km": 230.0,
        "outside_temp_avg": 20.0,
        "cost": 5.0,
        "geofence": "Home",
        "location": "Home",
        "latitude": 1.0,
        "longitude": 2.0,
        "max_power_kw": 11.0,
        "fast_charge": False,
    }


async def test_full_sync_batches_and_persists_state(tmp_path: Path) -> None:
    settings = AgentSettings(
        state_file=str(tmp_path / "state.json"),
        batch_size=1,
        full_sync_interval_hours=24,
    )
    source = FakeSource()
    source.drives = [
        drive_row(1, datetime(2026, 10, 19, 8, 0, tzinfo=UTC)),
        drive_row(2, datetime(2026, 10, 20, 8, 0, tzinfo=UTC)),
    ]
    source.charges = [charge_row(1, datetime(2026, 10, 18, 8, 0, tzinfo=UTC))]
    client = FakeClient()
    state = AgentState(agent_token="token")

    report = await sync_once(
        settings,
        state=state,
        source=source,
        client=client,
        now=datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
    )

    assert report.full_sync is True
    assert report.drives == 2
    assert report.charges == 1
    # vehicles+statuses, two drive batches, one charge batch
    assert len(client.sync_calls) == 4
    assert source.since_calls == [None, None]
    assert state.last_drive_start == "2026-10-20T08:00:00+00:00"
    assert state.last_charge_start == "2026-10-18T08:00:00+00:00"
    assert (tmp_path / "state.json").exists()


async def test_incremental_sync_uses_overlap_cursor(tmp_path: Path) -> None:
    settings = AgentSettings(state_file=str(tmp_path / "state.json"))
    source = FakeSource()
    client = FakeClient()
    state = AgentState(
        agent_token="token",
        last_drive_start="2026-10-20T08:00:00+00:00",
        last_charge_start="2026-10-18T08:00:00+00:00",
        last_full_sync_at="2026-10-21T00:00:00+00:00",
    )

    report = await sync_once(
        settings,
        state=state,
        source=source,
        client=client,
        now=datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
    )

    assert report.full_sync is False
    assert source.since_calls == [
        datetime(2026, 10, 18, 8, 0, tzinfo=UTC),
        datetime(2026, 10, 16, 8, 0, tzinfo=UTC),
    ]
    # vehicles+statuses only when there is no new drive/charge data
    assert len(client.sync_calls) == 1


async def test_pairing_saves_agent_token(tmp_path: Path) -> None:
    settings = AgentSettings(
        state_file=str(tmp_path / "state.json"),
        pairing_code="ABCD2345",
        source_name="Home TeslaMate",
    )
    source = FakeSource()
    client = FakeClient()
    state = AgentState()

    await sync_once(
        settings,
        state=state,
        source=source,
        client=client,
        now=datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
    )

    assert client.pair_calls
    assert state.agent_token == "mlf_agent_paired"
    assert client.token == "mlf_agent_paired"
    assert (tmp_path / "state.json").exists()
