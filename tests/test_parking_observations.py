"""ST08 closed parking windows through the approved HTTP seam and isolated PostgreSQL.

Every expected value is an independent hand computation over the seeded scenario; SQL is never
mocked. as_of is FIXED_NOW = 2026-03-15T12:00:00Z (see conftest). Scenarios live on car 2, which
the shared seed leaves empty, so each test controls its own events, positions and states.
"""

from datetime import datetime

import pytest

CAR = 2
URL = f"/v1/vehicles/{CAR}/parking-observations"
AS_OF = "2026-03-15T12:00:00Z"


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


async def _pos(db, pid, date, *, bl=None, odo=None, rated=None):
    await db.execute(
        "INSERT INTO positions(id,car_id,date,latitude,longitude,battery_level,odometer,"
        "rated_battery_range_km) VALUES ($1,$2,$3,31.2,121.4,$4,$5,$6)",
        pid,
        CAR,
        date,
        bl,
        odo,
        rated,
    )


async def _drive(
    db,
    did,
    start,
    end,
    *,
    sbl,
    sobl,
    ebl,
    eobl,
    srated=None,
    erated=None,
    match_start=True,
    match_end=True,
    odo_null_end=False,
):
    """Insert a drive plus its two boundary positions (exact timestamps unless disabled)."""
    sp, ep = 2_000_000 + did * 10, 2_000_000 + did * 10 + 1
    start_pos_date = start if match_start else start.replace(minute=30)
    end_pos_date = end if match_end else start
    await _pos(db, sp, start_pos_date, bl=sbl, odo=sobl, rated=srated)
    await _pos(db, ep, end_pos_date, bl=ebl, odo=None if odo_null_end else eobl, rated=erated)
    await db.execute(
        "INSERT INTO drives(id,car_id,start_date,end_date,start_position_id,end_position_id) "
        "VALUES ($1,$2,$3,$4,$5,$6)",
        did,
        CAR,
        start,
        end,
        sp,
        ep,
    )
    return sp, ep


async def _charge(db, cid, start, end, *, sbl, ebl, odo_start, odo_end, srated=None, erated=None):
    """Charging session; start odometer from position_id, end odometer from an end_date position."""
    sp, ep = 2_100_000 + cid * 10, 2_100_000 + cid * 10 + 1
    await _pos(db, sp, start, bl=sbl, odo=odo_start, rated=srated)
    await _pos(db, ep, end, bl=ebl, odo=odo_end, rated=erated)
    await db.execute(
        "INSERT INTO charging_processes(id,car_id,position_id,start_date,end_date,"
        "start_battery_level,end_battery_level,start_rated_range_km,end_rated_range_km) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
        cid,
        CAR,
        sp,
        start,
        end,
        sbl,
        ebl,
        srated,
        erated,
    )


async def _state(db, sid, state, start, end):
    await db.execute(
        "INSERT INTO states(id,car_id,state,start_date,end_date) VALUES ($1,$2,$3,$4,$5)",
        sid,
        CAR,
        state,
        start,
        end,
    )


def _params(**overrides):
    base = {"from": "2026-03-10T00:00:00Z", "to": "2026-03-11T00:00:00Z", "tz": "UTC"}
    base.update(overrides)
    return base


