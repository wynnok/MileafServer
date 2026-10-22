"""Named geofences and address fallback cases."""

import asyncpg


async def seed(db: asyncpg.Connection) -> None:
    await db.execute("""
        INSERT INTO geofences(id,name,latitude,longitude,radius,inserted_at,updated_at)
        VALUES (1,'家',31.2304,121.4737,100,'2026-01-01','2026-01-01'),
               (2,'公司',31.2404,121.4837,100,'2026-01-01','2026-01-01')
        ON CONFLICT DO NOTHING;
        INSERT INTO addresses(id,display_name,osm_id,osm_type,latitude,longitude,road,
                              house_number,city,raw,inserted_at,updated_at)
        VALUES (1,'测试路 10 号',1,'way',31.23,121.47,'测试路','10','上海','{}',
                '2026-01-01','2026-01-01'),
               (2,'上海',2,'relation',31.24,121.48,NULL,NULL,'上海','{}',
                '2026-01-01','2026-01-01')
        ON CONFLICT DO NOTHING;
    """)
