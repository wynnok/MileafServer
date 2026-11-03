#!/usr/bin/env bash
# Run from a checkout; never pipe this script into a shell (it needs deploy files).
set -Eeuo pipefail
umask 077

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
STATE_DIR="$ROOT/.deployment"
MQTT_CHOICE= GRAFANA_CHOICE= BIND_CHOICE=
TM_PORT_CHOICE= MILEAF_PORT_CHOICE= GRAFANA_PORT_CHOICE=
NON_INTERACTIVE=false PREPARE_ONLY=false
DOCKER=(docker)
# Nonempty prefixes also work with macOS Bash 3.2 and nounset.
DOCKER_PREFIX=(env)
SUDO=(env)

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
usage() {
    cat <<'EOF'
用法：bash deploy/install.sh [选项]
  --mqtt / --no-mqtt          安装 / 不安装 MQTT（首次默认不安装）
  --grafana / --no-grafana    安装 / 不安装 Grafana（首次默认不安装）
  --non-interactive          无交互，使用指定选项或已保存的选择
  --bind ADDRESS             HTTP 服务绑定 IPv4 地址，默认 127.0.0.1
  --teslamate-port PORT      默认 4000
  --mileaf-port PORT         默认 8080
  --grafana-port PORT        默认 3000
  --data-dir DIR             配置目录，默认仓库内 .deployment（数据存于 Docker 卷）
  --prepare-only             仅生成配置，不安装 Docker、不启动容器
  -h / --help               显示帮助

新装 Docker：Ubuntu/Debian + systemd，使用官方 apt 仓库，需要 root/sudo。
其他系统须已有可用 Docker Engine/Desktop 和 Compose v2.20+。
重复执行复用密码、项目名和数据卷；取消可选组件会停止其容器并保留数据。
EOF
}
value_required() { [[ $# -ge 2 && -n $2 && $2 != --* ]] || die "$1 缺少参数"; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mqtt) MQTT_CHOICE=true ;;
        --no-mqtt) MQTT_CHOICE=false ;;
        --grafana) GRAFANA_CHOICE=true ;;
        --no-grafana) GRAFANA_CHOICE=false ;;
        --non-interactive) NON_INTERACTIVE=true ;;
        --prepare-only) PREPARE_ONLY=true ;;
        --data-dir) value_required "$@"; STATE_DIR=$2; shift ;;
        --bind) value_required "$@"; BIND_CHOICE=$2; shift ;;
        --teslamate-port) value_required "$@"; TM_PORT_CHOICE=$2; shift ;;
        --mileaf-port) value_required "$@"; MILEAF_PORT_CHOICE=$2; shift ;;
        --grafana-port) value_required "$@"; GRAFANA_PORT_CHOICE=$2; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知选项：$1" ;;
    esac
    shift
done
trap 'printf "部署未完成（行 %s）。已保留配置和数据卷，修复原因后重跑；不会自动删除数据。\n" "$LINENO" >&2' ERR

random_secret() { od -An -N32 -tx1 /dev/urandom | tr -d ' \n'; }
ask_bool() {
    local label=$1 current=$2 answer
    if [[ $NON_INTERACTIVE == true || ! -t 0 ]]; then printf '%s' "$current"; return; fi
    while true; do
        printf '%s？[y/n，回车保留 %s] ' "$label" "$current" >&2
        read -r answer
        case "$answer" in
            y|Y|yes|YES) printf true; return ;;
            n|N|no|NO) printf false; return ;;
            '') printf '%s' "$current"; return ;;
        esac
    done
}