async def test_valid_window_rate_states_open_tail_and_method(client, db):
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=78,
        eobl=1020.0,
        srated=300,
        erated=295,
    )
    await _drive(
        db,
        901,
        _dt("2026-03-10T11:00"),
        _dt("2026-03-10T12:00"),
        sbl=77,
        sobl=1020.2,
        ebl=75,
        eobl=1040.0,
        srated=294,
        erated=290,
    )
    await _state(db, 900, "online", _dt("2026-03-10T09:00"), _dt("2026-03-10T10:00"))
    await _state(db, 901, "asleep", _dt("2026-03-10T10:00"), _dt("2026-03-10T11:00"))
    body = (await client.get(URL, params=_params())).json()

    assert body["status"] == "available"
    assert body["method"] == "closed-window-soc-v1"
    assert body["version"] == 1
    assert body["validation"] == "provisional"
    assert body["thresholds"] == {
        "min_duration_hours": 1.0,
        "max_abs_odometer_change_km": 1.0,
        "odometer_change_bound": "exclusive",
        "soc_min_pp": 0,
        "soc_max_pp": 100,
        "endpoint_rule": "exact_event_boundary_position",
        "state_rule": "closed_segment_intersection_open_unknown",
    }
    assert body["query"] == {
        "vehicle_id": CAR,
        "from": "2026-03-10T00:00:00Z",
        "to": "2026-03-11T00:00:00Z",
        "tz": "UTC",
        "bucket": "day",
        "filters": {},
        "time_basis": "window_start",
    }
    assert body["as_of"] == AS_OF
    # One closed window [09:00,11:00] plus the open tail after the last drive.
    assert body["matched_windows"] == 2
    totals = body["totals"]
    assert totals["candidate_windows"] == 1
    assert totals["valid_windows"] == 1
    assert totals["excluded_windows"] == 0
    assert totals["open_windows"] == 1
    assert totals["observed_hours"] == 2.0
    assert totals["soc_change_pp"] == -1
    assert totals["soc_change_pp_per_24h"] == pytest.approx(-12.0)
    assert (totals["online_hours"], totals["asleep_hours"]) == (1.0, 1.0)
    assert (totals["offline_hours"], totals["unknown_hours"]) == (0.0, 0.0)
    assert totals["online_share"] == pytest.approx(0.5)
    assert totals["asleep_share"] == pytest.approx(0.5)
    assert totals["excluded_by_reason"] == {
        "same_moment_gap": 0,
        "invalid_time_order": 0,
        "event_overlap": 0,
        "ambiguous_states": 0,
        "missing_soc": 0,
        "missing_odometer": 0,
        "odometer_change": 0,
        "insufficient_duration": 0,
    }
    assert len(body["buckets"]) == 1
    bucket = body["buckets"][0]
    assert bucket["bucket_start"] == "2026-03-10T00:00:00Z"
    assert bucket["valid_windows"] == 1 and bucket["open_windows"] == 1

    # items sorted window_start DESC: open tail (12:00) first, then the closed window (09:00).
    open_tail, window = body["items"]
    assert open_tail["window_id"] == "drive:901~open"
    assert open_tail["included"] is False
    assert open_tail["quality"] == "open_window"
    assert open_tail["end_event"] is None
    assert open_tail["window_end"] == AS_OF
    assert open_tail["soc_change_pp"] is None
    assert open_tail["start"]["soc"] == 75

    assert window["window_id"] == "drive:900~drive:901"
    assert window["included"] is True
    assert window["quality"] == "complete"
    assert window["exclusion_reason"] is None
    assert window["start_event"] == {"type": "drive", "id": 900}
    assert window["end_event"] == {"type": "drive", "id": 901}
    assert window["window_start"] == "2026-03-10T09:00:00Z"
    assert window["window_end"] == "2026-03-10T11:00:00Z"
    assert window["duration_hours"] == 2.0
    assert window["soc_change_pp"] == -1
    assert window["soc_change_pp_per_24h"] == pytest.approx(-12.0)
    assert window["odometer_change_km"] == pytest.approx(0.2)
    assert window["negative_odometer_change"] is False
    assert window["start"] == {
        "soc": 78,
        "odometer_km": 1020.0,
        "rated_range_km": 295.0,
        "soc_source": "drive_end_position",
        "odometer_source": "drive_end_position",
        "position_id": 2_009_001,
        "raw_time": "2026-03-10T09:00:00Z",
    }
    assert window["end"] == {
        "soc": 77,
        "odometer_km": 1020.2,
        "rated_range_km": 294.0,
        "soc_source": "drive_start_position",
        "odometer_source": "drive_start_position",
        "position_id": 2_009_010,
        "raw_time": "2026-03-10T11:00:00Z",
    }


async def test_cross_query_boundary_uses_out_of_range_events_as_context_only(client, db):
    # A drive ending before `from` is context: its window (start < from) is never emitted.
    await _drive(
        db,
        900,
        _dt("2026-03-09T08:00"),
        _dt("2026-03-09T09:00"),
        sbl=80,
        sobl=900.0,
        ebl=79,
        eobl=920.0,
    )
    await _drive(
        db,
        901,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=78,
        sobl=1000.0,
        ebl=78,
        eobl=1000.0,
    )
    await _drive(
        db,
        902,
        _dt("2026-03-10T11:00"),
        _dt("2026-03-10T12:00"),
        sbl=77,
        sobl=1000.3,
        ebl=76,
        eobl=1020.0,
    )
    body = (await client.get(URL, params=_params(to="2026-03-10T10:00:00Z"))).json()
    assert body["matched_windows"] == 1
    assert body["totals"]["candidate_windows"] == 1
    assert body["totals"]["open_windows"] == 0  # last drive ends at 12:00, beyond `to`
    window = body["items"][0]
    assert window["window_id"] == "drive:901~drive:902"
    assert window["window_start"] == "2026-03-10T09:00:00Z"
    assert window["window_end"] == "2026-03-10T11:00:00Z"  # end may exceed `to`
    assert window["included"] is True


