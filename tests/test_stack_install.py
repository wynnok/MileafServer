"""Installer checks without installing host software or contacting Tesla."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy/install.sh"
SECRET_KEYS = (
    "POSTGRES_PASSWORD",
    "MILEAF_READER_PASSWORD",
    "GRAFANA_READER_PASSWORD",
    "ENCRYPTION_KEY",
    "GRAFANA_ADMIN_PASSWORD",
    "MILEAF_ACCESS_PASSWORD",
)


def run(*args, env=None, input=None):
    return subprocess.run(
        args, cwd=ROOT, env=env, input=input, capture_output=True, text=True, timeout=60
    )


def install(directory, *options, env=None):
    return run(
        "bash",
        str(INSTALLER),
        "--data-dir",
        str(directory),
        "--non-interactive",
        *options,
        env=env,
    )


def read_config(directory):
    return dict(line.split("=", 1) for line in (directory / ".env").read_text().splitlines())


@pytest.mark.parametrize("mqtt", [False, True])
@pytest.mark.parametrize("grafana", [False, True])
def test_all_component_combinations_render_with_isolated_network(tmp_path, mqtt, grafana):
    result = install(
        tmp_path,
        "--prepare-only",
        "--mqtt" if mqtt else "--no-mqtt",
        "--grafana" if grafana else "--no-grafana",
    )
    assert result.returncode == 0, result.stderr
    config = read_config(tmp_path)
    for key in SECRET_KEYS:
        assert config[key] not in result.stdout + result.stderr
    assert len({config[key] for key in SECRET_KEYS}) == len(SECRET_KEYS)
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    profiles = [name for name, active in (("mqtt", mqtt), ("grafana", grafana)) if active]
    environment = {
        **os.environ,
        **config,
        "MILEAF_SOURCE_DIR": str(ROOT),
        "DISABLE_MQTT": "false" if mqtt else "true",
        "COMPOSE_PROFILES": ",".join(profiles),
    }
    try:
        rendered = run(
            "docker",
            "compose",
            "--env-file",
            str(tmp_path / ".env"),
            "-f",
            str(ROOT / "deploy/stack.compose.yml"),
            "config",
            "--format",
            "json",
            env=environment,
        )
    except FileNotFoundError:
        pytest.skip("Docker Compose is needed for configuration validation")
    assert rendered.returncode == 0  # Do not include expanded secrets in assertion output.
    model = json.loads(rendered.stdout)
    services = model["services"]
    expected = {"database", "teslamate", "mileaf"}
    if mqtt:
        expected.add("mosquitto")
    if grafana:
        expected.add("grafana")
    assert set(services) == expected
    assert all(set(service["networks"]) == {"default"} for service in services.values())
    assert "ports" not in services["database"]
    assert "ports" not in services.get("mosquitto", {})
    assert services["teslamate"]["environment"]["DISABLE_MQTT"] == str(not mqtt).lower()
    for name in expected - {"database", "mosquitto"}:
        assert services[name]["ports"][0]["host_ip"] == "127.0.0.1"
    assert "mileaf_reader:" in services["mileaf"]["environment"]["MILEAF_DATABASE_DSN"]


def test_rerun_preserves_identity_secrets_and_saved_choices(tmp_path):
    assert install(tmp_path, "--prepare-only", "--mqtt", "--grafana").returncode == 0
    original = read_config(tmp_path)
    assert install(tmp_path, "--prepare-only").returncode == 0
    assert read_config(tmp_path) == original
    assert install(tmp_path, "--prepare-only", "--no-mqtt", "--mileaf-port", "8081").returncode == 0
    changed = read_config(tmp_path)
    assert changed["MQTT_ENABLED"] == "false"
    assert changed["GRAFANA_ENABLED"] == "true"
    for key in (*SECRET_KEYS, "COMPOSE_PROJECT_NAME"):
        assert changed[key] == original[key]


@pytest.mark.parametrize(
    "options",
    [
        ["--bind", "999.0.0.1"],
        ["--mileaf-port", "65536"],
        ["--mileaf-port", "4000"],
        ["--grafana", "--grafana-port", "8080"],
        ["--data-dir"],
        ["--wat"],
    ],
)
def test_bad_arguments_do_not_create_configuration(tmp_path, options):
    assert install(tmp_path, "--prepare-only", *options).returncode != 0
    assert not (tmp_path / ".env").exists()


def test_config_is_not_executable_and_incomplete_config_is_not_replaced(tmp_path):
    marker = tmp_path / "must-not-exist"
    malicious = f"COMPOSE_PROJECT_NAME=$(touch {marker})\n"
    (tmp_path / ".env").write_text(malicious)
    result = install(tmp_path, "--prepare-only")
    assert result.returncode != 0
    assert not marker.exists()
    assert (tmp_path / ".env").read_text() == malicious


@pytest.mark.parametrize("enabled", [False, True])
def test_install_orders_migration_permissions_auth_before_start(tmp_path, enabled):
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    # Exercise the orchestration only; do not install software or launch containers.
    docker.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args == ["info", "--format", "{{.Architecture}}"]:
    print("aarch64")
if args == ["compose", "up", "--help"]:
    print("--wait-timeout")
if "compose" in args and "run" in args and "mileaf" in args:
    assert len(sys.stdin.read()) == 64
"""
    )
    docker.chmod(0o700)
    log = tmp_path / "commands.jsonl"
    environment = {**os.environ, "PATH": f"{binary}:{os.environ['PATH']}", "COMMAND_LOG": str(log)}
    result = install(
        tmp_path / "config",
        "--mqtt" if enabled else "--no-mqtt",
        "--grafana" if enabled else "--no-grafana",
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    migration = next(i for i, cmd in enumerate(commands) if "/bin/true" in cmd)
    permissions = next(i for i, cmd in enumerate(commands) if "/bootstrap/stack-reader.sql" in cmd)
    initialization = next(i for i, cmd in enumerate(commands) if "--entrypoint" in cmd)
    startup = next(i for i, cmd in enumerate(commands) if "up" in cmd and "mileaf" in cmd)
    assert migration < permissions < initialization < startup
    stopped = {cmd[-1] for cmd in commands if "stop" in cmd}
    assert stopped == (set() if enabled else {"mosquitto", "grafana"})
    for key in SECRET_KEYS:
        assert read_config(tmp_path / "config")[key] not in log.read_text()


async def test_stack_reader_bootstrap_is_repeatable_and_restricts_permissions(db, admin_dsn):
    # Only the isolated synthetic PostgreSQL fixture is touched.
    docker_dsn = admin_dsn.replace("127.0.0.1", "host.docker.internal")
    try:
        for _ in range(2):
            result = run(
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--volume",
                f"{ROOT / 'deploy'}:/bootstrap:ro",
                "--env",
                "MILEAF_READER_PASSWORD=synthetic-test-password",
                "--env",
                "GRAFANA_READER_PASSWORD=synthetic-grafana-password",
                "postgres:16-alpine",
                "psql",
                "-X",
                "-q",
                docker_dsn,
                "-f",
                "/bootstrap/stack-reader.sql",
            )
            assert result.returncode == 0, result.stderr
        assert await db.fetchval(
            "SELECT has_table_privilege('mileaf_reader', 'public.positions', 'SELECT')"
        )
        assert not await db.fetchval(
            "SELECT has_table_privilege('mileaf_reader', 'private.tokens', 'SELECT')"
        )
        assert not await db.fetchval(
            "SELECT has_table_privilege('mileaf_reader', 'public.positions', 'INSERT')"
        )
        assert await db.fetchval(
            "SELECT has_table_privilege('grafana_reader', 'public.car_settings', 'SELECT')"
        )
        assert not await db.fetchval(
            "SELECT has_table_privilege('grafana_reader', 'private.tokens', 'SELECT')"
        )
        await db.execute("GRANT UPDATE ON public.positions TO mileaf_reader")
        rejected = run(
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--volume",
            f"{ROOT / 'deploy'}:/bootstrap:ro",
            "--env",
            "MILEAF_READER_PASSWORD=synthetic-test-password",
            "--env",
            "GRAFANA_READER_PASSWORD=synthetic-grafana-password",
            "postgres:16-alpine",
            "psql",
            "-X",
            "-q",
            docker_dsn,
            "-f",
            "/bootstrap/stack-reader.sql",
        )
        assert rejected.returncode != 0
        assert "unexpected privileges" in rejected.stderr
    finally:
        await db.execute("DROP OWNED BY mileaf_reader")
        await db.execute("DROP ROLE mileaf_reader")
        await db.execute("DROP OWNED BY grafana_reader")
        await db.execute("DROP ROLE grafana_reader")
