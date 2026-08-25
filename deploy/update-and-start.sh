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

project_version() {
  sed -n 's/^version = "\([^"]*\)"/\1/p' "$APP_DIR/pyproject.toml" | head -n 1
}

OLD_COMMIT=$(git rev-parse HEAD)
OLD_VERSION=$(project_version)
echo "CADFlow update: $APP_DIR"
echo "Before: v${OLD_VERSION:-unknown} (${OLD_COMMIT:0:7})"
if ! git pull --ff-only --quiet; then
  echo "Update failed: Git fast-forward was not possible" >&2
  exit 1
fi
NEW_COMMIT=$(git rev-parse HEAD)
NEW_VERSION=$(project_version)
if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
  echo "Source: already up to date"
else
  echo "Source: v${NEW_VERSION:-unknown} (${NEW_COMMIT:0:7})"
fi

# Always restart after pulling so the new source and configuration guard are
# loaded, while preserving the existing venv, SQLite data and logs.
exec bash "$APP_DIR/deploy/install-user.sh" --force-restart "$@"