async def test_upper_bound_is_clipped_to_as_of_and_echoed(client, db):
    await _drive(
        db,
        900,
        _dt("2026-03-15T08:00"),
        _dt("2026-03-15T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=79,
        eobl=1020.0,
    )
    await _drive(
        db,
        901,
        _dt("2026-03-15T10:00"),
        _dt("2026-03-15T11:00"),
        sbl=78,
        sobl=1020.1,
        ebl=77,
        eobl=1040.0,
    )
    body = (
        await client.get(
            URL, params=_params(**{"from": "2026-03-15T00:00:00Z", "to": "2026-03-20T00:00:00Z"})
        )
    ).json()
    assert body["query"]["to"] == AS_OF  # effective upper bound clipped to as_of
    assert body["status"] == "available"
    window = next(w for w in body["items"] if w["end_event"])
    assert window["window_start"] == "2026-03-15T09:00:00Z"
    open_tail = next(w for w in body["items"] if w["end_event"] is None)
    assert open_tail["window_end"] == AS_OF  # open tail observed up to clipped as_of


@pytest.mark.parametrize(
    ("gap_start", "gap_end", "reason"),
    [
        ("2026-03-10T11:00", "2026-03-10T11:00", "same_moment_gap"),
        ("2026-03-10T12:00", "2026-03-10T11:00", "invalid_time_order"),
    ],
)
async def test_degenerate_gaps_excluded(client, db, gap_start, gap_end, reason):
    # First drive ends at gap_start; second drive starts at gap_end.
    await _drive(
        db, 900, _dt("2026-03-10T08:00"), _dt(gap_start), sbl=80, sobl=1000.0, ebl=78, eobl=1020.0
    )
    await _drive(
        db, 901, _dt(gap_end), _dt("2026-03-10T13:00"), sbl=77, sobl=1020.0, ebl=76, eobl=1040.0
    )
    body = (await client.get(URL, params=_params())).json()
    assert body["status"] == "insufficient_data"  # candidate but not valid
    assert body["totals"]["candidate_windows"] == 1
    assert body["totals"]["valid_windows"] == 0
    assert body["totals"]["excluded_by_reason"][reason] == 1
    assert body["totals"]["soc_change_pp"] is None  # never show 0 drain
    window = next(w for w in body["items"] if w["end_event"] is not None)
    assert window["included"] is False
    assert window["exclusion_reason"] == reason
    assert window["quality"] == "excluded"


async def test_event_overlap_and_in_progress_crossing_never_fabricate_long_window(client, db):
    # Sorted by start: 900(08:00), 910 charging in-progress(09:30), 901(10:00), 902(13:00).
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=79,
        eobl=1020.0,
    )
    await _pos(db, 2_099_990, _dt("2026-03-10T09:30"), bl=50, odo=1020.0)
    await db.execute(
        "INSERT INTO charging_processes(id,car_id,position_id,start_date,end_date,"
        "start_battery_level,end_battery_level) VALUES (910,$1,2099990,$2,NULL,50,NULL)",
        CAR,
        _dt("2026-03-10T09:30"),
    )
    await _drive(
        db,
        901,
        _dt("2026-03-10T10:00"),
        _dt("2026-03-10T11:00"),
        sbl=78,
        sobl=1020.1,
        ebl=77,
        eobl=1040.0,
    )
    await _drive(
        db,
        902,
        _dt("2026-03-10T13:00"),
        _dt("2026-03-10T14:00"),
        sbl=76,
        sobl=1040.2,
        ebl=75,
        eobl=1060.0,
    )
    body = (await client.get(URL, params=_params())).json()
    reasons = {w["window_id"]: w["exclusion_reason"] for w in body["items"] if w["end_event"]}
    # The in-progress session (910) is a boundary; the pair after it is skipped, so no fake
    # [09:00,13:00] window appears. Window 901->902 [11:00,13:00] is crossed by 910.
    assert "drive:900~drive:902" not in reasons
    assert reasons["drive:901~drive:902"] == "event_overlap"
    assert body["totals"]["excluded_by_reason"]["event_overlap"] >= 1
    assert body["totals"]["valid_windows"] == 0


async def test_missing_exact_endpoint_is_not_backfilled(client, db):
    # Window 900->901: drive 900 end position timestamp does not match the boundary => no SOC.
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=78,
        eobl=1020.0,
        match_end=False,
    )
    await _drive(
        db,
        901,
        _dt("2026-03-10T11:00"),
        _dt("2026-03-10T12:00"),
        sbl=77,
        sobl=1020.0,
        ebl=76,
        eobl=1040.0,
    )
    body = (await client.get(URL, params=_params())).json()
    window = next(w for w in body["items"] if w["end_event"])
    assert window["exclusion_reason"] == "missing_soc"
    assert window["start"]["soc"] is None
    assert window["start"]["soc_source"] is None
    assert body["totals"]["excluded_by_reason"]["missing_soc"] == 1


