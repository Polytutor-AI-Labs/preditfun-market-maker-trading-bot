#!/usr/bin/env bash

set -Eeuo pipefail

INSTALL_DIR="${PREDICT_MM_INSTALL_DIR:-/opt/predict-mm-bot}"
SERVICE_NAME="predict-mm-bot"
SERVICE_USER="predictmm"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
LEGACY_DIR="${PREDICT_MM_LEGACY_DIR:-/root/predict-mm-bot}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() {
  printf '\n\033[1;34m[predict-mm]\033[0m %s\n' "$*"
}

fail() {
  printf '\n\033[1;31m[predict-mm] 安装失败：\033[0m%s\n' "$*" >&2
  exit 1
}

trap 'fail "第 ${LINENO} 行执行失败。请保留完整输出以便排查。"' ERR

if [[ "${EUID}" -ne 0 ]]; then
  fail "请使用 README 中带 sudo 的一键安装命令。"
fi

if ! command -v apt-get >/dev/null 2>&1; then
  fail "一键脚本目前支持 Ubuntu/Debian 服务器，推荐 Ubuntu 24.04。"
fi

if ! command -v systemctl >/dev/null 2>&1; then
  fail "服务器未使用 systemd，无法创建后台网页服务。"
fi

log "安装系统组件"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl procps python3 python3-pip python3-venv tar

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  fail "Python 版本低于 3.11。请使用 Ubuntu 24.04，或先升级 Python。"
fi

CURRENT_STATUS="$(curl --silent --show-error --max-time 3 http://127.0.0.1:8080/api/status 2>/dev/null || true)"
if [[ "${CURRENT_STATUS}" == *'"running":true'* ]]; then
  fail "检测到机器人正在运行。请先在网页点击“停止并撤单”，再重新执行安装命令。"
fi

if [[ -n "${CURRENT_STATUS}" ]] && ! systemctl is-active --quiet "${SERVICE_NAME}"; then
  LEGACY_STOPPED=false
  while read -r process_id; do
    [[ -n "${process_id}" ]] || continue
    process_dir="$(readlink -f "/proc/${process_id}/cwd" 2>/dev/null || true)"
    if [[ "${process_dir}" == "${LEGACY_DIR}" || "${process_dir}" == "${INSTALL_DIR}" ]]; then
      log "停止旧的手动网页服务"
      kill "${process_id}"
      for _ in {1..15}; do
        if ! kill -0 "${process_id}" 2>/dev/null; then
          LEGACY_STOPPED=true
          break
        fi
        sleep 1
      done
    fi
  done < <(pgrep -f 'python.*-m predict_mm\.web' || true)
  if [[ "${LEGACY_STOPPED}" != true ]] || curl --silent --max-time 2 \
    http://127.0.0.1:8080/api/status >/dev/null 2>&1; then
    fail "端口 8080 上已有无法安全识别的服务。请先停止旧服务，再重新执行安装命令。"
  fi
fi

if systemctl is-active --quiet "${SERVICE_NAME}"; then
  log "停止旧版网页服务"
  systemctl stop "${SERVICE_NAME}"
fi

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${INSTALL_DIR}"
if [[ "${SOURCE_DIR}" != "${INSTALL_DIR}" ]]; then
  log "安装本地程序"
  tar -C "${SOURCE_DIR}" \
    --exclude='.venv' \
    --exclude='.git' \
    --exclude='logs' \
    --exclude='__pycache__' \
    --exclude='.env' \
    --exclude='config.toml' \
    -cf - . | tar -C "${INSTALL_DIR}" -xf -
fi
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${INSTALL_DIR}/logs"

if [[ "${LEGACY_DIR}" != "${INSTALL_DIR}" ]]; then
  if [[ ! -f "${INSTALL_DIR}/.env" && -f "${LEGACY_DIR}/.env" ]]; then
    log "迁移旧安装中的账户设置"
    cp -p "${LEGACY_DIR}/.env" "${INSTALL_DIR}/.env"
  fi
  if [[ ! -f "${INSTALL_DIR}/config.toml" && -f "${LEGACY_DIR}/config.toml" ]]; then
    log "迁移旧安装中的市场设置"
    cp -p "${LEGACY_DIR}/config.toml" "${INSTALL_DIR}/config.toml"
  fi
fi

log "创建 Python 环境并安装依赖"
if [[ ! -x "${INSTALL_DIR}/.venv/bin/python" ]]; then
  python3 -m venv "${INSTALL_DIR}/.venv"
fi
"${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade --editable "${INSTALL_DIR}"

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
chmod 0750 "${INSTALL_DIR}"
[[ ! -f "${INSTALL_DIR}/.env" ]] || chmod 0600 "${INSTALL_DIR}/.env"
[[ ! -f "${INSTALL_DIR}/config.toml" ]] || chmod 0600 "${INSTALL_DIR}/config.toml"

log "创建后台网页服务"
cat >"${SERVICE_FILE}" <<EOF
[Unit]
Description=Predict.fun Market Maker Web Console
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${INSTALL_DIR}/.venv/bin/python -m predict_mm.web --host 127.0.0.1 --port 8080
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=${INSTALL_DIR}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

log "检查网页服务"
for _ in {1..20}; do
  if curl --fail --silent --max-time 2 http://127.0.0.1:8080/api/status >/dev/null; then
    printf '\n\033[1;32m安装完成。\033[0m\n'
    printf '程序目录：%s\n' "${INSTALL_DIR}"
    printf '网页服务：已启动，并已设置为服务器重启后自动恢复。\n'
    printf '机器人状态：不会自动启动，请进入网页确认配置后手动启动。\n\n'
    printf '在自己的电脑建立 SSH 隧道：\n'
    printf '  ssh -L 8080:127.0.0.1:8080 用户名@服务器IP\n\n'
    printf '然后浏览器打开：http://127.0.0.1:8080\n'
    exit 0
  fi
  sleep 1
done

systemctl --no-pager --full status "${SERVICE_NAME}" || true
fail "网页服务未能在 20 秒内启动，请查看上方状态信息。"
