# Architecture

```text
Browser -> http://127.0.0.1:8080 -> FastAPI proxy -> Redlib
                                       |
                                       +-> Cloudflare Workers AI (Clef Flash)
```

The proxy uses a strict filter: Compose waits for Decider readiness, and if the model later becomes unavailable, affected posts are removed rather than silently bypassing the requested exclusions. Decisions are cached in memory for the lifetime of the proxy.
