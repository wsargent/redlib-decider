# Benchmarking

To repeat a latency benchmark, start the selected stack with a unique `DECISION_DB` path and wait for `/ready`, then run:

```sh
./scripts/benchmark.py \
  --label cloudflare-c2 \
  --concurrency 2 \
  --requests 1 \
  --require-fresh \
  --output benchmark.json
```

For concurrency comparisons, restart with `DECIDER_CONCURRENCY=4`, `8`, or `16` and a different `DECISION_DB` path for each run. The JSON records individual timings and proxy metric deltas, including cache hits and batch requests.
