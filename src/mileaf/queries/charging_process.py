"""Original-sample process metrics; PostgreSQL returns one bounded aggregate row."""

from datetime import datetime
from typing import Literal

import asyncpg
from pydantic import BaseModel

from mileaf.database import fetch_rows


class SocWindow(BaseModel):
    from_soc: int = 20
    to_soc: int = 80
    start_at: datetime | None
    end_at: datetime | None
    elapsed_seconds: float | None
    start_observed_soc: int | None
    end_observed_soc: int | None
    status: Literal[
        "available", "not_reached", "incomplete_session", "insufficient_samples", "gap_or_ambiguous"
    ]
    reason: str | None
    threshold_precision: Literal["exact", "crossed"] | None
    observed_seconds: float
    uncovered_seconds: float | None
    max_gap_seconds: int = 300
    rule_version: Literal["observed-threshold-v1"] = "observed-threshold-v1"


class ProcessMetrics(BaseModel):
    power_max_kw: float | None
    power_valid_samples: int
    power_time_weighted_kw: float | None
    observed_seconds: float
    uncovered_seconds: float
    max_gap_seconds: int = 300
    rule_version: Literal["previous-hold-v1"] = "previous-hold-v1"
    soc_windows: list[SocWindow]


async def process_metrics(
    db: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy, session_id: int, now: datetime
) -> ProcessMetrics:
    rows = await fetch_rows(
        db,
        "WITH process AS (SELECT * FROM charging_processes WHERE id=$1), samples AS ("
        "SELECT c.id,c.date,c.charger_power AS power, "
        "CASE WHEN c.battery_level BETWEEN 0 AND 100 THEN c.battery_level END AS soc "
        "FROM charges c JOIN process p ON p.id=c.charging_process_id "
        "WHERE c.date>=p.start_date AND c.date<=coalesce(p.end_date,$2::timestamptz "
        "AT TIME ZONE 'UTC')), ordered AS (SELECT *,row_number() OVER w AS rn, "
        "lead(date) OVER w AS next_date,lag(date) OVER w AS previous_date, "
        "lag(soc) OVER w AS previous_soc FROM samples WINDOW w AS (ORDER BY date,id)), "
        "lower_bound AS (SELECT min(rn) FILTER (WHERE soc>=20) AS lo FROM ordered), "
        "bounds AS (SELECT lo,(SELECT min(rn) FROM ordered WHERE soc>=80 AND rn>lo) AS hi "
        "FROM lower_bound), summary AS (SELECT "
        "max(power) FILTER (WHERE power>=0) AS peak,count(*) FILTER (WHERE power>=0) AS n, "
        "coalesce(sum(extract(epoch FROM next_date-date)) FILTER "
        "(WHERE power>=0 AND next_date>date AND next_date<=date+interval '300 seconds'),0) "
        "AS observed, sum(power*extract(epoch FROM next_date-date)) FILTER "
        "(WHERE power>=0 AND next_date>date AND next_date<=date+interval '300 seconds') "
        "AS weighted, count(soc) AS soc_n FROM ordered), window_summary AS (SELECT "
        "bool_or((rn>1 AND (date<=previous_date OR date>previous_date+interval '300 seconds' "
        "OR previous_soc IS NULL OR soc<previous_soc)) OR soc IS NULL) "
        "FILTER (WHERE rn BETWEEN lo AND hi) AS ambiguous, "
        "coalesce(sum(extract(epoch FROM date-previous_date)) FILTER "
        "(WHERE rn>lo AND rn<=hi AND date>previous_date "
        "AND date<=previous_date+interval '300 seconds' AND soc IS NOT NULL "
        "AND previous_soc IS NOT NULL),0) AS window_observed FROM ordered CROSS JOIN bounds) "
        "SELECT s.*,w.*, p.end_date IS NULL AS active, "
        "greatest(extract(epoch FROM coalesce(p.end_date,$2::timestamptz AT TIME ZONE 'UTC') "
        "-p.start_date),0) AS span, l.date AT TIME ZONE 'UTC' AS start_at, "
        "h.date AT TIME ZONE 'UTC' AS end_at,l.soc AS start_soc,h.soc AS end_soc "
        "FROM process p CROSS JOIN summary s CROSS JOIN window_summary w CROSS JOIN bounds b "
        "LEFT JOIN ordered l ON l.rn=b.lo LEFT JOIN ordered h ON h.rn=b.hi",
        session_id,
        now,
    )
    row = rows[0]
    status: Literal[
        "available", "not_reached", "incomplete_session", "insufficient_samples", "gap_or_ambiguous"
    ] = "available"
    if row["active"]:
        status = "incomplete_session"
    elif row["soc_n"] < 2:
        status = "insufficient_samples"
    elif row["start_soc"] is None or row["start_soc"] >= 80 or row["end_soc"] is None:
        status = "not_reached"
    elif row["ambiguous"]:
        status = "gap_or_ambiguous"
    elapsed = (
        (row["end_at"] - row["start_at"]).total_seconds()
        if row["end_at"] is not None and row["start_at"] is not None
        else None
    )
    observed = float(row["observed"])
    available = status == "available"
    window = SocWindow(
        start_at=row["start_at"] if not row["active"] else None,
        end_at=row["end_at"] if not row["active"] else None,
        start_observed_soc=row["start_soc"] if not row["active"] else None,
        end_observed_soc=row["end_soc"] if not row["active"] else None,
        elapsed_seconds=elapsed if available else None,
        status=status,
        reason=None if available else status,
        threshold_precision=(
            "exact" if row["start_soc"] == 20 and row["end_soc"] == 80 else "crossed"
        )
        if available
        else None,
        observed_seconds=float(row["window_observed"]) if not row["active"] else 0,
        uncovered_seconds=max(0, elapsed - float(row["window_observed"]))
        if elapsed is not None and not row["active"]
        else None,
    )
    return ProcessMetrics(
        power_max_kw=row["peak"],
        power_valid_samples=row["n"],
        power_time_weighted_kw=float(row["weighted"]) / observed if observed else None,
        observed_seconds=observed,
        uncovered_seconds=max(0, float(row["span"]) - observed),
        soc_windows=[window],
    )
