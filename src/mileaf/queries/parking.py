"""Read-only source queries for ST08 closed parking windows (closed-window-soc-v1).

Two bounded business queries feed the whole endpoint: one event/endpoint projection over
``drives`` and ``charging_processes`` (each resolved to its exact boundary position), and one
``states`` projection. Window construction, overlap detection, state intersection, exclusion
priority and aggregation run server side over the complete match set, never per window.
"""

from mileaf.queries.statistics_fields import STATISTICS_REQUIREMENTS

# Columns this feature reads that may be absent on older/partial schemas. Absence marks the
# feature unsupported without affecting 0.1/0.2 surfaces; udt_name keeps enum renames tolerant.
STATISTICS_REQUIREMENTS["parking_observations"] = {
    "drives": {
        "car_id": ("int2",),
        "start_date": ("timestamp",),
        "end_date": ("timestamp",),
        "start_position_id": ("int4",),
        "end_position_id": ("int4",),
    },
    "charging_processes": {
        "car_id": ("int2",),
        "start_date": ("timestamp",),
        "end_date": ("timestamp",),
        "position_id": ("int4",),
        "start_battery_level": ("int2",),
        "end_battery_level": ("int2",),
        "start_rated_range_km": ("numeric",),
        "end_rated_range_km": ("numeric",),
    },
    "positions": {
        "car_id": ("int2",),
        "date": ("timestamp",),
        "battery_level": ("int2",),
        "odometer": ("float8",),
        "rated_battery_range_km": ("numeric",),
    },
    "states": {
        "car_id": ("int2",),
        "state": ("states_status",),
        "start_date": ("timestamp",),
        "end_date": ("timestamp",),
    },
}

_EVENT_COLUMNS = (
    "event_type",
    "event_id",
    "event_start",
    "event_end",
    "start_soc",
    "start_odometer",
    "start_rated_range",
    "start_position_id",
    "start_pos_time",
    "end_soc",
    "end_odometer",
    "end_rated_range",
    "end_position_id",
    "end_pos_time",
)


def parking_events_sql() -> str:
    """Project every event that can bound or cross a window, with exact endpoint evidence.

    Bind ``$1`` car_id, ``$2`` as_of (timestamptz), ``$3`` from (timestamptz). Drive endpoints
    resolve only when the referenced position belongs to the same car and its timestamp equals
    the drive boundary; charging start odometer uses ``position_id`` at ``start_date`` and the
    charging end odometer uses the highest-id same-car position at exactly ``end_date``. No
    nearest-history fallback and no extrapolation: an unmatched endpoint stays NULL.
    """
    drives = (
        "SELECT 'drive'::text AS event_type, d.id AS event_id, "
        "d.start_date AT TIME ZONE 'UTC' AS event_start, "
        "d.end_date AT TIME ZONE 'UTC' AS event_end, "
        "sp.battery_level AS start_soc, sp.odometer AS start_odometer, "
        "sp.rated_battery_range_km AS start_rated_range, sp.id AS start_position_id, "
        "sp.date AT TIME ZONE 'UTC' AS start_pos_time, "
        "ep.battery_level AS end_soc, ep.odometer AS end_odometer, "
        "ep.rated_battery_range_km AS end_rated_range, ep.id AS end_position_id, "
        "ep.date AT TIME ZONE 'UTC' AS end_pos_time "
        "FROM drives d "
        "LEFT JOIN positions sp ON sp.id=d.start_position_id AND sp.car_id=d.car_id "
        "AND sp.date=d.start_date "
        "LEFT JOIN positions ep ON ep.id=d.end_position_id AND ep.car_id=d.car_id "
        "AND ep.date=d.end_date "
        "WHERE d.car_id=$1 AND d.start_date AT TIME ZONE 'UTC'<=$2 "
        "AND (d.end_date IS NULL OR d.end_date AT TIME ZONE 'UTC'>=$3)"
    )
    charging = (
        "SELECT 'charging_session'::text, cp.id, "
        "cp.start_date AT TIME ZONE 'UTC', cp.end_date AT TIME ZONE 'UTC', "
        "cp.start_battery_level, sp.odometer, cp.start_rated_range_km, sp.id, "
        "sp.date AT TIME ZONE 'UTC', "
        "cp.end_battery_level, ep.odometer, cp.end_rated_range_km, ep.id, "
        "ep.date AT TIME ZONE 'UTC' "
        "FROM charging_processes cp "
        "LEFT JOIN positions sp ON sp.id=cp.position_id AND sp.car_id=cp.car_id "
        "AND sp.date=cp.start_date "
        "LEFT JOIN LATERAL (SELECT p.id, p.odometer, p.date FROM positions p "
        "WHERE p.car_id=cp.car_id AND p.date=cp.end_date ORDER BY p.id DESC LIMIT 1) ep ON TRUE "
        "WHERE cp.car_id=$1 AND cp.start_date AT TIME ZONE 'UTC'<=$2 "
        "AND (cp.end_date IS NULL OR cp.end_date AT TIME ZONE 'UTC'>=$3)"
    )
    return (
        f"SELECT {', '.join('e.' + column for column in _EVENT_COLUMNS)} "
        f"FROM ({drives} UNION ALL {charging}) e "
        "ORDER BY e.event_start, e.event_type, e.event_id"
    )


def parking_states_sql() -> str:
    """Project state segments that can cover or cross a window.

    Bind ``$1`` car_id, ``$2`` as_of (timestamptz), ``$3`` from (timestamptz). Open segments
    (``end_date IS NULL``) keep a NULL end so the caller treats their span as unknown.
    """
    return (
        "SELECT s.state::text AS state, s.start_date AT TIME ZONE 'UTC' AS start_at, "
        "s.end_date AT TIME ZONE 'UTC' AS end_at, s.id AS state_id "
        "FROM states s WHERE s.car_id=$1 AND s.start_date AT TIME ZONE 'UTC'<=$2 "
        "AND (s.end_date IS NULL OR s.end_date AT TIME ZONE 'UTC'>=$3) "
        "ORDER BY s.start_date, s.id"
    )
