# Redlib with a Clef Flash content filter

This project runs a local Redlib UI behind a small FastAPI proxy. The primary configuration sends each post card on Redlib listing pages to Cloudflare Workers AI using the Clef Flash model and removes posts that fail the configured allowed-content decision.

## Requirements

- Docker Desktop on macOS.
- Network access to pull the Redlib image and reach Cloudflare Workers AI.
- A Cloudflare account with Workers AI enabled, plus an account ID and API token.

## Install and run

```sh
cp .env.example .env
# Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in .env.
docker compose -f compose.yaml -f compose.native-cloudflare.yaml up --build
```

Open <http://127.0.0.1:8080>. The primary setup uses Cloudflare Workers AI with the `clef-flash` model. The first startup pulls the Redlib image and may take several minutes.

To stop it:

```sh
docker compose down
```

To change the filtering content, edit `FILTER_EXCLUDED_TOPICS` in `.env` as a comma-separated list and restart. The default is:

```env
FILTER_EXCLUDED_TOPICS=politics,death or grief,divorce or breakups,cute animals
```

For example, to exclude politics and spoilers instead:

```env
FILTER_EXCLUDED_TOPICS=politics,spoilers
```

`ALLOWED_THRESHOLD` is the minimum probability that the complete post is allowed; higher values hide more posts. `0.50` is a practical starting point, not a validated model boundary.

## More documentation

- [Architecture, providers, development, and operations](docs/guide.md)
