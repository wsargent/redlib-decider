# Cloudflare Clef Flash usage measurement

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
