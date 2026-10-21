from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from mileaf.agent.client import AgentAuthError, MileafClient
from mileaf.agent.config import AgentSettings
from mileaf.agent.mapper import map_charge, map_drive, map_status, map_vehicle
from mileaf.agent.source import TeslaMateSource, ensure_supported_schema
from mileaf.agent.state import AgentState, load_state, save_state, utc_now_iso
from mileaf.schemas import AgentSyncIn

logger = logging.getLogger(__name__)

OVERLAP = timedelta(days=2)


@dataclass
class SyncReport:
    vehicles: int
    statuses: int
    drives: int
    charges: int
    full_sync: bool


async def sync_once(
    settings: AgentSettings,
    *,
    state: AgentState | None = None,
    source: TeslaMateSource | None = None,
    client: MileafClient | None = None,
    now: datetime | None = None,
) -> SyncReport:
    current_time = now or datetime.now(UTC)
    agent_state = state or load_state(settings.state_file, settings.agent_token)

    owns_source = source is None
    owns_client = client is None
    tm_source = source or TeslaMateSource(settings.asyncpg_dsn)
    api_client = client or MileafClient(
        settings.mileaf_url, timeout_seconds=settings.request_timeout_seconds
    )

    try:
        if owns_source:
            await tm_source.connect()
        if owns_client:
            await api_client.__aenter__()

        schema_version = await tm_source.schema_version()
        ensure_supported_schema(schema_version)
        source_version = settings.teslamate_version or "unknown"
        await _ensure_agent_token(
            settings,
            agent_state,
            api_client,
            schema_version=schema_version,
            source_version=source_version,
        )
        api_client.set_agent_token(agent_state.agent_token)

        vehicles = [map_vehicle(row) for row in await tm_source.fetch_vehicles()]
        efficiency_by_vehicle = {vehicle.external_id: vehicle.efficiency for vehicle in vehicles}
        statuses = [map_status(row) for row in await tm_source.fetch_statuses()]

        full_sync = _needs_full_sync(agent_state, current_time, settings)
        drive_since = None if full_sync else _incremental_since(agent_state.last_drive_start_at)
        charge_since = None if full_sync else _incremental_since(agent_state.last_charge_start_at)
        drives = [
            map_drive(row, efficiency_by_vehicle.get(int(row["car_id"])))
            for row in await tm_source.fetch_drives(drive_since)
        ]
        charges = [map_charge(row) for row in await tm_source.fetch_charges(charge_since)]

        sync_metadata: dict[str, str | None] = {
            "source_version": source_version,
            "schema_version": schema_version,
        }
        if vehicles:
            await api_client.sync(
                AgentSyncIn(
                    **sync_metadata,
                    vehicles=vehicles,
                    statuses=statuses,
                ).model_dump(mode="json")
            )
        else:
            await api_client.heartbeat(source_version=source_version, schema_version=schema_version)

        await _send_batches(api_client, drives, "drives", sync_metadata, settings.batch_size)
        await _send_batches(api_client, charges, "charges", sync_metadata, settings.batch_size)

        if drives:
            latest_drive = max(drive.start_date for drive in drives)
            agent_state.last_drive_start = latest_drive.isoformat()
        if charges:
            latest_charge = max(charge.start_date for charge in charges)
            agent_state.last_charge_start = latest_charge.isoformat()
        if full_sync:
            agent_state.last_full_sync_at = current_time.isoformat()
        agent_state.last_sync_at = utc_now_iso()
        save_state(settings.state_file, agent_state)

        logger.info(
            "sync complete: vehicles=%d statuses=%d drives=%d charges=%d full=%s",
            len(vehicles),
            len(statuses),
            len(drives),
            len(charges),
            full_sync,
        )
        return SyncReport(
            vehicles=len(vehicles),
            statuses=len(statuses),
            drives=len(drives),
            charges=len(charges),
            full_sync=full_sync,
        )
    finally:
        if owns_source:
            await tm_source.close()
        if owns_client:
            await api_client.__aexit__(None, None, None)


async def _ensure_agent_token(
    settings: AgentSettings,
    state: AgentState,
    client: MileafClient,
    *,
    schema_version: str | None,
    source_version: str,
) -> None:
    if state.agent_token:
        return
    if settings.agent_token:
        state.agent_token = settings.agent_token
        save_state(settings.state_file, state)
        return
    if not settings.pairing_code:
        raise AgentAuthError(
            "no agent token configured; set MILEAF_AGENT_PAIRING_CODE or MILEAF_AGENT_AGENT_TOKEN"
        )

    paired = await client.pair(
        code=settings.pairing_code,
        name=settings.source_name,
        source_version=source_version,
        schema_version=schema_version,
    )
    state.agent_token = paired["agent_token"]
    state.tenant_id = paired["tenant_id"]
    state.data_source_id = paired["data_source_id"]
    save_state(settings.state_file, state)
    logger.info("paired with Mileaf Cloud data_source_id=%s", state.data_source_id)


async def _send_batches[ItemT](
    client: MileafClient,
    items: Sequence[ItemT],
    field: str,
    sync_metadata: dict[str, str | None],
    batch_size: int,
) -> None:
    for batch in _chunks(items, batch_size):
        payload: dict[str, Any] = {**sync_metadata, field: batch}
        await client.sync(AgentSyncIn.model_validate(payload).model_dump(mode="json"))


def _chunks[ItemT](items: Sequence[ItemT], size: int) -> Iterator[list[ItemT]]:
    for index in range(0, len(items), size):
        yield list(items[index : index + size])


def _needs_full_sync(state: AgentState, now: datetime, settings: AgentSettings) -> bool:
    if state.last_drive_start_at is None or state.last_charge_start_at is None:
        return True
    last_full = state.last_full_sync_datetime
    if last_full is None:
        return True
    return now - last_full >= timedelta(hours=settings.full_sync_interval_hours)


def _incremental_since(cursor: datetime | None) -> datetime | None:
    # Re-read a small overlap because TeslaMate may update a completed record
    # (for example, when a charge cost is edited).
    return cursor - OVERLAP if cursor else None