async def test_missing_odometer_when_endpoint_position_lacks_it(client, db):
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=78,
        eobl=1020.0,
        odo_null_end=True,
    )
    await _drive(
        db,
        901,
        _dt("2026-03-10T11:00"),
        _dt("2026-03-10T12:00"),
        sbl=77,
        sobl=1020.0,
        ebl=76,
        eobl=1040.0,
    )
    body = (await client.get(URL, params=_params())).json()
    window = next(w for w in body["items"] if w["end_event"])
    # SOC endpoints are trustworthy, so this is missing_odometer (priority after missing_soc).
    assert window["exclusion_reason"] == "missing_odometer"
    assert window["start"]["soc"] == 78
    assert window["start"]["odometer_km"] is None
    assert body["totals"]["excluded_by_reason"]["missing_odometer"] == 1


@pytest.mark.parametrize(
    ("end_odo", "reason", "negative"),
    [
        (1022.0, "odometer_change", False),  # +2.0 km >= 1 km threshold
        (1019.5, None, True),  # -0.5 km within threshold: kept, flagged
    ],
)
async def test_odometer_change_threshold_and_negative_diagnostic(
    client, db, end_odo, reason, negative
):
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=78,
        eobl=1020.0,
    )
    await _drive(
        db,
        901,
        _dt("2026-03-10T11:00"),
        _dt("2026-03-10T12:00"),
        sbl=77,
        sobl=end_odo,
        ebl=76,
        eobl=1040.0,
    )
    body = (await client.get(URL, params=_params())).json()
    window = next(w for w in body["items"] if w["end_event"])
    assert window["exclusion_reason"] == reason
    assert window["included"] is (reason is None)
    assert window["negative_odometer_change"] is negative
    assert window["odometer_change_km"] == pytest.approx(end_odo - 1020.0)


@pytest.mark.parametrize(
    ("next_start", "valid"),
    [("2026-03-10T10:00", True), ("2026-03-10T09:59", False)],
)
async def test_duration_exactly_one_hour_boundary(client, db, next_start, valid):
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=78,
        eobl=1020.0,
    )
    await _drive(
        db, 901, _dt(next_start), _dt("2026-03-10T12:00"), sbl=77, sobl=1020.1, ebl=76, eobl=1040.0
    )
    body = (await client.get(URL, params=_params())).json()
    window = next(w for w in body["items"] if w["end_event"])
    assert window["included"] is valid
    assert window["exclusion_reason"] is None if valid else "insufficient_duration"


async def test_soc_rise_fall_and_real_zero_total(client, db):
    # Two valid windows: +2pp then -2pp => real zero total change, still available.
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=70,
        sobl=1000.0,
        ebl=70,
        eobl=1020.0,
    )
    await _drive(
        db,
        901,
        _dt("2026-03-10T11:00"),
        _dt("2026-03-10T12:00"),
        sbl=72,
        sobl=1020.1,
        ebl=72,
        eobl=1040.0,
    )
    await _drive(
        db,
        902,
        _dt("2026-03-10T14:00"),
        _dt("2026-03-10T15:00"),
        sbl=70,
        sobl=1040.1,
        ebl=70,
        eobl=1060.0,
    )
    body = (await client.get(URL, params=_params())).json()
    by_id = {w["window_id"]: w for w in body["items"] if w["end_event"]}
    assert by_id["drive:900~drive:901"]["soc_change_pp"] == 2  # 70 -> 72 rise
    assert by_id["drive:901~drive:902"]["soc_change_pp"] == -2  # 72 -> 70 fall
    assert body["totals"]["valid_windows"] == 2
    assert body["totals"]["soc_change_pp"] == 0  # real zero, not null
    assert body["status"] == "available"


async def test_partial_state_coverage_and_open_segment_unknown(client, db):
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=78,
        eobl=1020.0,
    )
    await _drive(
        db,
        901,
        _dt("2026-03-10T13:00"),
        _dt("2026-03-10T14:00"),
        sbl=77,
        sobl=1020.1,
        ebl=76,
        eobl=1040.0,
    )
    # Window [09:00,13:00] = 4h. online [09:00,10:00], offline [10:00,11:00], open asleep 12:00->.
    await _state(db, 900, "online", _dt("2026-03-10T09:00"), _dt("2026-03-10T10:00"))
    await _state(db, 901, "offline", _dt("2026-03-10T10:00"), _dt("2026-03-10T11:00"))
    await _state(db, 902, "asleep", _dt("2026-03-10T12:00"), None)
    body = (await client.get(URL, params=_params())).json()
    window = next(w for w in body["items"] if w["end_event"])
    assert window["included"] is True
    assert window["quality"] == "partial_state_coverage"
    assert window["duration_hours"] == 4.0
    assert window["online_hours"] == 1.0
    assert window["offline_hours"] == 1.0
    assert window["asleep_hours"] == 0.0  # open segment never inferred into asleep
    # [11:00,12:00] gap + [12:00,13:00] open segment => 2h unknown.
    assert window["unknown_hours"] == 2.0
    assert body["totals"]["unknown_share"] == pytest.approx(0.5)


