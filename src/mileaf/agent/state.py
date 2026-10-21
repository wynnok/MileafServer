from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class AgentState:
    agent_token: str | None = None
    tenant_id: str | None = None
    data_source_id: str | None = None
    last_drive_start: str | None = None
    last_charge_start: str | None = None
    last_full_sync_at: str | None = None
    last_sync_at: str | None = None

    @property
    def last_drive_start_at(self) -> datetime | None:
        return _parse_datetime(self.last_drive_start)

    @property
    def last_charge_start_at(self) -> datetime | None:
        return _parse_datetime(self.last_charge_start)

    @property
    def last_full_sync_datetime(self) -> datetime | None:
        return _parse_datetime(self.last_full_sync_at)


def load_state(path: str, fallback_token: str | None = None) -> AgentState:
    file_path = Path(path)
    if not file_path.exists():
        return AgentState(agent_token=fallback_token)
    data = json.loads(file_path.read_text(encoding="utf-8"))
    state = AgentState(**data)
    if not state.agent_token:
        state.agent_token = fallback_token
    return state


def save_state(path: str, state: AgentState) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = file_path.with_suffix(f"{file_path.suffix}.tmp")
    temporary.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(file_path)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
