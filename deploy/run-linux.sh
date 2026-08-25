#!/usr/bin/env bash
set -euo pipefail
[[ ${EUID} -eq 0 ]] || { echo "Run: sudo bash deploy/run-linux.sh" >&2; exit 1; }
systemctl enable --now cadflow-lite
systemctl --no-pager --full status cadflow-lite
