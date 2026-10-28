"""Database construction, aggregation and bounded paging of parking observations."""

from mileaf.queries.parking import parking_events_sql, parking_states_sql


def parking_observations_sql() -> str:
    endpoint_fields = {
        "soc": "soc",
        "odometer_km": "odometer",
        "rated_range_km": "rated_range",
        "position_id": "position_id",
    }

    def endpoint(side: str) -> str:
        prefix = "end" if side == "start" else "start"
        source = "event_type" if side == "start" else "next_type"
        soc_source = (
            f"CASE WHEN {source}='drive' THEN 'drive_{prefix}_position' "
            f"ELSE 'charging_{prefix}_battery_level' END"
        )
        odo_source = (
            f"CASE WHEN {source}='drive' THEN 'drive_{prefix}_position' "
            f"ELSE 'charging_{prefix}_position' END"
        )
        values = []
        for field, column in endpoint_fields.items():
            values.extend([f"'{field}'", f"{side}_{column}"])
        values.extend(
            [
                "'raw_time'",
                "ws" if side == "start" else "we",
                "'soc_source'",
                f"CASE WHEN {side}_soc IS NOT NULL THEN {soc_source} END",
                "'odometer_source'",
                f"CASE WHEN {side}_odometer IS NOT NULL THEN {odo_source} END",
            ]
        )
        return "jsonb_build_object(" + ",".join(values) + ")"

    reasons = (
        "same_moment_gap",
        "invalid_time_order",
        "event_overlap",
        "ambiguous_states",
        "missing_soc",
        "missing_odometer",
        "odometer_change",
        "insufficient_duration",
    )
    aggregates = {
        "candidate_windows": "count(*) FILTER (WHERE NOT is_open)",
        "valid_windows": "count(*) FILTER (WHERE included)",
        "excluded_windows": "count(*) FILTER (WHERE NOT is_open AND NOT included)",
        "open_windows": "count(*) FILTER (WHERE is_open)",
        "observed_hours": "coalesce(sum(duration_hours) FILTER (WHERE included),0)",
        "soc_change_pp": "sum(soc_change) FILTER (WHERE included)",
        "soc_change_pp_per_24h": (
            "sum(soc_change) FILTER (WHERE included)*24.0/"
            "nullif(sum(duration_hours) FILTER (WHERE included),0)"
        ),
    }
    for state in ("online", "asleep", "offline", "unknown"):
        aggregates[f"{state}_hours"] = f"coalesce(sum({state}_hours) FILTER (WHERE included),0)"
        aggregates[f"{state}_share"] = (
            f"sum({state}_hours) FILTER (WHERE included)/"
            "nullif(sum(duration_hours) FILTER (WHERE included),0)"
        )
    pairs = [value for name, expr in aggregates.items() for value in (f"'{name}'", expr)]
    reason_pairs = [
        value
        for reason in reasons
        for value in (f"'{reason}'", f"count(*) FILTER (WHERE reason='{reason}')")
    ]
    pairs.extend(["'excluded_by_reason'", "jsonb_build_object(" + ",".join(reason_pairs) + ")"])
    aggregate = "jsonb_build_object(" + ",".join(pairs) + ")"
    return f"""
    WITH events AS MATERIALIZED ({parking_events_sql()}),
    states AS MATERIALIZED ({parking_states_sql()}),
    ordered AS (
      SELECT e.*, lead(to_jsonb(e)) OVER (ORDER BY event_start,event_type,event_id) AS nxt,
             bool_and(event_end IS NOT NULL) OVER () AS all_closed,
             max(coalesce(event_end,$2)) OVER (ORDER BY event_start,event_type,event_id ROWS
UNBOUNDED PRECEDING) AS prior_extent
      FROM events e
    ), boundaries AS (
      SELECT event_type,event_id,nxt,all_closed,prior_extent,event_end AS ws,
        CASE WHEN nxt IS NULL THEN $2::timestamptz ELSE (nxt->>'event_start')::timestamptz END AS
we,
        nxt IS NULL AS is_open, nxt->>'event_type' AS next_type,
        (nxt->>'event_id')::int AS next_id,
        end_soc AS start_soc, end_odometer AS start_odometer,
        end_rated_range AS start_rated_range, end_position_id AS start_position_id,
        (nxt->>'start_soc')::int AS end_soc_value,
        (nxt->>'start_odometer')::float8 AS end_odometer_value,
        (nxt->>'start_rated_range')::numeric AS end_rated_range_value,
        (nxt->>'start_position_id')::int AS end_position_id_value
      FROM ordered WHERE event_end IS NOT NULL
    ), windows AS (
      SELECT event_type,event_id,next_type,next_id,ws,we,is_open,
        start_soc,start_odometer,start_rated_range,start_position_id,
        end_soc_value AS end_soc, end_odometer_value AS end_odometer,
        end_rated_range_value AS end_rated_range,end_position_id_value AS end_position_id,
        extract(epoch FROM we-ws)/3600.0 AS duration_hours,
        end_soc_value-start_soc AS soc_change,
        end_odometer_value-start_odometer AS odo_change,
        prior_extent>ws AS has_overlap
      FROM boundaries WHERE ws >= $3 AND ws < $4 AND we <= $2
        AND (NOT is_open OR (all_closed AND ws<$2))
    ), measured AS (
      SELECT w.*, coalesce(sh.online_hours,0) AS online_hours,
        coalesce(sh.asleep_hours,0) AS asleep_hours,coalesce(sh.offline_hours,0) AS offline_hours,
greatest(0,duration_hours-coalesce(sh.online_hours,0)-coalesce(sh.asleep_hours,0)-coalesce(sh.offline_hours,0))
AS unknown_hours,
        coalesce(sh.ambiguous,false) AS ambiguous
      FROM windows w LEFT JOIN LATERAL (
        SELECT sum(extract(epoch FROM hi-lo)/3600.0) FILTER (WHERE state='online' AND closed) AS
online_hours,
          sum(extract(epoch FROM hi-lo)/3600.0) FILTER (WHERE state='asleep' AND closed) AS
asleep_hours,
          sum(extract(epoch FROM hi-lo)/3600.0) FILTER (WHERE state='offline' AND closed) AS
offline_hours,
          bool_or(lo<prior_end) AS ambiguous
        FROM (SELECT *, max(hi) OVER (ORDER BY lo,hi ROWS BETWEEN UNBOUNDED PRECEDING AND 1
PRECEDING) AS prior_end
          FROM (SELECT state,greatest(start_at,ws) AS lo,least(coalesce(end_at,we),we) AS hi,
            end_at IS NOT NULL AS closed FROM states WHERE start_at<we AND coalesce(end_at,we)>ws)
clips
          WHERE hi>lo) spans
      ) sh ON true
    ), classified AS (
      SELECT *, CASE WHEN is_open THEN NULL WHEN ws=we THEN 'same_moment_gap'
        WHEN ws>we THEN 'invalid_time_order' WHEN has_overlap THEN 'event_overlap'
        WHEN ambiguous THEN 'ambiguous_states'
        WHEN start_soc IS NULL OR end_soc IS NULL OR start_soc NOT BETWEEN 0 AND 100 OR end_soc
NOT BETWEEN 0 AND 100 THEN 'missing_soc'
        WHEN start_odometer IS NULL OR end_odometer IS NULL THEN 'missing_odometer'
        WHEN NOT (odo_change > '-Infinity'::float8 AND odo_change < 'Infinity'::float8) OR
abs(odo_change)>=1 THEN 'odometer_change'
        WHEN duration_hours<1 THEN 'insufficient_duration' END AS reason
      FROM measured
    ), valid AS (
      SELECT *, NOT is_open AND reason IS NULL AS included,
        date_trunc($6,ws AT TIME ZONE $5) AT TIME ZONE $5 AS bucket_start
      FROM classified
    ), represented AS (
      SELECT *, jsonb_build_object(
        'window_id',event_type||':'||event_id||'~'||CASE WHEN is_open THEN 'open' ELSE
next_type||':'||next_id END,
        'start_event',jsonb_build_object('type',event_type,'id',event_id),
        'end_event',CASE WHEN NOT is_open THEN jsonb_build_object('type',next_type,'id',next_id)
END,
        'window_start',ws,'window_end',we,'duration_hours',duration_hours,
        'start',{endpoint("start")},'end',CASE WHEN is_open THEN '{{}}'::jsonb ELSE
{endpoint("end")} END,
        'soc_change_pp',soc_change,'soc_change_pp_per_24h',CASE WHEN included THEN
soc_change/duration_hours*24 END,
        'odometer_change_km',odo_change,'negative_odometer_change',included AND odo_change<0,
'online_hours',online_hours,'asleep_hours',asleep_hours,'offline_hours',offline_hours,'unknown_hours',unknown_hours,
        'quality',CASE WHEN is_open THEN 'open_window' WHEN NOT included THEN 'excluded' WHEN
unknown_hours>0 THEN 'partial_state_coverage' ELSE 'complete' END,
        'exclusion_reason',reason,'included',included) AS item
      FROM valid
    ), aggregated AS (
      SELECT bucket_start, grouping(bucket_start)=1 AS is_total,{aggregate} AS result
      FROM represented GROUP BY GROUPING SETS ((bucket_start),())
    ), page AS (
      SELECT * FROM represented WHERE $8::timestamptz IS NULL OR ws<$8 OR
        (ws=$8 AND
(event_type,event_id,coalesce(next_type,''),coalesce(next_id,0))>($9,$10,$11,$12))
      ORDER BY ws DESC,event_type,event_id,coalesce(next_type,''),coalesce(next_id,0) LIMIT $7
    )
    SELECT (SELECT result FROM aggregated WHERE is_total) AS totals,
      (SELECT coalesce(jsonb_agg(jsonb_build_object('bucket_start',bucket_start,'result',result)),
'[]') FROM aggregated WHERE NOT is_total) AS buckets,
      (SELECT coalesce(jsonb_agg(item ORDER BY ws
DESC,event_type,event_id,coalesce(next_type,''),coalesce(next_id,0)),'[]') FROM page) AS items,
      (SELECT count(*) FROM represented) AS matched
    """
