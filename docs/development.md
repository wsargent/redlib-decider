# Local development with uv

```sh
uv sync
uv run uvicorn proxy.main:app --reload
```

The proxy supports four decision providers. The primary provider is `DECISION_PROVIDER=cloudflare`, which uses Workers AI with `CLOUDFLARE_MODEL=clef-flash`; configure `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`. Set `DECISION_PROVIDER=local` to use the bundled Strands Decider, set `DECISION_PROVIDER=openai` and provide `OPENAI_API_KEY` to use the OpenAI Decisions API, or set `DECISION_PROVIDER=codex` to use the native Codex adapter with the subscription-backed `gpt-6-luna` model; start it with `./scripts/start-native-codex.sh`.

The Codex path batches a page into one structured CLI request. The proxy also exposes Prometheus-compatible metrics at `/metrics`. The Codex adapter listens on port `8098` by default; override it with `CODEX_ADAPTER_PORT`, and override its model with `CODEX_MODEL` (default `gpt-6-luna`).

The local-provider proxy expects Redlib at `http://redlib:8080` and the local Decider at `http://decider:8099` by default. Override those when using the local provider:

```sh
REDLIB_URL=http://127.0.0.1:8081 DECIDER_URL=http://127.0.0.1:8099 \
  uv run uvicorn proxy.main:app --reload
```
