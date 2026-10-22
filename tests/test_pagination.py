"""Pagination contracts at the shared database primitive seam."""

from datetime import UTC, datetime

import pytest

from mileaf.errors import APIError
from mileaf.queries.pagination import fetch_page

DRIVES = "SELECT id, start_date AT TIME ZONE 'UTC' AS started_at FROM drives WHERE car_id=$1"


async def test_pages_cover_all_drives_and_ignore_newer_insert(db):
    page = await fetch_page(db, source=DRIVES, args=(1,))
    ids = [r["id"] for r in page.rows]
    assert len(ids) == 20
    await db.execute("INSERT INTO drives(id,car_id,start_date) VALUES (100,1,'2026-04-01')")
    while page.next_cursor:
        page = await fetch_page(db, source=DRIVES, args=(1,), cursor=page.next_cursor)
        ids.extend(r["id"] for r in page.rows)
    assert len(ids) == 50
    assert set(ids) == set(range(1, 51))
    assert page.next_cursor is None


async def test_half_open_bounds_empty_results_and_tied_timestamps(db):
    await db.execute("UPDATE drives SET start_date='2026-03-15 08:00' WHERE id=2")
    options = dict(
        source=DRIVES,
        args=(1,),
        limit=1,
        from_=datetime(2026, 3, 15, 8, tzinfo=UTC),
        to=datetime(2026, 3, 15, 9, tzinfo=UTC),
    )
    page = await fetch_page(db, **options)
    assert [r["id"] for r in page.rows] == [2]
    page = await fetch_page(db, **options, cursor=page.next_cursor)
    assert [r["id"] for r in page.rows] == [1]
    assert page.next_cursor is None
    empty = await fetch_page(db, source=DRIVES, args=(2,))
    assert empty.rows == []
    assert empty.next_cursor is None


@pytest.mark.parametrize("limit", [0, 101, "abc", "2.5"])
async def test_invalid_limit(db, limit):
    with pytest.raises(APIError) as error:
        await fetch_page(db, source=DRIVES, args=(1,), limit=limit)
    assert error.value.status_code == 400


@pytest.mark.parametrize("cursor", ["!", "", "YWJj", "MjAyNi0wMS0wMXwx", "bm90LWRhdGV8MQ"])
async def test_invalid_cursor(db, cursor):
    with pytest.raises(APIError) as error:
        await fetch_page(db, source=DRIVES, args=(1,), cursor=cursor)
    assert error.value.status_code == 400
    assert error.value.message == "Invalid cursor"
