#!/usr/bin/env bash
set -Eeuo pipefail

# Pull the configured upstream and restart the ordinary-user deployment.
# This intentionally does not use sudo or systemd.
APP_DIR=${CADFLOW_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}

[[ ${EUID} -ne 0 ]] || {
  echo "Run this script as the normal CADFlow account; sudo/systemd is not used" >&2
  exit 1
}
command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 1; }
[[ -d "$APP_DIR/.git" ]] || { echo "Not a Git checkout: $APP_DIR" >&2; exit 1; }

cd "$APP_DIR"
git pull --ff-only

# Always restart after pulling so the new source and configuration guard are
# loaded, while preserving the existing venv, SQLite data and logs.
exec bash "$APP_DIR/deploy/install-user.sh" --force-restart "$@"
