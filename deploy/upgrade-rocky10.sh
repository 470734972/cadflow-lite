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

RELEASE="$APP_ROOT/releases/$(date +%Y%m%d%H%M%S)"; PREVIOUS=$(readlink -f "$APP_ROOT/current")
install -d -m 0755 "$RELEASE"
tar --exclude=.git --exclude=.venv --exclude=.pytest_cache --exclude=data -C "$SOURCE_DIR" -cf - . | tar -C "$RELEASE" -xf -
python3 -m venv "$RELEASE/.venv"
"$RELEASE/.venv/bin/pip" install --upgrade pip -i "$PIP_INDEX_URL"
"$RELEASE/.venv/bin/pip" install "$RELEASE" -i "$PIP_INDEX_URL"
"$RELEASE/.venv/bin/python" -m compileall -q "$RELEASE/app"
chmod 0755 "$RELEASE/deploy/cadflow-serve"

systemctl stop cadflow-lite
if [[ -f "$DATA_DIR/cadflow.db" ]]; then cp -a "$DATA_DIR/cadflow.db" "$DATA_DIR/backups/cadflow-$(date +%Y%m%d%H%M%S).db"; fi
ln -s "$RELEASE" "$APP_ROOT/current.new"; mv -Tf "$APP_ROOT/current.new" "$APP_ROOT/current"
if systemctl start cadflow-lite && sleep 2 && curl --fail --silent http://127.0.0.1:8080/api/health >/dev/null; then
  echo "Upgraded to $RELEASE"
else
  echo "Upgrade failed; rolling back to $PREVIOUS" >&2
  ln -s "$PREVIOUS" "$APP_ROOT/current.rollback"; mv -Tf "$APP_ROOT/current.rollback" "$APP_ROOT/current"
  systemctl start cadflow-lite || true
  exit 1
fi
