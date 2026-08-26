#!/usr/bin/env bash
set -Eeuo pipefail

# One-command, ordinary-user installer for an offline Linux host.
# It never uses sudo/systemd and deliberately leaves LSF/FlexNet paths empty;
# those site-specific values are entered later in the Web configuration page.

APP_DIR=${CADFLOW_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
WHEELHOUSE=${CADFLOW_WHEELHOUSE:-$APP_DIR/wheelhouse}
WHEELHOUSE_EXPLICIT=0
if [[ -n "${CADFLOW_WHEELHOUSE:-}" ]]; then
  WHEELHOUSE_EXPLICIT=1
fi
PYTHON_BIN=${CADFLOW_PYTHON_BIN:-}
HOST=${CADFLOW_BIND_HOST:-0.0.0.0}
PORT=${CADFLOW_PORT:-8080}
ENV_FILE=${CADFLOW_ENV_FILE:-$APP_DIR/.cadflow.env}
PID_FILE=${CADFLOW_PID_FILE:-$APP_DIR/cadflow.pid}
LOG_FILE=${CADFLOW_LOG_FILE:-$APP_DIR/logs/cadflow.log}
HEALTH_TIMEOUT=${CADFLOW_HEALTH_TIMEOUT_SECONDS:-20}
VENV_DIR=${CADFLOW_VENV_DIR:-$APP_DIR/.venv}
LOCK_FILE=$APP_DIR/.cadflow-install.lock

usage() {
  cat <<'EOF'
Usage: bash deploy/install-user.sh [options]

Options:
  --app-dir DIR       Project checkout (default: this project)
  --wheelhouse DIR    Offline Python wheels (default: APP_DIR/wheelhouse)
  --python PATH       Python 3.9+ executable (default: auto-select 3.12/3.11/3.10/3.9)
  --host ADDRESS      Bind address (default: 0.0.0.0)
  --port PORT         HTTP port (default: 8080)
  --force-restart     Stop the recorded CADFlow process before starting
  --skip-deps         Do not install dependencies; use the existing venv
  -h, --help          Show this help

No LSF/FlexNet path is required here. Configure those values in the Web
configuration page after the Demo page is available.
EOF
}

fail() {
  echo "CADFlow install failed: $*" >&2
  exit 1
}

project_version() {
  sed -n 's/^version = "\([^"]*\)"/\1/p' "$APP_DIR/pyproject.toml" | head -n 1
}

normalize_config_password_hash() {
  [[ -f "$ENV_FILE" ]] || return 0
  local raw_hash tmp_file
  raw_hash=$(sed -n 's/^CADFLOW_CONFIG_PASSWORD_HASH=//p' "$ENV_FILE" | head -n 1)
  # PBKDF2 hashes contain literal '$' separators (for example $310000$).
  # Quote an older unquoted entry before sourcing the env file under `set -u`.
  [[ "$raw_hash" == pbkdf2_sha256\$* ]] || return 0
  tmp_file="${ENV_FILE}.tmp.$$"
  awk -v hash="$raw_hash" '
    BEGIN { replaced = 0; quote = sprintf("%c", 39) }
    /^CADFLOW_CONFIG_PASSWORD_HASH=/ && !replaced {
      print "CADFLOW_CONFIG_PASSWORD_HASH=" quote hash quote
      replaced = 1
      next
    }
    { print }
  ' "$ENV_FILE" > "$tmp_file" || { rm -f "$tmp_file"; fail "unable to normalize configuration password hash"; }
  chmod 600 "$tmp_file"
  mv -f "$tmp_file" "$ENV_FILE"
}

FORCE_RESTART=0
SKIP_DEPS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      [[ $# -ge 2 ]] || fail "--app-dir requires a directory"
      APP_DIR=$2
      shift 2
      ;;
    --wheelhouse)
      [[ $# -ge 2 ]] || fail "--wheelhouse requires a directory"
      WHEELHOUSE=$2
      WHEELHOUSE_EXPLICIT=1
      shift 2
      ;;
    --python)
      [[ $# -ge 2 ]] || fail "--python requires an executable"
      PYTHON_BIN=$2
      shift 2
      ;;
    --host)
      [[ $# -ge 2 ]] || fail "--host requires an address"
      HOST=$2
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || fail "--port requires a number"
      PORT=$2
      shift 2
      ;;
    --force-restart)
      FORCE_RESTART=1
      shift
      ;;
    --skip-deps)
      SKIP_DEPS=1
      shift
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

[[ ${EUID} -ne 0 ]] || fail "run as the normal application account; sudo/systemd is not used"
APP_DIR=$(cd "$APP_DIR" 2>/dev/null && pwd) || fail "project directory not found: $APP_DIR"
if [[ $WHEELHOUSE_EXPLICIT -eq 0 ]]; then
  WHEELHOUSE=$APP_DIR/wheelhouse
fi
ENV_FILE=${CADFLOW_ENV_FILE:-$APP_DIR/.cadflow.env}
PID_FILE=${CADFLOW_PID_FILE:-$APP_DIR/cadflow.pid}
LOG_FILE=${CADFLOW_LOG_FILE:-$APP_DIR/logs/cadflow.log}
VENV_DIR=${CADFLOW_VENV_DIR:-$APP_DIR/.venv}
LOCK_FILE=$APP_DIR/.cadflow-install.lock

[[ -f "$APP_DIR/pyproject.toml" ]] || fail "pyproject.toml not found in $APP_DIR"
[[ -f "$APP_DIR/deploy/cadflow-serve" ]] || fail "deploy/cadflow-serve not found"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v flock >/dev/null 2>&1 || fail "flock is required"

python_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

python_has_sqlite() {
  "$1" -c 'import sqlite3' >/dev/null 2>&1
}

venv_has_sqlite() {
  "$1" -c '
try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3
' >/dev/null 2>&1
}

if [[ -n "$PYTHON_BIN" ]]; then
  if [[ "$PYTHON_BIN" != */* ]]; then
    PYTHON_BIN=$(command -v "$PYTHON_BIN" || true)
  fi
  [[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || fail "Python executable is not available: ${CADFLOW_PYTHON_BIN:-$PYTHON_BIN}"
  python_supported "$PYTHON_BIN" || fail "Python 3.9+ is required: $PYTHON_BIN"
  if ! python_has_sqlite "$PYTHON_BIN" && [[ ! -d "$WHEELHOUSE" ]]; then
    fail "$PYTHON_BIN lacks sqlite3; provide an offline wheelhouse containing pysqlite3-binary"
  fi
else
  # RHEL-family hosts may keep an older system python3 beside a newer module.
  # Prefer the newest conventional executable without changing system config.
  if [[ -d "$WHEELHOUSE" ]]; then
    for candidate in python3.12 python3.11 python3.10 python3.9 python3 python; do
      candidate_path=$(command -v "$candidate" || true)
      if [[ -n "$candidate_path" ]] && python_supported "$candidate_path"; then
        PYTHON_BIN=$candidate_path
        break
      fi
    done
  else
    for candidate in python3.12 python3.11 python3.10 python3.9 python3 python; do
      candidate_path=$(command -v "$candidate" || true)
      if [[ -n "$candidate_path" ]] && python_supported "$candidate_path" && python_has_sqlite "$candidate_path"; then
        PYTHON_BIN=$candidate_path
        break
      fi
    done
  fi
  [[ -n "$PYTHON_BIN" ]] || fail "Python 3.9+ with sqlite3 was not found; load a compatible Python or provide a pysqlite3 wheelhouse"
fi

PYTHON_VERSION=$($PYTHON_BIN -c 'import sys; print("%d.%d" % sys.version_info[:2])') || fail "cannot run $PYTHON_BIN"

cd "$APP_DIR"
exec 9>"$LOCK_FILE"
flock -n 9 || fail "another CADFlow install/start operation is running"

if [[ -x "$VENV_DIR/bin/python" ]] && ! python_supported "$VENV_DIR/bin/python"; then
  fail "existing $VENV_DIR is older than Python 3.9; move it aside (for example: mv $VENV_DIR ${VENV_DIR}.old) and rerun"
fi
if [[ -x "$VENV_DIR/bin/python" ]] && ! venv_has_sqlite "$VENV_DIR/bin/python" && [[ ! -d "$WHEELHOUSE" ]]; then
  fail "existing $VENV_DIR lacks SQLite support; move it aside (for example: mv $VENV_DIR ${VENV_DIR}.old) and provide a compatible Python or wheelhouse"
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Creating virtual environment: $VENV_DIR"
  VENV_ARGS=()
  if [[ ! -d "$WHEELHOUSE" ]] && "$PYTHON_BIN" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
    # Keep the installation offline and isolated while reusing the packages
    # already supplied by the site-managed Python environment.
    VENV_ARGS+=(--system-site-packages)
    echo "No wheelhouse found; reusing FastAPI/Uvicorn from $PYTHON_BIN"
  fi
  "$PYTHON_BIN" -m venv "${VENV_ARGS[@]}" "$VENV_DIR" || fail "unable to create virtual environment"
fi
VENV_PYTHON=$VENV_DIR/bin/python

python_supported "$VENV_PYTHON" || fail "the virtual environment must use Python 3.9+"

if [[ $SKIP_DEPS -eq 0 ]]; then
  if [[ -d "$WHEELHOUSE" ]]; then
    echo "Installing offline dependencies from $WHEELHOUSE ..."
    OFFLINE_REQUIREMENTS=("fastapi>=0.115,<1" "uvicorn>=0.30,<1")
    if ! venv_has_sqlite "$VENV_PYTHON"; then
      OFFLINE_REQUIREMENTS+=("pysqlite3-binary>=0.5,<1")
    fi
    PIP_LOG=$(mktemp "${TMPDIR:-/tmp}/cadflow-pip.XXXXXX")
    if ! "$VENV_PYTHON" -m pip install --no-index --find-links="$WHEELHOUSE" \
      "${OFFLINE_REQUIREMENTS[@]}" >"$PIP_LOG" 2>&1; then
      cat "$PIP_LOG" >&2
      rm -f "$PIP_LOG"
      fail "offline dependency installation failed; check wheelhouse and Python ABI"
    fi
    rm -f "$PIP_LOG"
    echo "Dependencies: ready (offline wheelhouse)"
  elif "$VENV_PYTHON" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
    echo "wheelhouse not found; existing FastAPI/Uvicorn installation will be used"
  else
    fail "wheelhouse not found and FastAPI/Uvicorn are not installed: $WHEELHOUSE"
  fi
else
  "$VENV_PYTHON" -c 'import fastapi, uvicorn' >/dev/null 2>&1 || \
    fail "--skip-deps requested but FastAPI/Uvicorn are missing"
fi

venv_has_sqlite "$VENV_PYTHON" || fail "Python has no sqlite3/pysqlite3 support; add pysqlite3-binary to the offline wheelhouse"

"$VENV_PYTHON" -m compileall -q "$APP_DIR/app" || fail "Python source compilation failed"
mkdir -p "$APP_DIR/data" "$(dirname "$LOG_FILE")"

if [[ ! -f "$ENV_FILE" ]]; then
  umask 077
  ADMIN_TOKEN=${CADFLOW_ADMIN_TOKEN:-$($VENV_PYTHON -c 'import secrets; print(secrets.token_hex(32))')}
  cat > "$ENV_FILE" <<EOF
# Generated by deploy/install-user.sh. Site-specific LSF/FlexNet values are
# intentionally configured from the Web UI and are not stored here.
CADFLOW_MODE=demo
CADFLOW_CLUSTER_NAME=demo-cluster
CADFLOW_DB_PATH=./data/cadflow.db
CADFLOW_BIND_HOST=$HOST
CADFLOW_PORT=$PORT
CADFLOW_ADMIN_TOKEN=$ADMIN_TOKEN
CADFLOW_DB_RETENTION_DAYS=7
CADFLOW_DB_MAX_SIZE_MB=1024
EOF
  echo "Created configuration: $ENV_FILE"
fi

CONFIG_PASSWORD_GENERATED=""
normalize_config_password_hash
if ! grep -Eq '^CADFLOW_CONFIG_PASSWORD_HASH=[^[:space:]]+$' "$ENV_FILE"; then
  umask 077
  CONFIG_PASSWORD_GENERATED=$($VENV_PYTHON -c 'import secrets; print(secrets.token_urlsafe(12))')
  CONFIG_PASSWORD_HASH=$(CADFLOW_CONFIG_PASSWORD="$CONFIG_PASSWORD_GENERATED" "$VENV_PYTHON" -c '
import base64, hashlib, os, secrets
password = os.environ["CADFLOW_CONFIG_PASSWORD"].encode("utf-8")
iterations = 310000
salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac("sha256", password, salt, iterations)
encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
print(f"pbkdf2_sha256${iterations}${encode(salt)}${encode(digest)}")
')
  printf "\nCADFLOW_CONFIG_PASSWORD_HASH='%s'\n" "$CONFIG_PASSWORD_HASH" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi
if [[ -n "$CONFIG_PASSWORD_GENERATED" ]]; then
  # The running process cannot enforce the newly generated password until it
  # is restarted, so make the first migration restart it automatically.
  FORCE_RESTART=1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

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

wait_health() {
  local url="http://127.0.0.1:${CADFLOW_PORT:-$PORT}/api/health"
  for _ in $(seq 1 "$HEALTH_TIMEOUT"); do
    # Always bound each probe.  A listening but wedged uvicorn process must
    # not leave the installer blocked forever on curl's default timeout.
    if curl --connect-timeout 1 --max-time 3 --fail --silent "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

running_version() {
  local url="http://127.0.0.1:${CADFLOW_PORT:-$PORT}/api/health" body
  body=$(curl --connect-timeout 1 --max-time 3 --fail --silent "$url" 2>/dev/null || true)
  [[ -n "$body" ]] || { printf '%s\n' "unknown"; return 0; }
  printf '%s' "$body" | "$VENV_PYTHON" -c 'import json, sys; print(json.load(sys.stdin).get("version", "unknown"))' 2>/dev/null || printf '%s\n' "unknown"
}

show_generated_config_password() {
  if [[ -n "$CONFIG_PASSWORD_GENERATED" ]]; then
    echo "配置管理口令（请立即保存）：$CONFIG_PASSWORD_GENERATED"
  fi
}

OLD_PID=$(read_pid || true)
if [[ -n "$OLD_PID" ]]; then
  if [[ $FORCE_RESTART -eq 0 ]]; then
    if wait_health; then
      echo "CADFlow is already running: PID=$OLD_PID"
      echo "Version: v$(project_version) (API v$(running_version))"
      echo "Open: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${CADFLOW_PORT:-$PORT}"
      show_generated_config_password
      exit 0
    fi
    echo "Recorded process $OLD_PID is not healthy; restarting it"
  fi
  stop_pid "$OLD_PID" || fail "existing CADFlow process $OLD_PID did not stop"
fi

echo "Starting CADFlow and waiting for health check ..."
# Do not let the long-running server inherit the install lock.  Otherwise the
# next manual upgrade would see the healthy server as an active installer.
nohup bash "$APP_DIR/deploy/cadflow-serve" 9>&- >>"$LOG_FILE" 2>&1 &
NEW_PID=$!
printf '%s\n' "$NEW_PID" > "$PID_FILE"

if wait_health; then
  SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo "CADFlow deployed successfully"
  echo "Version: v$(project_version) (API v$(running_version))"
  echo "Commit: $(git -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "PID: $NEW_PID"
  echo "Open: http://${SERVER_IP:-127.0.0.1}:${CADFLOW_PORT:-$PORT}"
  echo "Config: $ENV_FILE"
  echo "Log: $LOG_FILE"
  show_generated_config_password
  echo "Next: open Web 配置 and enter the site-specific LSF/FlexNet paths"
  exit 0
fi

echo "CADFlow did not become healthy; recent log:" >&2
tail -n 80 "$LOG_FILE" >&2 || true
stop_pid "$NEW_PID" || true
rm -f "$PID_FILE"
exit 1
