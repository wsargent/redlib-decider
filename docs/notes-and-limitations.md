# Notes and limitations

- Redlib has no post-filter extension point, so the proxy filters rendered HTML using Redlib's current `.post` selectors. If Redlib changes its markup, update `proxy/main.py`.
- `redlib.env` contains only Redlib settings. Proxy and Decider settings belong in `.env` and Compose's `environment` block.
- The classifier evaluates title, community, and preview text. It removes posts that are low-quality or substantially about the topics in `FILTER_EXCLUDED_TOPICS`. It does not fetch linked pages or media. Set that variable to a comma-separated list in `.env` and restart to change the excluded content.
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
