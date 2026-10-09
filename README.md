# Redlib with a Strands Decider filter

This project runs a local Redlib UI behind a small FastAPI proxy. The proxy sends each post card on Redlib listing pages to the local Strands Decider model and removes posts classified as low quality.

## Architecture

```text
Browser -> http://127.0.0.1:8080 -> FastAPI proxy -> Redlib
                                      |
                                      +-> Strands Decider HTTP server
```

The proxy is deliberately a fail-open filter: while the model is downloading, warming up, or unavailable, Redlib content is returned unchanged. Decisions are cached in memory for the lifetime of the proxy.

## Requirements

- Docker Desktop on macOS.
- `uv` for local Python tooling. The containers use `uv` and `uvx` too.
- Network access to pull the Redlib and Decider images.

## Run it

```sh
cp .env.example .env
docker compose up --build
```

Open <http://127.0.0.1:8080>. The first startup pulls the Redlib image and downloads the Decider model, so it can take several minutes. The proxy remains available and fails open while Decider is warming up. Redlib uses the source-built `tagliasteel/redlib:latest` image referenced by the Ansible deployment because the prebuilt `quay.io/redlib/redlib:latest` image is currently unreliable with Reddit OAuth. It is forced to IPv4 to avoid a class of Reddit OAuth failures.

To stop it:

```sh
docker compose down
```

To change the filtering strictness, edit `.env` and restart. `QUALITY_THRESHOLD` is the minimum yes-probability from Decider; higher values hide more posts. A value around `0.55` is a starting point, not a validated quality boundary.

## Local development with uv

```sh
uv sync
uv run uvicorn proxy.main:app --reload
```

The local proxy expects Redlib at `http://redlib:8080` and Decider at `http://decider:8099` by default. Override those for local services:

```sh
REDLIB_URL=http://127.0.0.1:8081 DECIDER_URL=http://127.0.0.1:8099 \
  uv run uvicorn proxy.main:app --reload
```

## Notes and limitations

- Redlib has no post-filter extension point, so the proxy filters rendered HTML using Redlib's current `.post` selectors. If Redlib changes its markup, update `proxy/main.py`.
- `redlib.env` contains only Redlib settings. Proxy and Decider settings belong in `.env` and Compose's `environment` block.
- The classifier evaluates title, community, preview, score, and comment text. It does not fetch linked pages or media.
- Only HTML GET responses are filtered. Assets and non-GET requests are passed through.
- The model uses CPU by default in this Compose setup. A future Linux/Proxmox deployment can add a GPU-specific Decider image or device configuration without changing the proxy contract.
