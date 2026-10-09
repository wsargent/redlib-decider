# Redlib Decider

This project lets me surf Reddit without any ads, suggestions, or posts I don't want to read.

It does this by using [Redlib](https://github.com/redlib-org/redlib), a private front-end for Reddit, and then filtering it through [Clef Flash](https://blog.cloudflare.com/clef-decision-models/), Cloudflare's decision model (aka "Jev-like").  

The line of reasoning for using Clef Flash as opposed to Jev, Stands Decider or another decision model:

* You can't filter posts one at a time -- it's too slow.
* You run into CPU problems with local models -- using Strands Decider in parallel spikes a laptop CPU.
* Clef has a free tier, and Jev doesn't.
* It takes 1 second to filter 16 posts concurrently, and then an sqlite cache stores the result.

It takes around 1 neuron to make a Clef-Flash call, and there's a budget of 10K neurons per day, so filtering with Clef Flash is essentially free.

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