async def test_ambiguous_overlapping_states_excluded(client, db):
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=78,
        eobl=1020.0,
    )
    await _drive(
        db,
        901,
        _dt("2026-03-10T13:00"),
        _dt("2026-03-10T14:00"),
        sbl=77,
        sobl=1020.1,
        ebl=76,
        eobl=1040.0,
    )
    # Two closed segments overlap positively inside [09:00,13:00].
    await _state(db, 900, "online", _dt("2026-03-10T09:00"), _dt("2026-03-10T11:30"))
    await _state(db, 901, "asleep", _dt("2026-03-10T11:00"), _dt("2026-03-10T13:00"))
    body = (await client.get(URL, params=_params())).json()
    window = next(w for w in body["items"] if w["end_event"])
    assert window["exclusion_reason"] == "ambiguous_states"
    assert window["included"] is False
    assert body["totals"]["excluded_by_reason"]["ambiguous_states"] == 1


async def test_overall_rate_is_sum_over_sum_and_page_size_independent(client, db):
    # W1 [09:00,11:00] 2h change -1; W2 [12:00,16:00] 4h change -3.
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=78,
        eobl=1020.0,
    )
    await _drive(
        db,
        901,
        _dt("2026-03-10T11:00"),
        _dt("2026-03-10T12:00"),
        sbl=77,
        sobl=1020.1,
        ebl=76,
        eobl=1040.0,
    )
    await _drive(
        db,
        902,
        _dt("2026-03-10T16:00"),
        _dt("2026-03-10T17:00"),
        sbl=73,
        sobl=1040.1,
        ebl=72,
        eobl=1060.0,
    )
    expected_rate = (-1 + -3) / (2 + 4) * 24  # = -16, not the -15 average of per-window rates
    for limit in (1, 2, 50):
        body = (await client.get(URL, params=_params(limit=str(limit)))).json()
        assert body["totals"]["soc_change_pp"] == -4
        assert body["totals"]["observed_hours"] == 6.0
        assert body["totals"]["soc_change_pp_per_24h"] == pytest.approx(expected_rate)
    by_id = {w["window_id"]: w for w in (await client.get(URL, params=_params())).json()["items"]}
    assert by_id["drive:900~drive:901"]["soc_change_pp_per_24h"] == pytest.approx(-12.0)
    assert by_id["drive:901~drive:902"]["soc_change_pp_per_24h"] == pytest.approx(-18.0)