# Parse only our known literal keys. Never source/eval an editable .env file.
KEYS='COMPOSE_PROJECT_NAME MQTT_ENABLED GRAFANA_ENABLED BIND_ADDRESS TESLAMATE_PORT MILEAF_PORT GRAFANA_PORT POSTGRES_PASSWORD MILEAF_READER_PASSWORD GRAFANA_READER_PASSWORD ENCRYPTION_KEY GRAFANA_ADMIN_PASSWORD MILEAF_ACCESS_PASSWORD'
for key in $KEYS; do unset "$key"; done
if [[ -f "$STATE_DIR/.env" ]]; then
    while IFS='=' read -r key value || [[ -n $key ]]; do
        [[ -z $key || $key == \#* ]] && continue
        case " $KEYS " in
            *" $key "*) printf -v "$key" '%s' "$value" ;;
            *) die '配置包含未知字段，请使用原始部署配置' ;;
        esac
    done < "$STATE_DIR/.env"
    for key in $KEYS; do [[ -n ${!key:-} ]] || die "配置缺少 ${key}，拒绝重新生成已有部署密钥"; done
else
    COMPOSE_PROJECT_NAME="mileaf-$(random_secret | cut -c1-12)"
    MQTT_ENABLED=false GRAFANA_ENABLED=false BIND_ADDRESS=127.0.0.1
    TESLAMATE_PORT=4000 MILEAF_PORT=8080 GRAFANA_PORT=3000
    POSTGRES_PASSWORD=$(random_secret)
    MILEAF_READER_PASSWORD=$(random_secret)
    GRAFANA_READER_PASSWORD=$(random_secret)
    ENCRYPTION_KEY=$(random_secret)
    GRAFANA_ADMIN_PASSWORD=$(random_secret)
    MILEAF_ACCESS_PASSWORD=$(random_secret)
fi
[[ $COMPOSE_PROJECT_NAME =~ ^mileaf-[a-f0-9]{12}$ ]] || die '项目名格式无效'
for key in POSTGRES_PASSWORD MILEAF_READER_PASSWORD GRAFANA_READER_PASSWORD ENCRYPTION_KEY GRAFANA_ADMIN_PASSWORD MILEAF_ACCESS_PASSWORD; do
    [[ ${!key} =~ ^[a-f0-9]{64}$ ]] || die "$key 必须保留为脚本生成的 64 位十六进制值"
done
MQTT_ENABLED=${MQTT_CHOICE:-$(ask_bool '安装 MQTT（Mosquitto）' "$MQTT_ENABLED")}
GRAFANA_ENABLED=${GRAFANA_CHOICE:-$(ask_bool '安装 Grafana' "$GRAFANA_ENABLED")}
for key in MQTT_ENABLED GRAFANA_ENABLED; do
    [[ ${!key} == true || ${!key} == false ]] || die "$key 必须为 true 或 false"
