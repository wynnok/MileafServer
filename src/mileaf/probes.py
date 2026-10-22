"""Enumerable capability requirements and read-only startup validation."""

from dataclasses import dataclass

import asyncpg

# PostgreSQL udt_name, so enum renames between releases do not affect text casts.
CAPABILITIES: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {
    "meta": {
        "schema_migrations": {"version": ("int8",)},
        "settings": {
            "unit_of_length": ("unit_of_length",),
            "preferred_range": ("range", "preferred_range"),
        },
        "positions": {"date": ("timestamp",)},
        "states": {"start_date": ("timestamp",)},
        "cars": {"id": ("int2",)},
    },
}
TESTED_SCHEMA_MIN = TESTED_SCHEMA_MAX = 20260808090000


@dataclass(frozen=True)
class Compatibility:
    teslamate_schema_version: int | None
    tested: bool


async def probe_database(pool: asyncpg.Pool) -> Compatibility:
    columns = await pool.fetch(
        "SELECT table_name, column_name, udt_name FROM information_schema.columns "
        "WHERE table_schema='public'"
    )
    available = {(r["table_name"], r["column_name"]): r["udt_name"] for r in columns}
    missing = set()
    for tables in CAPABILITIES.values():
        for table, required in tables.items():
            for column, types in required.items():
                actual = available.get((table, column))
                if actual not in types:
                    missing.add(f"{table}.{column} (expected {'/'.join(types)})")
    if missing:
        raise RuntimeError(
            "Incompatible TeslaMate schema: "
            + ", ".join(sorted(missing))
            + ". Restore required tables/columns and reader SELECT grants, "
            "or use a supported TeslaMate version; do not create empty replacement tables."
        )
    # Check privileges via pg_class OID: private schema USAGE is intentionally not required.
    readable = await pool.fetchval(
        "SELECT has_table_privilege(current_user, c.oid, 'SELECT') "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='private' AND c.relname='tokens'"
    )
    if readable is None:
        raise RuntimeError("Missing private.tokens. Restore the supported TeslaMate schema.")
    if readable:
        raise RuntimeError(
            "Credential table is readable. REVOKE SELECT ON TABLE private.tokens FROM "
            '"YOUR_READER_ROLE"; also revoke inherited/PUBLIC grants. '
            "Use a non-owner, non-superuser reader role."
        )
    schema_version = await pool.fetchval("SELECT max(version) FROM schema_migrations")
    return Compatibility(
        schema_version,
        schema_version is not None and TESTED_SCHEMA_MIN <= schema_version <= TESTED_SCHEMA_MAX,
    )
