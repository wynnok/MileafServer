"""State transitions are bounded independently of the continuous curve budget."""

from datetime import datetime
from typing import Literal

import asyncpg
from pydantic import BaseModel

from mileaf.database import fetch_rows
from mileaf.queries.charging_fields import HEATER_FIELDS


class StateEvent(BaseModel):
    t: datetime
    field: Literal[
        "battery_heater", "battery_heater_on", "battery_heater_no_power", "not_enough_power_to_heat"
    ]
    value: bool | None


class StateEvents(BaseModel):
    events: list[StateEvent]
    total_events: int
    truncated: bool
    limit: int = 1000
    rule_version: Literal["raw-transitions-v1"] = "raw-transitions-v1"


async def state_events(
    db: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    session_id: int,
    requested: tuple[str, ...],
    supported: dict[str, bool],
) -> StateEvents:
    fields = [name for name in HEATER_FIELDS if name in requested]
    terms = ",".join(f"'{name}',{name if supported[name] else 'NULL::boolean'}" for name in fields)
    rows = await fetch_rows(
        db,
        f"WITH raw AS (SELECT id,date,jsonb_build_object({terms}) AS states "
        "FROM charges WHERE charging_process_id=$1), expanded AS ("
        "SELECT id,date,e.key AS field,(e.value #>> '{}')::boolean AS value "
        "FROM raw CROSS JOIN LATERAL jsonb_each(states) e), ordered AS ("
        "SELECT *,row_number() OVER w AS rn,lag(value) OVER w AS previous "
        "FROM expanded WINDOW w AS (PARTITION BY field ORDER BY date,id)), changes AS ("
        "SELECT * FROM ordered WHERE rn=1 OR value IS DISTINCT FROM previous) "
        "SELECT date AT TIME ZONE 'UTC' AS t,field,value,count(*) OVER () AS total_events "
        "FROM changes ORDER BY date,id,field LIMIT 1000",
        session_id,
    )
    total = rows[0]["total_events"] if rows else 0
    return StateEvents(
        events=[StateEvent(**dict(row)) for row in rows], total_events=total, truncated=total > 1000
    )