async def test_pagination_is_stable_and_totals_ignore_cursor(client, db):
    for index, hour in enumerate((8, 11, 14, 17)):
        end_hour = hour + 1
        await _drive(
            db,
            900 + index,
            _dt(f"2026-03-10T{hour:02}:00"),
            _dt(f"2026-03-10T{end_hour:02}:00"),
            sbl=80,
            sobl=1000.0 + index,
            ebl=78,
            eobl=1020.0 + index,
        )
    # Four drives => three 2h/3h gaps plus an open tail after the last drive.
    seen, cursor, pages = [], None, 0
    first_totals = None
    while True:
        params = _params(limit="1")
        if cursor:
            params["cursor"] = cursor
        body = (await client.get(URL, params=params)).json()
        pages += 1
        if first_totals is None:
            first_totals = body["totals"]
        assert body["totals"] == first_totals  # totals independent of cursor/page
        assert body["matched_windows"] == 4
        for item in body["items"]:
            assert item["window_id"] not in seen
            seen.append(item["window_id"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 4
    assert len(seen) == 4  # no duplicates, no gaps across pages


async def test_no_candidates_and_all_excluded_are_insufficient(client, db):
    # Single event: no adjacent pair => no candidate window.
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=78,
        eobl=1020.0,
    )
    empty = (await client.get(URL, params=_params(to="2026-03-10T09:30:00Z"))).json()
    assert empty["status"] == "insufficient_data"
    assert empty["totals"]["candidate_windows"] == 0
    assert empty["totals"]["open_windows"] == 1  # car parked after its only drive, still open
    assert empty["matched_windows"] == 1
    # A gap shorter than 1h is a candidate but never valid => insufficient_data.
    await _drive(
        db,
        901,
        _dt("2026-03-10T09:30"),
        _dt("2026-03-10T10:00"),
        sbl=77,
        sobl=1020.0,
        ebl=76,
        eobl=1040.0,
    )
    short = (await client.get(URL, params=_params())).json()
    assert short["status"] == "insufficient_data"
    assert short["totals"]["candidate_windows"] == 1
    assert short["totals"]["excluded_by_reason"]["insufficient_duration"] == 1


async def test_meta_declares_parking_capability(client):
    meta = (await client.get("/v1/meta")).json()
    assert meta["statistics_insights"]["features"]["parking_observations"] is True


async def test_missing_source_column_is_unsupported_not_empty(db, app):
    import httpx

    await db.execute("ALTER TABLE charging_processes DROP COLUMN end_rated_range_km")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        await client.post("/v1/auth/setup", json={"password": "fixture password"})
        login = (await client.post("/v1/auth/login", json={"password": "fixture password"})).json()
        client.headers["Authorization"] = "Bearer " + login["token"]
        meta = (await client.get("/v1/meta")).json()
        assert meta["statistics_insights"]["features"]["parking_observations"] is False
        body = (await client.get(URL, params=_params())).json()
        assert body["status"] == "unsupported"
        assert body["matched_windows"] == 0
        assert body["items"] == []


async def test_business_query_budget_is_bounded_and_not_per_window(app, client, db, database_dsn):
    import asyncio

    import asyncpg

    from mileaf.dependencies import get_pool

    for index, hour in enumerate((8, 11, 14, 17, 20)):
        await _drive(
            db,
            900 + index,
            _dt(f"2026-03-10T{hour:02}:00"),
            _dt(f"2026-03-10T{hour + 1:02}:00"),
            sbl=80,
            sobl=1000.0,
            ebl=78,
            eobl=1020.0,
        )
    queries: list[str] = []

    async def record(connection):
        connection.add_query_logger(lambda q: queries.append(q.query))

    pool = await asyncpg.create_pool(
        database_dsn,
        min_size=1,
        max_size=1,
        init=record,
        server_settings={"default_transaction_read_only": "on"},
    )
    app.dependency_overrides[get_pool] = lambda: pool
    try:
        await client.get(URL, params=_params())
        await asyncio.sleep(0)
        queries.clear()
        response = await client.get(URL, params=_params())
        await asyncio.sleep(0)
        assert response.status_code == 200
        selects = [q for q in queries if q.lstrip().upper().startswith(("SELECT", "WITH"))]
        # One events+endpoints projection and one states projection; never one query per window.
        events_q = [q for q in selects if "FROM drives d" in q]
        states_q = [q for q in selects if "FROM states s" in q]
        assert len(events_q) == 1
        assert len(states_q) == 1
        assert len(events_q) + len(states_q) <= 4  # business query budget
        assert "charging_processes cp" in events_q[0]  # both event kinds in one statement
    finally:
        app.dependency_overrides.pop(get_pool)
        await pool.close()


@pytest.mark.parametrize(
    "params",
    [
        {"to": "2026-03-11T00:00:00Z"},  # missing from
        {"from": "2026-03-10T00:00:00Z"},  # missing to
        {"from": "2026-03-10T00:00:00", "to": "2026-03-11T00:00:00Z"},  # naive from
        {"from": "2026-03-11T00:00:00Z", "to": "2026-03-10T00:00:00Z"},  # from >= to
        {"from": "2026-03-10T00:00:00Z", "to": "2028-03-11T00:00:00Z"},  # > 730 days
        {"from": "2026-03-16T00:00:00Z", "to": "2026-03-17T00:00:00Z"},  # empty after clip
        {"from": "2026-03-10T00:00:00Z", "to": "2026-03-11T00:00:00Z", "tz": "Mars/Olympus"},
        {"from": "2026-03-10T00:00:00Z", "to": "2026-03-11T00:00:00Z", "bucket": "year"},
        {"from": "2026-03-10T00:00:00Z", "to": "2026-03-11T00:00:00Z", "limit": "0"},
        {"from": "2026-03-10T00:00:00Z", "to": "2026-03-11T00:00:00Z", "limit": "101"},
    ],
)
async def test_invalid_parameters_are_400(client, params):
    assert (await client.get(URL, params=params)).status_code == 400


async def test_cursor_is_bound_to_request_context(client, db):
    for index, hour in enumerate((8, 11, 14)):
        await _drive(
            db,
            900 + index,
            _dt(f"2026-03-10T{hour:02}:00"),
            _dt(f"2026-03-10T{hour + 1:02}:00"),
            sbl=80,
            sobl=1000.0,
            ebl=78,
            eobl=1020.0,
        )
    page = (await client.get(URL, params=_params(limit="1"))).json()
    cursor = page["next_cursor"]
    assert cursor is not None
    # Same context, different page size: cursor still valid (limit is not bound).
    assert (await client.get(URL, params=_params(limit="2", cursor=cursor))).status_code == 200
    # Changed context (bucket / tz / requested_to) invalidates the cursor.
    assert (
        await client.get(URL, params=_params(limit="1", cursor=cursor, bucket="week"))
    ).status_code == 400
    assert (
        await client.get(URL, params=_params(limit="1", cursor=cursor, tz="America/New_York"))
    ).status_code == 400
    assert (
        await client.get(URL, params=_params(limit="1", cursor=cursor, to="2026-03-12T00:00:00Z"))
    ).status_code == 400


async def test_conditional_request_returns_304(client, db):
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=78,
        eobl=1020.0,
    )
    await _drive(
        db,
        901,
        _dt("2026-03-10T11:00"),
        _dt("2026-03-10T12:00"),
        sbl=77,
        sobl=1020.1,
        ebl=76,
        eobl=1040.0,
    )
    first = await client.get(URL, params=_params())
    etag = first.headers["etag"]
    assert etag.startswith("W/")
    second = await client.get(URL, params=_params(), headers={"If-None-Match": etag})
    assert second.status_code == 304


