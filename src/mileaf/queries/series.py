"""Bounded SQL time buckets followed by LTTB over original samples."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

from mileaf.database import fetch_rows
from mileaf.errors import APIError, ErrorCode
from mileaf.queries.pagination import bounded_integer


@dataclass(frozen=True)
class Series:
    series: list[dict[str, Any]]
    resolution: Literal["empty", "server_sampled"]


def lttb(rows: list[dict[str, Any]], points: int, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Select samples without inventing values. Multi-field areas use normalized axes.

    Missing values do not contribute triangle area; returned NULLs remain untouched.
    The SQL stage prioritizes extrema of the first requested field. Subsequent LTTB
    selection considers every requested field, independent of their units.
    """
    if len(rows) <= points:
        return rows
    if points == 2:
        return [rows[0], rows[-1]]
    x = [(row["t"] - rows[0]["t"]).total_seconds() for row in rows]
    scales = {}
    for field in fields:
        values = [float(row[field]) for row in rows if row[field] is not None]
        scales[field] = max(values) - min(values) if values else 0.0
    every = (len(rows) - 2) / (points - 2)
    selected = [rows[0]]
    anchor = 0
    for bucket in range(points - 2):
        next_start = math.floor((bucket + 1) * every) + 1
        next_end = min(math.floor((bucket + 2) * every) + 1, len(rows))
        avg_x = sum(x[next_start:next_end]) / (next_end - next_start)
        means = {}
        for field in fields:
            values = [
                float(row[field]) for row in rows[next_start:next_end] if row[field] is not None
            ]
            means[field] = sum(values) / len(values) if values else None
        start = math.floor(bucket * every) + 1
        end = min(next_start, len(rows) - 1)
        best, best_area = start, -1.0
        for candidate in range(start, end):
            area = 0.0
            for field in fields:
                a, b, mean = rows[anchor][field], rows[candidate][field], means[field]
                scale = scales[field]
                if a is not None and b is not None and mean is not None and scale:
                    area += (
                        abs(
                            (x[anchor] - avg_x) * (float(b) - float(a))
                            - (x[anchor] - x[candidate]) * (mean - float(a))
                        )
                        / scale
                    )
            if area > best_area:
                best, best_area = candidate, area
        selected.append(rows[best])
        anchor = best
    return [*selected, rows[-1]]


async def fetch_series(
    db: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    *,
    table: str,
    time_column: str,
    columns: Mapping[str, str],
    where: str,
    args: tuple[object, ...] = (),
    fields: str | None = None,
    required_fields: tuple[str, ...] = (),
    points: int | str = 200,
) -> Series:
    """SQL inputs/column aliases are trusted developer constants; values use args.

    Source timestamps are TeslaMate UTC timestamp-without-time-zone columns. Tables
    must have a unique id for deterministic ordering at equal timestamps. columns
    maps allowed output names to numeric SQL expressions. The first requested field
    supplies SQL bucket extrema; put the chart's principal metric first.
    """
    size = bounded_integer(points, default_name="points", minimum=2, maximum=1000)
    requested = tuple(fields.split(",")) if fields is not None else tuple(columns)
    if not requested or any(field not in columns for field in (*requested, *required_fields)):
        raise APIError(400, ErrorCode.INVALID_REQUEST, "Invalid fields")
    names = tuple(dict.fromkeys((*requested, *required_fields)))
    projection = ", ".join(f'{columns[name]} AS "{name}"' for name in names)
    output_columns = ", ".join(f'"{name}"' for name in names)
    principal = f'"{requested[0]}"'
    count = f"${len(args) + 1}::integer"
    # Each time bucket retains at most four original rows, including first/last.
    # Global first/last therefore survive; at most 4*points rows leave PostgreSQL.
    rows = await fetch_rows(
        db,
        f"WITH source AS (SELECT id AS _id, {time_column} AT TIME ZONE 'UTC' AS t, "
        f"{projection} FROM {table} WHERE {where}), "
        "bounds AS (SELECT *, count(*) OVER () AS n, min(t) OVER () AS lo, "
        "max(t) OVER () AS hi FROM source), "
        "bucketed AS (SELECT *, CASE WHEN hi=lo THEN 0 ELSE "
        f"least({count}-1, floor(extract(epoch FROM (t-lo)) / "
        f"nullif(extract(epoch FROM (hi-lo)),0) * {count})::integer) END AS bucket "
        "FROM bounds), ranked AS (SELECT *, "
        "row_number() OVER (PARTITION BY bucket ORDER BY t,_id) AS first_rank, "
        "row_number() OVER (PARTITION BY bucket ORDER BY t DESC,_id DESC) AS last_rank, "
        f"row_number() OVER (PARTITION BY bucket ORDER BY {principal} ASC NULLS LAST,t,_id) "
        "AS min_rank, "
        f"row_number() OVER (PARTITION BY bucket ORDER BY {principal} DESC NULLS LAST,t,_id) "
        "AS max_rank FROM bucketed) "
        f"SELECT t, {output_columns} FROM ranked "
        f"WHERE n <= 4*{count} OR first_rank=1 OR last_rank=1 OR min_rank=1 OR max_rank=1 "
        "ORDER BY t,_id",
        *args,
        size,
    )
    sampled = lttb([dict(row) for row in rows], size, names)
    return Series(sampled, "server_sampled" if sampled else "empty")
