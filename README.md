# Redlib with a Clef Flash content filter

This project runs a local Redlib UI behind a small FastAPI proxy. The primary configuration sends each post card on Redlib listing pages to Cloudflare Workers AI using the Clef Flash model and removes posts that fail the configured allowed-content decision.

## Architecture

```text
Browser -> http://127.0.0.1:8080 -> FastAPI proxy -> Redlib
                                      |
                                      +-> Cloudflare Workers AI (Clef Flash)
```

The proxy uses a strict filter: Compose waits for Decider readiness, and if the model later becomes unavailable, affected posts are removed rather than silently bypassing the requested exclusions. Decisions are cached in memory for the lifetime of the proxy.

## Requirements

- Docker Desktop on macOS.
- `uv` for local Python tooling. The containers use `uv` and `uvx` too.
- Network access to pull the Redlib image and reach Cloudflare Workers AI.
- A Cloudflare account with Workers AI enabled, plus an account ID and API token.

## Run it

```sh
cp .env.example .env
# Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in .env.
docker compose -f compose.yaml -f compose.native-cloudflare.yaml up --build
```

Open <http://127.0.0.1:8080>. The primary setup uses Cloudflare Workers AI with the `clef-flash` model, so it needs a Cloudflare account ID and API token. The first startup pulls the Redlib image and may take several minutes. Redlib uses the source-built `tagliasteel/redlib:latest` image referenced by the Ansible deployment because the prebuilt `quay.io/redlib/redlib:latest` image is currently unreliable with Reddit OAuth. It is forced to IPv4 to avoid a class of Reddit OAuth failures.

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

The proxy supports four decision providers. The primary provider is `DECISION_PROVIDER=cloudflare`, which uses Workers AI with `CLOUDFLARE_MODEL=clef-flash`; configure `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`. Set `DECISION_PROVIDER=local` to use the bundled Strands Decider, set `DECISION_PROVIDER=openai` and provide `OPENAI_API_KEY` to use the OpenAI Decisions API, or set `DECISION_PROVIDER=codex` to use the native Codex adapter with the subscription-backed `gpt-6-luna` model; start it with `./scripts/start-native-codex.sh`. The Codex path batches a page into one structured CLI request. The proxy also exposes Prometheus-compatible metrics at `/metrics`. The Codex adapter listens on port `8098` by default; override it with `CODEX_ADAPTER_PORT`, and override its model with `CODEX_MODEL` (default `gpt-6-luna`). To repeat a latency benchmark, start the selected stack with a unique `DECISION_DB` path and wait for `/ready`, then run `./scripts/benchmark.py --label cloudflare-c2 --concurrency 2 --requests 1 --require-fresh --output benchmark.json`. For concurrency comparisons, restart with `DECIDER_CONCURRENCY=4`, `8`, or `16` and a different `DECISION_DB` path for each run. The JSON records individual timings and proxy metric deltas, including cache hits and batch requests.
The local-provider proxy expects Redlib at `http://redlib:8080` and the local Decider at `http://decider:8099` by default. Override those when using the local provider:

```sh
REDLIB_URL=http://127.0.0.1:8081 DECIDER_URL=http://127.0.0.1:8099 \
  uv run uvicorn proxy.main:app --reload
```

## Cloudflare Clef Flash usage measurement

Workers AI provides a free allocation of 10,000 Neurons per day. Clef Flash does not publish a fixed Neuron cost per request, so request capacity must be measured using the actual prompt and output shape used by this proxy. The Neuron dashboard value is rounded, and the allowance resets at 00:00 UTC.

A cold-cache benchmark on 2026-10-09 produced 25 uncached classifications per run. Two clean runs at `DECIDER_CONCURRENCY=8` increased the Cloudflare dashboard from approximately `1.43k` to `1.47k` Neurons, or about 40 Neurons for 50 classifications. That is approximately:

```text
0.8 Neurons per classification
12,500 uncached classifications per 10,000-Neuron allowance
500 25-post pages per allowance
```

This is an approximate observed estimate, not a guaranteed quota. The dashboard rounds its displayed value, and the account total included earlier benchmark runs. Use a fresh SQLite cache for each measurement and compare the dashboard before and after a known number of newly classified posts. The repeatable benchmark command is:

```sh
DECISION_PROVIDER=cloudflare \
DECISION_DB=/data/unique-benchmark.sqlite3 \
docker compose -f compose.yaml -f compose.native-cloudflare.yaml up -d --build

./scripts/benchmark.py \
  --label clef-flash-run \
  --concurrency 8 \
  --requests 1 \
  --warmup 0 \
  --require-fresh \
  --output benchmark.json
```

The benchmark waits for `/ready`, and its JSON records posts seen, cache hits, successful decisions, failures, and timings. To calculate the observed cost, divide the Neuron increase shown in the Cloudflare dashboard by the number of newly classified posts. Persistent cache hits do not make new Workers AI requests.

## Notes and limitations

- Redlib has no post-filter extension point, so the proxy filters rendered HTML using Redlib's current `.post` selectors. If Redlib changes its markup, update `proxy/main.py`.
- `redlib.env` contains only Redlib settings. Proxy and Decider settings belong in `.env` and Compose's `environment` block.
- The classifier evaluates title, community, and preview text. It removes posts that are low-quality or substantially about politics, death or grief, divorce or breakups, or cute animals. It does not fetch linked pages or media.
- Uncached posts are classified concurrently with a bounded `DECIDER_CONCURRENCY` setting (default `16`) to keep listing pages responsive. Cloudflare Clef Flash testing found 16 to be the fastest reliable setting; higher values caused intermittent request failures.
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