async def test_requires_auth_and_existing_vehicle(client):
    assert (
        await client.get(URL, params=_params(), headers={"Authorization": ""})
    ).status_code == 401
    assert (
        await client.get("/v1/vehicles/999/parking-observations", params=_params())
    ).status_code == 404
    assert (
        await client.get("/v1/vehicles/40000/parking-observations", params=_params())
    ).status_code == 404


async def test_week_month_buckets_and_timezone_local_midnight(client, db):
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=78,
        eobl=1020.0,
    )
    await _drive(
        db,
        901,
        _dt("2026-03-10T11:00"),
        _dt("2026-03-10T12:00"),
        sbl=77,
        sobl=1020.1,
        ebl=76,
        eobl=1040.0,
    )
    week = (await client.get(URL, params=_params(bucket="week"))).json()
    assert week["buckets"][0]["bucket_start"] == "2026-03-09T00:00:00Z"  # local Monday
    month = (await client.get(URL, params=_params(bucket="month"))).json()
    assert month["buckets"][0]["bucket_start"] == "2026-03-01T00:00:00Z"
    # tz=America/New_York (EDT, UTC-4 on 2026-03-10): local midnight is 04:00Z.
    ny = (
        await client.get(
            URL,
            params=_params(
                **{
                    "from": "2026-03-10T00:00:00Z",
                    "to": "2026-03-12T00:00:00Z",
                    "tz": "America/New_York",
                }
            ),
        )
    ).json()
    starts = [b["bucket_start"] for b in ny["buckets"] if b["valid_windows"] == 1]
    assert starts == ["2026-03-10T04:00:00Z"]


async def test_charging_endpoints_resolve_exactly_and_never_reuse_start_position(client, db):
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=78,
        eobl=1020.0,
        erated=295,
    )
    # Charging 11:00->12:00: start position odometer 1020.5, distinct end position odometer 1020.6.
    await _charge(
        db,
        910,
        _dt("2026-03-10T11:00"),
        _dt("2026-03-10T12:00"),
        sbl=77,
        ebl=90,
        odo_start=1020.5,
        odo_end=1020.6,
        srated=300,
        erated=400,
    )
    await _drive(
        db,
        901,
        _dt("2026-03-10T14:00"),
        _dt("2026-03-10T15:00"),
        sbl=89,
        sobl=1020.7,
        ebl=88,
        eobl=1040.0,
    )
    body = (await client.get(URL, params=_params())).json()
    by_id = {w["window_id"]: w for w in body["items"] if w["end_event"]}

    # Window drive->charging: end endpoint is the charging START.
    w1 = by_id["drive:900~charging_session:910"]
    assert w1["included"] is True
    assert w1["end"]["soc"] == 77
    assert w1["end"]["soc_source"] == "charging_start_battery_level"
    assert w1["end"]["odometer_km"] == pytest.approx(1020.5)
    assert w1["end"]["odometer_source"] == "charging_start_position"
    assert w1["end"]["rated_range_km"] == pytest.approx(300.0)
    assert w1["soc_change_pp"] == -1  # 78 -> 77

    # Window charging->drive: start endpoint is the charging END, odometer from the end_date
    # position (1020.6), never the start position (1020.5).
    w2 = by_id["charging_session:910~drive:901"]
    assert w2["included"] is True
    assert w2["start"]["soc"] == 90
    assert w2["start"]["soc_source"] == "charging_end_battery_level"
    assert w2["start"]["odometer_km"] == pytest.approx(1020.6)
    assert w2["start"]["odometer_source"] == "charging_end_position"
    assert w2["start"]["rated_range_km"] == pytest.approx(400.0)
    assert w2["soc_change_pp"] == -1  # 90 -> 89
    assert w2["odometer_change_km"] == pytest.approx(0.1)


