#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

if [ -d "$VENV_DIR" ]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
fi

export PORT="${PORT:-8001}"
export DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:///$ROOT_DIR/sessions.db}"

cd "$ROOT_DIR"
exec uvicorn server:app --host 0.0.0.0 --port "$PORT"
