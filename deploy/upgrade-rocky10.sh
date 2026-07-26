#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=/opt/cadflow-lite
DATA_DIR=/var/lib/cadflow-lite
PIP_INDEX_URL=${CADFLOW_PIP_INDEX_URL:-https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple}
GITEE_URL=https://gitee.com/raychade/cadflow-lite.git
SOURCE_DIR=
usage(){ echo "Usage: sudo bash deploy/upgrade-rocky10.sh [--source DIR] [--ref BRANCH_OR_TAG]"; }
REF=master
while [[ $# -gt 0 ]]; do case "$1" in --source) SOURCE_DIR=$2; shift 2;; --ref) REF=$2; shift 2;; -h|--help) usage; exit 0;; *) usage >&2; exit 2;; esac; done
[[ ${EUID} -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
command -v python3 >/dev/null && command -v systemctl >/dev/null && command -v curl >/dev/null && command -v tar >/dev/null || {
  echo "python3, systemctl, curl and tar are required" >&2
  exit 1
}
exec 9>"/run/cadflow-lite-upgrade.lock"; flock -n 9 || { echo "Another CADFlow operation is running" >&2; exit 1; }

TMPDIR=
cleanup(){ [[ -n $TMPDIR ]] && rm -rf "$TMPDIR"; }
trap cleanup EXIT
if [[ -z $SOURCE_DIR ]]; then
  command -v git >/dev/null || { echo "git is required when --source is omitted" >&2; exit 1; }
  TMPDIR=$(mktemp -d); git clone --depth 1 --branch "$REF" "$GITEE_URL" "$TMPDIR/source"; SOURCE_DIR="$TMPDIR/source"
fi
[[ -f "$SOURCE_DIR/pyproject.toml" ]] || { echo "Invalid source directory: $SOURCE_DIR" >&2; exit 1; }
[[ -L "$APP_ROOT/current" ]] || { echo "CADFlow is not installed; run install-rocky10.sh first" >&2; exit 1; }

install -d -m 0755 "$APP_ROOT/releases" "$DATA_DIR/backups"
RELEASE=$(mktemp -d "$APP_ROOT/releases/$(date +%Y%m%d%H%M%S).XXXXXX")
PREVIOUS=$(readlink -f "$APP_ROOT/current")
tar --exclude=.git --exclude=.venv --exclude=.pytest_cache --exclude=data -C "$SOURCE_DIR" -cf - . | tar -C "$RELEASE" -xf -
python3 -m venv "$RELEASE/.venv"
"$RELEASE/.venv/bin/pip" install --upgrade pip -i "$PIP_INDEX_URL"
"$RELEASE/.venv/bin/pip" install "$RELEASE" -i "$PIP_INDEX_URL"
"$RELEASE/.venv/bin/python" -m compileall -q "$RELEASE/app"
chmod 0755 "$RELEASE/deploy/cadflow-serve"
chmod 0755 "$RELEASE/deploy/cadflow-update"

systemctl stop cadflow-lite
if [[ -f "$DATA_DIR/cadflow.db" ]]; then cp -a "$DATA_DIR/cadflow.db" "$DATA_DIR/backups/cadflow-$(date +%Y%m%d%H%M%S).db"; fi
ln -sfn "$RELEASE" "$APP_ROOT/current.new"; mv -Tf "$APP_ROOT/current.new" "$APP_ROOT/current"
systemctl daemon-reload
HEALTHY=0
if systemctl start cadflow-lite; then
  for _ in {1..20}; do
    if curl --fail --silent http://127.0.0.1:8080/api/health >/dev/null; then
      HEALTHY=1
      break
    fi
    sleep 1
  done
fi
if [[ $HEALTHY -eq 1 ]]; then
  install -m 0755 "$RELEASE/deploy/cadflow-update" /usr/local/sbin/cadflow-update
  echo "Upgraded to $RELEASE"
  echo "Health check: http://127.0.0.1:8080/api/health OK"
else
  echo "Upgrade failed; rolling back to $PREVIOUS" >&2
  systemctl stop cadflow-lite || true
  ln -sfn "$PREVIOUS" "$APP_ROOT/current.rollback"; mv -Tf "$APP_ROOT/current.rollback" "$APP_ROOT/current"
  systemctl daemon-reload
  systemctl start cadflow-lite || true
  exit 1
fi