async def test_charging_end_without_exact_position_is_missing_odometer(client, db):
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=78,
        eobl=1020.0,
    )
    # Charging with a start position but NO position at end_date => end odometer unresolved.
    await _pos(db, 2_109_100, _dt("2026-03-10T11:00"), bl=77, odo=1020.5)
    await db.execute(
        "INSERT INTO charging_processes(id,car_id,position_id,start_date,end_date,"
        "start_battery_level,end_battery_level) VALUES (910,$1,2109100,$2,$3,77,90)",
        CAR,
        _dt("2026-03-10T11:00"),
        _dt("2026-03-10T12:00"),
    )
    await _drive(
        db,
        901,
        _dt("2026-03-10T14:00"),
        _dt("2026-03-10T15:00"),
        sbl=89,
        sobl=1020.7,
        ebl=88,
        eobl=1040.0,
    )
    body = (await client.get(URL, params=_params())).json()
    by_id = {w["window_id"]: w for w in body["items"] if w["end_event"]}
    # The charging->drive window cannot resolve the charging END odometer (start position is not
    # substituted), so it is excluded missing_odometer even though both SOCs are present.
    w2 = by_id["charging_session:910~drive:901"]
    assert w2["exclusion_reason"] == "missing_odometer"
    assert w2["start"]["soc"] == 90
    assert w2["start"]["odometer_km"] is None
    assert body["totals"]["excluded_by_reason"]["missing_odometer"] == 1


async def test_charging_end_odometer_uses_highest_id_position_at_end_date(client, db):
    await _drive(
        db,
        900,
        _dt("2026-03-10T08:00"),
        _dt("2026-03-10T09:00"),
        sbl=80,
        sobl=1000.0,
        ebl=78,
        eobl=1020.0,
    )
    # Charging end position (id 2109101) carries a decoy odometer; a higher-id position at the
    # exact end_date (id 2109105) must win per the "multiple => highest id" rule.
    await _charge(
        db,
        910,
        _dt("2026-03-10T11:00"),
        _dt("2026-03-10T12:00"),
        sbl=77,
        ebl=90,
        odo_start=1020.5,
        odo_end=9999.0,
    )
    await _pos(db, 2_109_105, _dt("2026-03-10T12:00"), bl=90, odo=1020.6)
    await _drive(
        db,
        901,
        _dt("2026-03-10T14:00"),
        _dt("2026-03-10T15:00"),
        sbl=89,
        sobl=1020.7,
        ebl=88,
        eobl=1040.0,
    )
    body = (await client.get(URL, params=_params())).json()
    by_id = {w["window_id"]: w for w in body["items"] if w["end_event"]}
    w2 = by_id["charging_session:910~drive:901"]
    assert w2["start"]["odometer_km"] == pytest.approx(1020.6)  # highest id, not the 9999.0 decoy
    assert w2["start"]["position_id"] == 2_109_105
    assert w2["included"] is True


async def test_nested_states_started_before_window_are_ambiguous(client, db):
    await _drive(
        db,
        990,
        _dt("2026-03-10T01:00"),
        _dt("2026-03-10T03:00"),
        sbl=80,
        sobl=100,
        ebl=79,
        eobl=110,
    )
    await _drive(
        db,
        991,
        _dt("2026-03-10T04:00"),
        _dt("2026-03-10T05:00"),
        sbl=78,
        sobl=110,
        ebl=77,
        eobl=120,
    )
    await _state(db, 990, "online", _dt("2026-03-10T00:00"), _dt("2026-03-10T10:00"))
    await _state(db, 991, "asleep", _dt("2026-03-10T01:00"), _dt("2026-03-10T02:00"))
    await _state(db, 992, "offline", _dt("2026-03-10T02:30"), _dt("2026-03-10T04:00"))
    result = (await client.get(URL, params=_params())).json()
    closed = next(item for item in result["items"] if item["end_event"] is not None)
    assert closed["exclusion_reason"] == "ambiguous_states"
    assert result["totals"]["valid_windows"] == 0
