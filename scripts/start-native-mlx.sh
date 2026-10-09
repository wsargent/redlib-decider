#!/usr/bin/env bash
set -Eeuo pipefail

MODEL="${DECIDER_MODEL:-StrandsAgents/strands-decider-2B-hobson-v21}"
HOST="${DECIDER_HOST:-0.0.0.0}"
PORT="${DECIDER_PORT:-8099}"
HEALTH_URL="http://127.0.0.1:${PORT}/health"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "This script requires an Apple-silicon Mac (Darwin arm64) for MLX." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to install the MLX-enabled Decider." >&2
  echo "Install uv, then rerun this script." >&2
  exit 1
fi

# The released PyPI package currently has no mlx extra. Install the extra from
# the upstream source repository into an isolated uv tool environment. Force
# this on every run so an earlier regular PyPI install cannot be reused.
echo "Installing the MLX-enabled Decider from the upstream source..."
uv tool install --force 'strands-decider[mlx] @ git+https://github.com/strands-labs/strands-decider.git'
export PATH="$(uv tool dir --bin):${PATH}"
hash -r

if ! command -v strands-decider >/dev/null 2>&1; then
  echo "strands-decider installation completed, but the executable is not in PATH." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required but was not found in PATH." >&2
  exit 1
fi

cleanup() {
  if [[ -n "${DECIDER_PID:-}" ]] && kill -0 "$DECIDER_PID" 2>/dev/null; then
    echo "Stopping native Decider (PID ${DECIDER_PID})..."
    kill "$DECIDER_PID" 2>/dev/null || true
    wait "$DECIDER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "Starting native MLX Decider on ${HOST}:${PORT}..."
strands-decider serve "$MODEL" \
  --device mlx \
  --host "$HOST" \
  --port "$PORT" &
DECIDER_PID=$!

ready=false
for _ in {1..120}; do
  if curl --fail --silent "$HEALTH_URL" >/dev/null 2>&1; then
    ready=true
    break
  fi
  if ! kill -0 "$DECIDER_PID" 2>/dev/null; then
    echo "Native Decider exited before becoming ready." >&2
    wait "$DECIDER_PID" || true
    exit 1
  fi
  sleep 1
done

if [[ "$ready" != true ]]; then
  echo "Native Decider did not become ready at ${HEALTH_URL}." >&2
  exit 1
fi

echo "Native MLX Decider is ready. Starting Redlib and the proxy..."
docker compose \
  -f compose.yaml \
  -f compose.native-mlx.yaml \
  up --build
status=$?
exit "$status"
