#!/usr/bin/env bash
set -Eeuo pipefail

# User-mode updater for an offline LSF login node.
# It updates the existing clone from its configured Git remote, preserves the
# venv/data/logs, restarts only the PID recorded by cadflow.pid, and rolls the
# source tree back when the health check fails.

APP_DIR=${CADFLOW_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
ENV_FILE=${CADFLOW_ENV_FILE:-$APP_DIR/.cadflow.env}
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

REMOTE=${CADFLOW_REMOTE:-origin}
REF=${CADFLOW_REF:-master}
PID_FILE=${CADFLOW_PID_FILE:-$APP_DIR/cadflow.pid}
LOG_FILE=${CADFLOW_LOG_FILE:-$APP_DIR/logs/cadflow.log}
DB_PATH=${CADFLOW_DB_PATH:-$APP_DIR/data/cadflow.db}
PYTHON=${CADFLOW_PYTHON:-$APP_DIR/.venv/bin/python}
SERVE_SCRIPT=$APP_DIR/deploy/cadflow-serve
PORT=${CADFLOW_PORT:-8080}
HEALTH_URL=${CADFLOW_HEALTH_URL:-http://127.0.0.1:$PORT/api/health}
HEALTH_TIMEOUT=${CADFLOW_HEALTH_TIMEOUT_SECONDS:-20}
LOCK_FILE=$APP_DIR/.cadflow-update.lock

usage() {
  cat <<'EOF'
Usage: bash deploy/update-user.sh [--ref BRANCH]

Updates the current user-mode checkout, preserves data and the virtualenv,
restarts the recorded CADFlow process, and rolls back on a failed health check.
Environment overrides: CADFLOW_APP_DIR, CADFLOW_ENV_FILE, CADFLOW_REMOTE,
CADFLOW_REF, CADFLOW_PID_FILE, CADFLOW_LOG_FILE, CADFLOW_DB_PATH,
CADFLOW_PYTHON, CADFLOW_PORT, CADFLOW_HEALTH_TIMEOUT_SECONDS.
EOF
}

fail() {
  echo "CADFlow update failed: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref)
      [[ $# -ge 2 ]] || fail "--ref requires a branch name"
      REF=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$ENV_FILE" && -z "${CADFLOW_MODE:-}" ]]; then
  fail "CADFLOW_MODE is not set; run from the app environment or create $ENV_FILE"
fi

[[ ${EUID} -ne 0 ]] || fail "run as the normal CADFlow account; sudo/systemd is not used"
[[ -d "$APP_DIR/.git" ]] || fail "not a Git checkout: $APP_DIR"
[[ -x "$PYTHON" ]] || fail "Python venv not found: $PYTHON"
[[ -f "$SERVE_SCRIPT" ]] || fail "serve script not found: $SERVE_SCRIPT"
command -v git >/dev/null 2>&1 || fail "git is required"
command -v curl >/dev/null 2>&1 || fail "curl is required for the health check"
command -v flock >/dev/null 2>&1 || fail "flock is required"

# uvicorn imports app.main from the project directory.  Keep the updater
# independent of the caller's current directory and make relative env paths
# (for example ./data/cadflow.db) resolve inside this checkout.
cd "$APP_DIR"

exec 9>"$LOCK_FILE"
flock -n 9 || fail "another CADFlow update is already running"

CURRENT_BRANCH=$(git -C "$APP_DIR" branch --show-current)
[[ "$CURRENT_BRANCH" == "$REF" ]] || fail "current branch is '$CURRENT_BRANCH', expected '$REF'"
git -C "$APP_DIR" diff --quiet || fail "tracked files have local changes; commit or stash them first"
git -C "$APP_DIR" diff --cached --quiet || fail "staged files exist; commit or unstage them first"
[[ -z "$(git -C "$APP_DIR" status --porcelain --untracked-files=normal)" ]] || \
  fail "untracked files exist; move or remove them before updating"

OLD_COMMIT=$(git -C "$APP_DIR" rev-parse HEAD)
git -C "$APP_DIR" remote get-url "$REMOTE" >/dev/null 2>&1 || fail "Git remote not found: $REMOTE"

echo "Fetching $REMOTE/$REF ..."
git -C "$APP_DIR" fetch --prune "$REMOTE" "$REF"
git -C "$APP_DIR" merge --ff-only FETCH_HEAD
NEW_COMMIT=$(git -C "$APP_DIR" rev-parse HEAD)

if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
  echo "Already up to date: ${NEW_COMMIT:0:7}"
  exit 0
fi

"$PYTHON" -c 'import fastapi, uvicorn' >/dev/null 2>&1 || {
  git -C "$APP_DIR" reset --hard "$OLD_COMMIT" >/dev/null
  fail "current .venv is missing FastAPI/Uvicorn; install dependencies from the offline wheelhouse before updating"
}
"$PYTHON" -m compileall -q "$APP_DIR/app" || {
  git -C "$APP_DIR" reset --hard "$OLD_COMMIT" >/dev/null
  fail "new source failed Python compilation; source tree was rolled back"
}

read_pid() {
  [[ -s "$PID_FILE" ]] || return 1
  local pid
  pid=$(tr -d '[:space:]' < "$PID_FILE")
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o args= 2>/dev/null | grep -Fq 'app.main:app' || return 1
  printf '%s\n' "$pid"
}

stop_pid() {
  local pid=$1
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done
  return 1
}

start_app() {
  mkdir -p "$(dirname "$LOG_FILE")"
  nohup bash "$SERVE_SCRIPT" >>"$LOG_FILE" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" > "$PID_FILE"
  printf '%s\n' "$pid"
}

wait_health() {
  for _ in $(seq 1 "$HEALTH_TIMEOUT"); do
    if curl --fail --silent "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

OLD_PID=$(read_pid || true)
if [[ -z "$OLD_PID" ]] && curl --fail --silent "$HEALTH_URL" >/dev/null 2>&1; then
  git -C "$APP_DIR" reset --hard "$OLD_COMMIT" >/dev/null
  fail "CADFlow responds on $HEALTH_URL but cadflow.pid is missing or invalid"
fi

BACKUP_DIR=$APP_DIR/backups
mkdir -p "$BACKUP_DIR" "$(dirname "$LOG_FILE")"
STAMP=$(date +%Y%m%d%H%M%S)
if [[ -f "$DB_PATH" ]]; then
  cp -p "$DB_PATH" "$BACKUP_DIR/cadflow-$STAMP.db"
  for suffix in -wal -shm; do
    [[ -f "$DB_PATH$suffix" ]] && cp -p "$DB_PATH$suffix" "$BACKUP_DIR/cadflow-$STAMP.db$suffix"
  done
fi

if [[ -n "$OLD_PID" ]] && ! stop_pid "$OLD_PID"; then
  git -C "$APP_DIR" reset --hard "$OLD_COMMIT" >/dev/null
  fail "existing CADFlow process $OLD_PID did not stop; source tree was rolled back"
fi

NEW_PID=$(start_app)
if wait_health; then
  echo "Updated to ${NEW_COMMIT:0:7}; PID=$NEW_PID"
  echo "Health: $HEALTH_URL"
  echo "Log: $LOG_FILE"
  exit 0
fi

echo "Health check failed; stopping new process and rolling back ..." >&2
stop_pid "$NEW_PID" || true
git -C "$APP_DIR" reset --hard "$OLD_COMMIT" >/dev/null
"$PYTHON" -m compileall -q "$APP_DIR/app"
ROLLBACK_PID=$(start_app)
if wait_health; then
  echo "Rollback restored ${OLD_COMMIT:0:7}; PID=$ROLLBACK_PID" >&2
else
  echo "Rollback process also failed health check; inspect $LOG_FILE" >&2
fi
exit 1
