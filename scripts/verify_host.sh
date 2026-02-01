#!/usr/bin/env bash
set -e

echo "[verify] checking base requirements..."

command -v nginx >/dev/null || {
  echo "nginx not installed"
  exit 1
}

command -v python3 >/dev/null || {
  echo "python3 not installed"
  exit 1
}

systemctl is-active nginx >/dev/null || {
  echo "nginx not running"
  exit 1
}

echo "[verify] OK"
