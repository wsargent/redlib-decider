#!/usr/bin/env python3
"""Benchmark the running Redlib filtering proxy.

Examples:
  ./scripts/benchmark.py --label mlx --requests 5
  ./scripts/benchmark.py --url http://127.0.0.1:8080/r/popular/new --label codex
  ./scripts/benchmark.py --requests 10 --output benchmark.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def fetch(url: str, timeout: float) -> tuple[int, int, float]:
    started = time.perf_counter()
    try:
        with urlopen(Request(url, headers={"Accept": "text/html"}), timeout=timeout) as response:
            body = response.read()
            return response.status, len(body), time.perf_counter() - started
    except HTTPError as exc:
        exc.read()
        return exc.code, 0, time.perf_counter() - started


def metrics(url: str, timeout: float) -> dict[str, float]:
    with urlopen(url, timeout=timeout) as response:
        text = response.read().decode()
    result = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, value = line.split(None, 1)
        result[name] = float(value)
    return result


def delta(after: dict[str, float], before: dict[str, float]) -> dict[str, float]:
    return {key: after.get(key, 0) - before.get(key, 0) for key in sorted(after)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080/r/popular/new")
    parser.add_argument("--metrics-url", default="http://127.0.0.1:8080/metrics")
    parser.add_argument("--label", default="unspecified")
    parser.add_argument("--requests", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", help="Write the JSON result to this file as well as stdout")
    parser.add_argument("--concurrency", type=int, help="Expected DECIDER_CONCURRENCY setting for result labeling")
    parser.add_argument("--require-fresh", action="store_true", help="Fail if the first request uses cached decisions")
    args = parser.parse_args()
    if args.requests < 1 or args.warmup < 0:
        parser.error("--requests must be positive and --warmup cannot be negative")

    try:
        base_url = args.metrics_url.rsplit("/metrics", 1)[0]
        fetch(base_url + "/health", args.timeout)
        ready_status, _, _ = fetch(base_url + "/ready", args.timeout)
        if ready_status != 200:
            raise RuntimeError(f"Redlib is not ready: HTTP {ready_status}; retry after it becomes healthy")
        before = metrics(args.metrics_url, args.timeout)
        if args.require_fresh and before.get("redlib_decider_cache_hits", 0) > 0:
            raise RuntimeError("benchmark cache is not fresh; use a unique DECISION_DB and restart the proxy")
        warmups = [fetch(args.url, args.timeout) for _ in range(args.warmup)]
        before_measurements = metrics(args.metrics_url, args.timeout)
        measurements = []
        for _ in range(args.requests):
            status, size, seconds = fetch(args.url, args.timeout)
            measurements.append({"status": status, "bytes": size, "seconds": seconds})
        after = metrics(args.metrics_url, args.timeout)
    except HTTPError as exc:
        print(f"benchmark failed: HTTP {exc.code} from {exc.url}; rebuild/restart the proxy if an endpoint is missing", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, ValueError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 1

    latencies = [item["seconds"] for item in measurements]
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "label": args.label,
        "url": args.url,
        "requests": args.requests,
        "warmup": args.warmup,
        "concurrency": args.concurrency,
        "warmup_results": [
            {"status": status, "bytes": size, "seconds": seconds}
            for status, size, seconds in warmups
        ],
        "measurements": measurements,
        "summary": {
            "min_seconds": min(latencies),
            "mean_seconds": statistics.mean(latencies),
            "median_seconds": statistics.median(latencies),
            "max_seconds": max(latencies),
            "successful_requests": sum(item["status"] == 200 for item in measurements),
        },
        "proxy_metrics_before": before_measurements,
        "proxy_metrics_after": after,
        "proxy_metrics_delta": delta(after, before_measurements),
        "proxy_metrics_since_start": delta(after, before),
    }
    encoded = json.dumps(result, indent=2) + "\n"
    print(encoded, end="")
    if args.output:
        with open(args.output, "w") as file:
            file.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
