# Redlib with a Strands Decider filter

This project runs a local Redlib UI behind a small FastAPI proxy. The proxy sends each post card on Redlib listing pages to the local Strands Decider model and removes posts that fail the configured allowed-content decision.

## Architecture

```text
Browser -> http://127.0.0.1:8080 -> FastAPI proxy -> Redlib
                                      |
                                      +-> Strands Decider HTTP server
```

The proxy uses a strict filter: Compose waits for Decider readiness, and if the model later becomes unavailable, affected posts are removed rather than silently bypassing the requested exclusions. Decisions are cached in memory for the lifetime of the proxy.

## Requirements

- Docker Desktop on macOS.
- `uv` for local Python tooling. The containers use `uv` and `uvx` too.
- Network access to pull the Redlib and Decider images.

## Run it

```sh
cp .env.example .env
docker compose up --build
```

Open <http://127.0.0.1:8080>. The first startup pulls the Redlib image and downloads the Decider model, so it can take several minutes. The proxy remains available while Decider is warming up, but posts are removed until classification succeeds. The Decider Python package is installed during the Docker build and the model files are stored in the persistent `decider_model_cache` volume, so normal container recreation does not repeat those downloads. Redlib uses the source-built `tagliasteel/redlib:latest` image referenced by the Ansible deployment because the prebuilt `quay.io/redlib/redlib:latest` image is currently unreliable with Reddit OAuth. It is forced to IPv4 to avoid a class of Reddit OAuth failures.

To stop it:

```sh
docker compose down
```

To change the filtering strictness, edit `.env` and restart. `ALLOWED_THRESHOLD` is the minimum probability that the complete post is allowed. Higher values hide more posts. The single decision covers quality, politics, death/grief, divorce/breakups, and cute animals. `0.50` is a practical starting point, not a validated model boundary.

## Local development with uv

```sh
uv sync
uv run uvicorn proxy.main:app --reload
```

The proxy supports three decision providers. The default `DECISION_PROVIDER=local` uses the bundled Strands Decider. Set `DECISION_PROVIDER=openai` and provide `OPENAI_API_KEY` to use the OpenAI Decisions API. Set `DECISION_PROVIDER=codex` to use the native Codex adapter with the subscription-backed `gpt-6-luna` model; start it with `./scripts/start-native-codex.sh`. The Codex path batches a page into one structured CLI request. The proxy also exposes Prometheus-compatible metrics at `/metrics`. The Codex adapter listens on port `8098` by default; override it with `CODEX_ADAPTER_PORT`, and override its model with `CODEX_MODEL` (default `gpt-6-luna`). To repeat a latency benchmark, rebuild/restart the selected stack and run `./scripts/benchmark.py --label mlx --requests 5 --warmup 1 --output benchmark.json`. The JSON records individual timings and proxy metric deltas, including cache hits and batch requests.
The local proxy expects Redlib at `http://redlib:8080` and Decider at `http://decider:8099` by default. Override those for local services:

```sh
REDLIB_URL=http://127.0.0.1:8081 DECIDER_URL=http://127.0.0.1:8099 \
  uv run uvicorn proxy.main:app --reload
```

## Notes and limitations

- Redlib has no post-filter extension point, so the proxy filters rendered HTML using Redlib's current `.post` selectors. If Redlib changes its markup, update `proxy/main.py`.
- `redlib.env` contains only Redlib settings. Proxy and Decider settings belong in `.env` and Compose's `environment` block.
- The classifier evaluates title, community, and preview text. It removes posts that are low-quality or substantially about politics, death or grief, divorce or breakups, or cute animals. It does not fetch linked pages or media.
- Uncached posts are classified concurrently with a bounded `DECIDER_CONCURRENCY` setting (default `2`) to keep listing pages responsive without overwhelming the local model.
- `DECISION_CACHE_SIZE` bounds both the in-memory cache and the persistent SQLite cache. The oldest persistent decisions are pruned after each new decision.
- Only HTML GET responses are filtered. Assets and non-GET requests are passed through.
- The Compose Decider uses CPU-only Torch. On an Apple-silicon Mac, MLX can be substantially faster when Decider runs natively outside Docker. To use that setup, install the MLX extra on the Mac, start the native server, and launch the remaining services with the included override:

  The startup script installs the MLX-enabled Decider from the upstream source repository automatically. If you install it manually, use an isolated tool environment:

  ```sh
  uv tool install 'strands-decider[mlx] @ git+https://github.com/strands-labs/strands-decider.git'
  ```

  Or use the startup script, which installs the dependency if needed, launches Decider, waits for `/health`, starts Docker, and stops Decider when Docker exits:

  ```sh
  ./scripts/start-native-mlx.sh
  ```

  The script accepts `DECIDER_MODEL`, `DECIDER_HOST`, and `DECIDER_PORT` overrides. The Compose override disables the containerized Decider and points the proxy at `host.docker.internal:8099`. The proxy contract is unchanged.