done
BIND_ADDRESS=${BIND_CHOICE:-$BIND_ADDRESS}
TESLAMATE_PORT=${TM_PORT_CHOICE:-$TESLAMATE_PORT}
MILEAF_PORT=${MILEAF_PORT_CHOICE:-$MILEAF_PORT}
GRAFANA_PORT=${GRAFANA_PORT_CHOICE:-$GRAFANA_PORT}
[[ $BIND_ADDRESS =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die '--bind 需要 IPv4 地址'
IFS=. read -r -a octets <<< "$BIND_ADDRESS"
for octet in "${octets[@]}"; do (( 10#$octet <= 255 )) || die 'IPv4 地址无效'; done
for key in TESLAMATE_PORT MILEAF_PORT GRAFANA_PORT; do
    [[ ${!key} =~ ^[1-9][0-9]{0,4}$ ]] && (( ${!key} <= 65535 )) || die "$key 端口无效"
done
[[ $TESLAMATE_PORT != "$MILEAF_PORT" ]] || die 'TeslaMate 与 Mileaf 端口不能相同'
if [[ $GRAFANA_ENABLED == true ]]; then
    [[ $GRAFANA_PORT != "$TESLAMATE_PORT" && $GRAFANA_PORT != "$MILEAF_PORT" ]] || die 'Grafana 端口不能与其他服务相同'
fi

mkdir -p -- "$STATE_DIR"
STATE_DIR=$(cd -- "$STATE_DIR" && pwd)
[[ ! -L "$STATE_DIR/.env" ]] || die '拒绝覆盖符号链接配置文件'
ENV_TMP=$(mktemp "$STATE_DIR/.env.XXXXXX")
trap 'rm -f -- "$ENV_TMP"' EXIT
for key in $KEYS; do printf '%s=%s\n' "$key" "${!key}"; done > "$ENV_TMP"
chmod 600 "$ENV_TMP"
mv -f -- "$ENV_TMP" "$STATE_DIR/.env"
trap - EXIT
printf '配置已保存：%s/.env（权限 0600；请备份）\n' "$STATE_DIR"
[[ $PREPARE_ONLY == false ]] || exit 0

as_root() {
    if [[ $EUID -ne 0 && ${SUDO[0]} != sudo ]]; then
        command -v sudo >/dev/null || die '安装/启动 Docker 需要 root 或 sudo'
        sudo -v || die '无法获取 sudo 权限'
        SUDO=(sudo)
    fi
    "${SUDO[@]}" "$@"
}
install_docker() {
    [[ $(uname -s) == Linux && -f /etc/os-release ]] || die '此系统请先安装并启动 Docker Desktop：https://docs.docker.com/desktop/'
    local ID VERSION_CODENAME architecture package
    . /etc/os-release
    case "$ID" in ubuntu|debian) ;; *) die '自动安装仅支持 Ubuntu/Debian；其他发行版请先安装 Docker 和 Compose' ;; esac
    [[ ${VERSION_CODENAME:-} =~ ^[a-z]+$ ]] || die '无法确定发行版代号'
    [[ -d /run/systemd/system ]] || die '自动安装需要 systemd；WSL 请先启用 systemd 或 Docker Desktop 集成'
    architecture=$(dpkg --print-architecture)
    case "$architecture" in amd64|arm64) ;; *) die 'TeslaMate 需要 amd64 或 arm64' ;; esac
    # Do not silently uninstall container runtimes used by other applications.
    for package in docker.io docker-compose docker-doc podman-docker containerd runc; do
        if [[ $(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true) == 'install ok installed' ]]; then
            die "已有 ${package}；请先按 Docker 官方指南解决包冲突，脚本不会卸载现有运行时"
        fi
    done
    printf '通过 Docker 官方 apt 仓库安装 Engine、Buildx 和 Compose…\n'
    as_root apt-get update
    as_root apt-get install -y ca-certificates curl
    as_root install -m 0755 -d /etc/apt/keyrings
    as_root curl -fsSL --retry 3 "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
    as_root chmod a+r /etc/apt/keyrings/docker.asc
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
        "$architecture" "$ID" "$VERSION_CODENAME" | as_root tee /etc/apt/sources.list.d/docker.list >/dev/null
    as_root apt-get update
    as_root apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    as_root systemctl enable --now docker
}
command -v docker >/dev/null || install_docker
if ! docker info >/dev/null 2>&1; then
    [[ $(uname -s) == Linux ]] || die '请启动 Docker Desktop 后重跑'
    # Existing rootless/remote contexts must not silently switch to another daemon.
    [[ -z ${DOCKER_HOST:-} && -z ${DOCKER_CONTEXT:-} && $(docker context show) == default ]] || die '当前 Docker context 不可用，请先修复连接'
    as_root docker info >/dev/null 2>&1 || as_root systemctl start docker
    DOCKER=("${SUDO[@]}" docker)
    DOCKER_PREFIX=("${SUDO[@]}")
    "${DOCKER[@]}" info >/dev/null 2>&1 || die 'Docker daemon 不可用'
fi
"${DOCKER[@]}" compose version >/dev/null 2>&1 || die '缺少 Compose v2 插件，请安装 docker-compose-plugin 后重跑'
"${DOCKER[@]}" compose up --help | grep -q -- --wait-timeout || die 'Compose 版本过旧，需要 v2.20+'
case $("${DOCKER[@]}" info --format '{{.Architecture}}') in x86_64|amd64|aarch64|arm64) ;; *) die 'TeslaMate 需要 amd64 或 arm64 Docker 主机' ;; esac

