#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=/opt/cadflow-lite
DATA_DIR=/var/lib/cadflow-lite
CONFIG_DIR=/etc/cadflow-lite
SERVICE_USER=cadflow
PIP_INDEX_URL=${CADFLOW_PIP_INDEX_URL:-https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple}
SOURCE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

usage(){ echo "Usage: sudo bash deploy/install-rocky10.sh [--source DIR] [--service-user USER] [--no-firewall]"; }
while [[ $# -gt 0 ]]; do case "$1" in
  --source) SOURCE_DIR=$2; shift 2;;
  --service-user) SERVICE_USER=$2; shift 2;;
  --no-firewall) NO_FIREWALL=1; shift;;
  -h|--help) usage; exit 0;;
  *) usage >&2; exit 2;;
esac; done

[[ ${EUID} -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
[[ -f "$SOURCE_DIR/pyproject.toml" && -f "$SOURCE_DIR/deploy/cadflow-lite.service" ]] || { echo "Invalid source directory: $SOURCE_DIR" >&2; exit 1; }
command -v python3 >/dev/null && command -v systemctl >/dev/null && command -v curl >/dev/null || { echo "python3, systemctl and curl are required" >&2; exit 1; }

exec 9>"/run/cadflow-lite-install.lock"; flock -n 9 || { echo "Another CADFlow operation is running" >&2; exit 1; }
id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home /nonexistent --shell /sbin/nologin "$SERVICE_USER"
install -d -m 0755 "$APP_ROOT/releases" "$CONFIG_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$DATA_DIR" "$DATA_DIR/backups"

RELEASE="$APP_ROOT/releases/$(date +%Y%m%d%H%M%S)"
install -d -m 0755 "$RELEASE"
tar --exclude=.git --exclude=.venv --exclude=.pytest_cache --exclude=data -C "$SOURCE_DIR" -cf - . | tar -C "$RELEASE" -xf -
python3 -m venv "$RELEASE/.venv"
"$RELEASE/.venv/bin/pip" install --upgrade pip -i "$PIP_INDEX_URL"
"$RELEASE/.venv/bin/pip" install "$RELEASE" -i "$PIP_INDEX_URL"
"$RELEASE/.venv/bin/python" -m compileall -q "$RELEASE/app"
chmod 0755 "$RELEASE/deploy/cadflow-serve"

if [[ ! -f "$CONFIG_DIR/cadflow.env" ]]; then
  sed "s|^CADFLOW_DB_PATH=.*|CADFLOW_DB_PATH=$DATA_DIR/cadflow.db|" "$RELEASE/deploy/cadflow-lsf.env.example" > "$CONFIG_DIR/cadflow.env"
  sed -i "s|^CADFLOW_ADMIN_TOKEN=.*|CADFLOW_ADMIN_TOKEN=$(openssl rand -hex 32)|" "$CONFIG_DIR/cadflow.env"
  chown root:"$SERVICE_USER" "$CONFIG_DIR/cadflow.env"; chmod 0640 "$CONFIG_DIR/cadflow.env"
fi
sed "s/^User=.*/User=$SERVICE_USER/; s/^Group=.*/Group=$SERVICE_USER/" "$RELEASE/deploy/cadflow-lite.service" > /etc/systemd/system/cadflow-lite.service
ln -s "$RELEASE" "$APP_ROOT/current.new"; mv -Tf "$APP_ROOT/current.new" "$APP_ROOT/current"
systemctl daemon-reload
systemctl enable --now cadflow-lite
if [[ -z ${NO_FIREWALL:-} ]] && command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then firewall-cmd --permanent --add-port=8080/tcp; firewall-cmd --reload; fi
sleep 2; curl --fail --silent http://127.0.0.1:8080/api/health >/dev/null
echo "Installed $(readlink -f "$APP_ROOT/current")"
echo "Open: http://$(hostname -I | awk '{print $1}'):8080"
