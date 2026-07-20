#!/usr/bin/env bash
# CADFlow Lite laboratory installer for Rocky Linux 10.x.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run: sudo bash deploy/install-rocky10.sh" >&2
  exit 1
fi

SOURCE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
APP_DIR=/opt/cadflow
DATA_DIR=/var/lib/cadflow
CONFIG_DIR=/etc/cadflow
SERVICE_USER=${SUDO_USER:-cadflow}
PIP_INDEX_URL=${CADFLOW_PIP_INDEX_URL:-https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple}

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required" >&2; exit 1; }
id "$SERVICE_USER" >/dev/null 2>&1 || { echo "service user does not exist: $SERVICE_USER" >&2; exit 1; }

install -d -m 0755 "$APP_DIR" "$CONFIG_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$DATA_DIR"
cp -a "$SOURCE_DIR"/. "$APP_DIR"/
chown -R root:root "$APP_DIR"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$DATA_DIR"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -i "$PIP_INDEX_URL"
"$APP_DIR/.venv/bin/pip" install "$APP_DIR" -i "$PIP_INDEX_URL"

if [[ ! -f "$CONFIG_DIR/cadflow.env" ]]; then
  cat >"$CONFIG_DIR/cadflow.env" <<EOF
CADFLOW_MODE=demo
CADFLOW_CLUSTER_NAME=eda-lab
CADFLOW_DB_PATH=$DATA_DIR/cadflow.db
CADFLOW_BIND_HOST=0.0.0.0
CADFLOW_PORT=8080
CADFLOW_ADMIN_TOKEN=$(openssl rand -hex 32)
EOF
  chown root:"$SERVICE_USER" "$CONFIG_DIR/cadflow.env"
  chmod 0640 "$CONFIG_DIR/cadflow.env"
fi

sed "s/^User=.*/User=$SERVICE_USER/; s/^Group=.*/Group=$SERVICE_USER/" "$APP_DIR/deploy/cadflow-lite.service" > /etc/systemd/system/cadflow-lite.service
chmod 0755 "$APP_DIR/deploy/cadflow-serve"
systemctl daemon-reload
systemctl enable --now cadflow-lite

if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port=8080/tcp
  firewall-cmd --reload
fi

sleep 2
curl --fail --silent http://127.0.0.1:8080/api/health >/dev/null
echo "CADFlow is ready: http://$(hostname -I | awk '{print $1}'):8080"
echo "Open the 配置 page to enter LSF and FlexNet parameters."
