#!/usr/bin/env bash
set -Eeuo pipefail

ADAPTER_HOST="${CODEX_ADAPTER_HOST:-0.0.0.0}"
ADAPTER_PORT="${CODEX_ADAPTER_PORT:-8098}"
CODEX_MODEL="${CODEX_MODEL:-gpt-6-luna}"
HEALTH_URL="http://127.0.0.1:${ADAPTER_PORT}/health"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script requires macOS for the host Codex CLI session." >&2
  exit 1
fi

for command in codex docker uv curl; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "$command is required but was not found in PATH." >&2
    exit 1
  fi
done

if ! codex --version >/dev/null 2>&1; then
  echo "Codex is not available or is not authenticated." >&2
  exit 1
fi

echo "Starting native Codex adapter with ${CODEX_MODEL} on ${ADAPTER_HOST}:${ADAPTER_PORT}..."
CODEX_MODEL="$CODEX_MODEL" uv run uvicorn adapter.codex_adapter:app \
  --host "$ADAPTER_HOST" \
  --port "$ADAPTER_PORT" &
ADAPTER_PID=$!

cleanup() {
  if [[ -n "${ADAPTER_PID:-}" ]] && kill -0 "$ADAPTER_PID" 2>/dev/null; then
    echo "Stopping native Codex adapter (PID ${ADAPTER_PID})..."
    kill "$ADAPTER_PID" 2>/dev/null || true
    wait "$ADAPTER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

ready=false
for _ in {1..30}; do
  if curl --fail --silent "$HEALTH_URL" >/dev/null 2>&1; then
    ready=true
    break
  fi
  if ! kill -0 "$ADAPTER_PID" 2>/dev/null; then
    echo "Codex adapter exited before becoming ready." >&2
    wait "$ADAPTER_PID" || true
    exit 1
  fi
  sleep 1
done

if [[ "$ready" != true ]]; then
  echo "Codex adapter did not become ready at ${HEALTH_URL}." >&2
  exit 1
fi

echo "Native Codex adapter is ready. Starting Redlib and the proxy..."
docker compose \
  -f compose.yaml \
  -f compose.native-codex.yaml \
  up --build
status=$?
exit "$status"
