"""Rewrite the frozen OpenAPI snapshot from the runtime schema.

    UV_CACHE_DIR=.cache/uv uv run python scripts/freeze_openapi.py

The contract is additive inside /v1, so the snapshot carries the current server
version and the previous freeze stays on disk as the released surface.
"""

import asyncio
import json
import tempfile
from importlib.metadata import version
from pathlib import Path

import httpx

from mileaf.app import create_app
from mileaf.config import Settings

ROOT = Path(__file__).resolve().parents[1]


async def runtime_schema() -> dict[str, object]:
    """Read /openapi.json through the public HTTP seam; no database is contacted."""
    with tempfile.TemporaryDirectory() as state:
        app = create_app(
            Settings(
                database_dsn="postgresql://fixture@127.0.0.1:1/unused",
                state_file=Path(state) / "state.json",
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/openapi.json")
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]


def main() -> Path:
    schema = asyncio.run(runtime_schema())
    declared = version("mileaf-server")
    if schema["info"]["version"] != declared:  # type: ignore[index]
        raise SystemExit(f"runtime {schema['info']['version']} != installed {declared}")
    target = ROOT / "docs" / "api" / f"openapi-{declared}.json"
    target.write_text(json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return target


if __name__ == "__main__":
    written = main()
    print(f"froze {written.relative_to(ROOT)}")