DISABLE_MQTT=true COMPOSE_PROFILES=
SERVICES=(database teslamate mileaf)
PULL_SERVICES=(database teslamate)
if [[ $MQTT_ENABLED == true ]]; then
    DISABLE_MQTT=false COMPOSE_PROFILES=mqtt
    SERVICES+=(mosquitto); PULL_SERVICES+=(mosquitto)
fi
if [[ $GRAFANA_ENABLED == true ]]; then
    COMPOSE_PROFILES="${COMPOSE_PROFILES:+$COMPOSE_PROFILES,}grafana"
    SERVICES+=(grafana); PULL_SERVICES+=(grafana)
fi
compose() {
    local compose_env=("MILEAF_SOURCE_DIR=$ROOT" "DISABLE_MQTT=$DISABLE_MQTT" "COMPOSE_PROFILES=$COMPOSE_PROFILES" "BUILDX_CONFIG=$ROOT/.cache/buildx") key
    for key in $KEYS; do compose_env+=("$key=${!key}"); done
    # Pass only deployment settings across sudo, rather than preserving all env.
    "${DOCKER_PREFIX[@]}" env "${compose_env[@]}" docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file "$STATE_DIR/.env" \
        -f "$ROOT/deploy/stack.compose.yml" "$@"
}
compose config --quiet
compose pull --quiet "${PULL_SERVICES[@]}"
compose build mileaf
compose up -d --wait --wait-timeout 180 database
printf '等待 TeslaMate 官方入口完成数据库迁移…\n'
# Upstream entrypoint migrates before exec; /bin/true then exits successfully.
compose run --rm --no-deps teslamate /bin/true
compose exec -T database psql -X -q -v ON_ERROR_STOP=1 -U teslamate -d teslamate -f /bootstrap/stack-reader.sql

printf '初始化 Mileaf 访问口令（已有口令会保留）…\n'
printf '%s' "$MILEAF_ACCESS_PASSWORD" | compose run --rm -T --no-deps --entrypoint python mileaf -c '
import sys
from datetime import UTC, datetime
from pathlib import Path
from mileaf.auth import AuthStore
from mileaf.errors import APIError, ErrorCode
try:
    AuthStore(Path("/data/state.json")).setup(sys.stdin.read(), datetime.now(UTC))
except APIError as exc:
    if exc.code != ErrorCode.ALREADY_INITIALIZED:
        raise
'
# Explicitly stop disabled profiles; normal up does not stop previously enabled services.
[[ $MQTT_ENABLED == true ]] || compose --profile mqtt stop mosquitto
[[ $GRAFANA_ENABLED == true ]] || compose --profile grafana stop grafana
compose up -d --wait --wait-timeout 240 "${SERVICES[@]}"
compose exec -T mileaf python -c '
import time, urllib.error, urllib.request
for attempt in range(60):
    try:
        with urllib.request.urlopen("http://teslamate:4000", timeout=3) as response:
            assert response.status == 200
        break
    except (OSError, urllib.error.URLError):
        time.sleep(2)
else:
    raise SystemExit("TeslaMate HTTP 启动超时，请检查 teslamate 容器日志")
'
compose ps
printf '\n部署完成。项目：%s\nTeslaMate：http://%s:%s\nMileaf：http://%s:%s\n' \
    "$COMPOSE_PROJECT_NAME" "$BIND_ADDRESS" "$TESLAMATE_PORT" "$BIND_ADDRESS" "$MILEAF_PORT"
if [[ $GRAFANA_ENABLED == true ]]; then printf 'Grafana：http://%s:%s（用户 admin）\n' "$BIND_ADDRESS" "$GRAFANA_PORT"; fi
printf '访问口令在 %s/.env 的 MILEAF_ACCESS_PASSWORD；Grafana 密码为 GRAFANA_ADMIN_PASSWORD。\n' "$STATE_DIR"
printf '请通过 TeslaMate 页面完成车辆授权；脚本不收集 Tesla 凭据。远程接入见 docs/one-click-installation.md。\n'
