"""One completed firmware update."""

import asyncpg


async def seed(db: asyncpg.Connection) -> None:
    await db.execute("""
        INSERT INTO updates(id,car_id,start_date,end_date,version)
        VALUES (1,1,'2026-02-15 08:00','2026-02-15 08:30','2026.2.3')
        ON CONFLICT DO NOTHING
    """)
