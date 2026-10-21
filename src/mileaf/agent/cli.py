from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from mileaf.agent.client import AgentAuthError, MileafApiError, MileafClient
from mileaf.agent.config import AgentSettings
from mileaf.agent.source import (
    TeslaMateSource,
    TeslaMateSourceError,
    ensure_supported_schema,
)
from mileaf.agent.state import AgentState, load_state, save_state
from mileaf.agent.sync import sync_once

logger = logging.getLogger("mileaf.agent")


def main() -> None:
    parser = argparse.ArgumentParser(prog="mileaf-agent")
    parser.add_argument("command", choices=["run", "pair"], nargs="?", default="run")
    parser.add_argument("--once", action="store_true", help="run one sync and exit")
    args = parser.parse_args()

    settings = AgentSettings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        if args.command == "pair":
            asyncio.run(_pair(settings))
        elif args.once:
            asyncio.run(_run_once(settings))
        else:
            asyncio.run(_run_forever(settings))
    except KeyboardInterrupt:
        logger.info("agent stopped")
    except (AgentAuthError, MileafApiError, TeslaMateSourceError, RuntimeError) as exc:
        logger.error("%s", exc)
        sys.exit(1)


async def _pair(settings: AgentSettings) -> None:
    if not settings.pairing_code:
        raise RuntimeError("MILEAF_AGENT_PAIRING_CODE is required for pairing")

    state = load_state(settings.state_file, settings.agent_token)
    async with TeslaMateSource(settings.asyncpg_dsn) as source:
        schema_version = await source.schema_version()
        ensure_supported_schema(schema_version)
        async with MileafClient(
            settings.mileaf_url, timeout_seconds=settings.request_timeout_seconds
        ) as client:
            paired = await client.pair(
                code=settings.pairing_code,
                name=settings.source_name,
                source_version=settings.teslamate_version or "unknown",
                schema_version=schema_version,
            )

    state.agent_token = paired["agent_token"]
    state.tenant_id = paired["tenant_id"]
    state.data_source_id = paired["data_source_id"]
    save_state(settings.state_file, state)
    logger.info("paired successfully; data_source_id=%s", state.data_source_id)


async def _run_once(settings: AgentSettings) -> None:
    state = load_state(settings.state_file, settings.agent_token)
    report = await sync_once(settings, state=state)
    logger.info(
        "sync finished: vehicles=%d drives=%d charges=%d full=%s",
        report.vehicles,
        report.drives,
        report.charges,
        report.full_sync,
    )


async def _run_forever(settings: AgentSettings) -> None:
    state = load_state(settings.state_file, settings.agent_token)
    while True:
        try:
            report = await sync_once(settings, state=state)
            logger.info(
                "sync finished: vehicles=%d drives=%d charges=%d full=%s",
                report.vehicles,
                report.drives,
                report.charges,
                report.full_sync,
            )
        except AgentAuthError:
            if not settings.pairing_code:
                logger.error("agent token rejected and no pairing code is configured")
                raise
            logger.warning("agent token rejected; attempting to re-pair")
            state = AgentState()
            save_state(settings.state_file, state)
            continue
        except Exception:
            logger.exception("sync failed; retrying in 60 seconds")
            await asyncio.sleep(60)
            continue
        await asyncio.sleep(settings.interval_seconds)
