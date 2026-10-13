from __future__ import annotations

import asyncio
import hashlib
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from sqlite_utils import Database
from fastapi import FastAPI, Request, Response

REDLIB_URL = os.getenv("REDLIB_URL", "http://redlib:8080").rstrip("/")
DECIDER_URL = os.getenv("DECIDER_URL", "http://decider:8099").rstrip("/")
CODEX_ADAPTER_URL = os.getenv("CODEX_ADAPTER_URL", "http://host.docker.internal:8098").rstrip("/")
ALLOWED_THRESHOLD = float(os.getenv("ALLOWED_THRESHOLD", "0.50"))
DECIDER_TIMEOUT = float(os.getenv("DECIDER_TIMEOUT", "20"))
# Timeout for fetching pages from Redlib, which waits on Reddit upstream.
# Cold or slow Redlib responses routinely exceed the decision timeout.
REDLIB_TIMEOUT = float(os.getenv("REDLIB_TIMEOUT", "60"))
CACHE_SIZE = int(os.getenv("DECISION_CACHE_SIZE", "512"))
DECIDER_CONCURRENCY = int(os.getenv("DECIDER_CONCURRENCY", "2"))
DECISION_DB = os.getenv("DECISION_DB", "/data/decisions.sqlite3")
DECISION_PROVIDER = os.getenv("DECISION_PROVIDER", "local").lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_MODEL = os.getenv("CLOUDFLARE_MODEL", "clef-flash")
FILTER_EXCLUDED_TOPICS = os.getenv(
    "FILTER_EXCLUDED_TOPICS",
    "politics,death or grief,divorce or breakups,cute animals",
)

SKIP_PREFIXES = ("/static/", "/css/", "/js/", "/favicon")

# Hidden-until-hover styling for filtered-post titles, matching redlib's
# .md-spoiler-text behavior but usable outside markdown content. The
# .revealed class gives touch devices a tap-to-toggle reveal.
DECIDER_SPOILER_CSS = (
    ".decider-spoiler,.decider-spoiler a{background:var(--highlighted);color:transparent;border-radius:3px;cursor:pointer;}"
    ".decider-spoiler:hover,.decider-spoiler.revealed{background:var(--foreground);color:var(--text);}"
    ".decider-spoiler:hover a{color:var(--accent);}"
    ".decider-spoiler.revealed,.decider-spoiler.revealed a{color:var(--text);}"
)
DECIDER_SPOILER_JS = (
    'document.addEventListener("click",function(e){'
    "var t=e.target.closest&&e.target.closest('.decider-spoiler');"
    "if(t){t.classList.toggle('revealed');}});"
)


@dataclass
class DecisionCache:
    values: OrderedDict[str, bool]

    def get(self, key: str) -> bool | None:
        value = self.values.get(key)
        if value is not None:
            self.values.move_to_end(key)
        return value

    def put(self, key: str, value: bool) -> None:
        self.values[key] = value
        self.values.move_to_end(key)
        while len(self.values) > CACHE_SIZE:
            self.values.popitem(last=False)


cache = DecisionCache(OrderedDict())

METRICS = {
    "page_requests": 0,
    "page_seconds": 0.0,
    "posts_seen": 0,
    "cache_hits": 0,
    "decisions": 0,
    "decision_seconds": 0.0,
    "decision_failures": 0,
    "upstream_failures": 0,
    "posts_filtered": 0,
    "posts_no_response": 0,
    "batch_requests": 0,
}


PERSISTENT_CACHE = True


def load_persistent_cache() -> None:
    global PERSISTENT_CACHE
    try:
        os.makedirs(os.path.dirname(DECISION_DB), exist_ok=True)
        db = Database(DECISION_DB)
        table = db["decisions"]
        table.create({"key": str, "allowed": int}, pk="key", if_not_exists=True)
        for row in table.rows_where(order_by="rowid DESC", limit=CACHE_SIZE):
            cache.put(row["key"], bool(row["allowed"]))
    except OSError:
        PERSISTENT_CACHE = False


def persist_decision(key: str, result: bool) -> None:
    if not PERSISTENT_CACHE:
        return
    try:
        db = Database(DECISION_DB)
        table = db["decisions"]
        table.create({"key": str, "allowed": int}, pk="key", if_not_exists=True)
        table.upsert({"key": key, "allowed": int(result)}, pk="key")
        stale = list(table.rows_where(order_by="rowid DESC", offset=CACHE_SIZE))
        if stale:
            table.delete_where("key in (?)", [row["key"] for row in stale])
    except OSError:
        pass


load_persistent_cache()


def post_text(post) -> str:
    title = post.select_one(".post_title")
    body = post.select_one(".post_body.post_preview")
    subreddit = post.select_one(".post_subreddit")
    # Keep the classifier input stable across refreshes. Scores and comment
    # counts change frequently and do not help classify the requested topics;
    # including them would defeat the persistent decision cache.
    parts = [
        f"Title: {title.get_text(' ', strip=True) if title else ''}",
        f"Community: {subreddit.get_text(' ', strip=True) if subreddit else ''}",
        f"Preview: {body.get_text(' ', strip=True) if body else ''}",
    ]
    return "\n".join(parts)[:6000]


DEFAULT_TOPIC_DETAILS = {
    "politics": "politics, elections, government, or geopolitical conflict",
    "death or grief": "death, dying, bereavement, grief, funerals, or terminal illness",
    "divorce or breakups": "divorce, separation, breakups, infidelity, or romantic conflict",
    "cute animals": "cute, adorable, wholesome, or amusing animals",
}


def excluded_topics() -> list[str]:
    return [topic.strip() for topic in FILTER_EXCLUDED_TOPICS.split(",") if topic.strip()]


def topic_description(topic: str) -> str:
    return DEFAULT_TOPIC_DETAILS.get(topic.lower(), topic)


def allowed_instructions() -> str:
    topics = excluded_topics()
    details = ", ".join(topic_description(topic) for topic in topics)
    return (
        f"Reddit posts are allowed unless they are substantially about {details or 'any excluded topic'}. "
        "Judge only the excluded topics: never judge quality, explicitness, NSFW status, or substance. "
        "Answer true for allowed posts and false for posts substantially about "
        f"{details or 'any excluded topic'}."
    )


def decision_criteria() -> dict[str, str]:
    topics = ", ".join(excluded_topics()) or "any excluded topic"
    return {
        "true": "The post is not substantially about any excluded category.",
        "false": f"The post is substantially about {topics}.",
    }


async def decision_answers(text: str, client: httpx.AsyncClient) -> dict[str, float]:
    if DECISION_PROVIDER == "cloudflare":
        if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
            raise ValueError("CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are required when DECISION_PROVIDER=cloudflare")
        response = await client.post(
            f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/@cf/cloudflare/{CLOUDFLARE_MODEL}",
            headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
            json={
                "model": CLOUDFLARE_MODEL,
                "state": text,
                "questions": {"allowed": {"type": "noul", "instructions": allowed_instructions()}},
            },
        )
        response.raise_for_status()
        answer = response.json()["result"]["answers"]["allowed"]
        return {"allowed": float(answer["noul"])}
    if DECISION_PROVIDER == "openai":
        if not OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is required when DECISION_PROVIDER=openai")
        questions = [{"type": "predicate", "name": "allowed", "instructions": allowed_instructions()}]
        response = await client.post(
            f"{OPENAI_BASE_URL}/decisions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"model": OPENAI_MODEL, "input": text, "questions": questions},
        )
        response.raise_for_status()
        answers = response.json()["answers"]
        return {answer["name"]: float(answer["probability"]) for answer in answers}

    request = {
        "state": text,
        "questions": {
            "allowed": {
                "type": "noul",
                "instructions": allowed_instructions(),
                "criteria": decision_criteria(),
            }
        },
    }
    response = await client.post(f"{DECIDER_URL}/v1/systemone", json=request)
    response.raise_for_status()
    answer = response.json()["answers"]["allowed"]
    return {"allowed": float(answer["noul"])}


def cache_key(text: str) -> str:
    # Include the prompt hash so classifier changes re-classify instead of
    # serving verdicts cached under the old prompt.
    prompt = hashlib.sha256(allowed_instructions().encode()).hexdigest()[:16]
    return hashlib.sha256(
        f"{DECISION_PROVIDER}:{OPENAI_MODEL}:{prompt}:{FILTER_EXCLUDED_TOPICS}:{text}".encode()
    ).hexdigest()


def store_decision(key: str, result: bool) -> None:
    cache.put(key, result)
    persist_decision(key, result)


async def is_allowed(text: str, client: httpx.AsyncClient) -> str:
    """Classify post text: "allowed", "filtered", or "no response".

    Decision failures are never cached: a transient provider error must not
    permanently hide a post the way a real "filtered" verdict would.
    """
    key = cache_key(text)
    cached = cache.get(key)
    if cached is not None:
        METRICS["cache_hits"] += 1
        return "allowed" if cached else "filtered"
    started = asyncio.get_running_loop().time()
    try:
        answers = await decision_answers(text, client)
        allowed = answers["allowed"] >= ALLOWED_THRESHOLD
    except (httpx.HTTPError, KeyError, TypeError, ValueError, RuntimeError):
        METRICS["decision_failures"] += 1
        METRICS["decision_seconds"] += asyncio.get_running_loop().time() - started
        return "no response"
    METRICS["decisions"] += 1
    METRICS["decision_seconds"] += asyncio.get_running_loop().time() - started
    store_decision(key, allowed)
    return "allowed" if allowed else "filtered"


async def batch_codex(posts, client: httpx.AsyncClient) -> dict[str, bool]:
    uncached = []
    results = {}
    for index, post in enumerate(posts):
        text = post_text(post)
        key = cache_key(text)
        cached = cache.get(key)
        if cached is not None:
            METRICS["cache_hits"] += 1
            results[str(index)] = cached
        else:
            uncached.append({"id": str(index), "text": text, "key": key})
    if not uncached:
        return results
    started = asyncio.get_running_loop().time()
    METRICS["batch_requests"] += 1
    try:
        response = await client.post(f"{CODEX_ADAPTER_URL}/decide", json={"posts": uncached})
        response.raise_for_status()
        decisions = response.json()["decisions"]
        by_id = {item["id"]: bool(item["allowed"]) for item in decisions}
        for item in uncached:
            result = by_id[item["id"]]
            results[item["id"]] = result
            store_decision(item["key"], result)
            METRICS["decisions"] += 1
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        METRICS["decision_failures"] += len(uncached)
        # Do not persist failures: they are not verdicts. Uncached posts are
        # reclassified on the next request.
        for item in uncached:
            results[item["id"]] = False
    METRICS["decision_seconds"] += asyncio.get_running_loop().time() - started
    return results


async def filter_html(html: str, client: httpx.AsyncClient) -> str:
    page_started = asyncio.get_running_loop().time()
    soup = BeautifulSoup(html, "html.parser")
    posts = soup.select("#posts .post") or soup.select(".post")
    METRICS["page_requests"] += 1
    METRICS["posts_seen"] += len(posts)
    if DECISION_PROVIDER == "codex":
        results = await batch_codex(posts, client)
        allowed = ["allowed" if results[str(index)] else "filtered" for index in range(len(posts))]
    else:
        semaphore = asyncio.Semaphore(DECIDER_CONCURRENCY)

        async def classify(post) -> bool:
            async with semaphore:
                return await is_allowed(post_text(post), client)

        allowed = await asyncio.gather(*(classify(post) for post in posts))
    METRICS["page_seconds"] += asyncio.get_running_loop().time() - page_started
    # Keep failed classifications visible: replace the post card with a
    # short marker plus the original title as hover-to-reveal spoiler text,
    # so it is obvious what was filtered and why without exposing the post
    # body or accidentally reading the title.
    marked = False
    for post, status in zip(posts, allowed):
        if status == "allowed":
            continue
        if status == "no response":
            METRICS["posts_no_response"] += 1
        else:
            METRICS["posts_filtered"] += 1
        marked = True
        title_el = post.select_one(".post_title")
        title_text = title_el.get_text(" ", strip=True) if title_el else ""
        href = title_el.get("href") if title_el else None
        post.clear()
        marker = soup.new_tag("span")
        marker.string = status
        post.append(marker)
        if title_text:
            post.append(" ")
            small = soup.new_tag("small")
            small["class"] = "decider-spoiler"
            if href:
                link = soup.new_tag("a", href=href)
                link.string = title_text
                small.append(link)
            else:
                small.string = title_text
            post.append(small)
    if marked:
        style = soup.new_tag("style")
        style.string = DECIDER_SPOILER_CSS
        (soup.head if soup.head is not None else soup).append(style)
        script = soup.new_tag("script")
        script.string = DECIDER_SPOILER_JS
        (soup.body if soup.body is not None else soup).append(script)
    return str(soup)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(follow_redirects=False, timeout=DECIDER_TIMEOUT) as client:
        app.state.client = client
        yield


app = FastAPI(title="Redlib Decider Proxy", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request) -> Response:
    try:
        response = await request.app.state.client.get(f"{REDLIB_URL}/settings")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return Response(str(exc), status_code=503)
    return Response('{"status":"ready"}', media_type="application/json")


@app.get("/metrics")
async def metrics() -> Response:
    lines = []
    for name, value in METRICS.items():
        metric_type = "counter" if name.endswith(("requests", "seen", "hits", "decisions", "failures", "filtered", "no_response")) else "gauge"
        lines.append(f"# TYPE redlib_decider_{name} {metric_type}")
        lines.append(f"redlib_decider_{name} {value}")
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy(request: Request, path: str) -> Response:
    target = urljoin(f"{REDLIB_URL}/", path)
    if request.url.query:
        target = f"{target}?{request.url.query}"
    body = await request.body()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "accept-encoding"}
    }
    # httpx transparently decompresses upstream responses, but the proxy may
    # rewrite HTML. Request identity encoding so we never forward compressed
    # bytes after removing the upstream Content-Encoding header.
    headers["accept-encoding"] = "identity"
    # Redlib waits on Reddit upstream and can be slow on cold fetches; give
    # the fetch its own timeout and retry idempotent requests once before
    # failing the page with a clean 502 instead of an unhandled 500.
    upstream = None
    for attempt in range(2):
        try:
            upstream = await request.app.state.client.request(
                request.method,
                target,
                content=body,
                headers=headers,
                timeout=REDLIB_TIMEOUT,
            )
            break
        except httpx.TransportError:
            METRICS["upstream_failures"] += 1
            if request.method not in ("GET", "HEAD") or attempt:
                return Response("Bad Gateway: redlib upstream unreachable", status_code=502)
    if upstream is None:  # pragma: no cover - loop always sets or returns
        return Response("Bad Gateway: redlib upstream unreachable", status_code=502)
    content_type = upstream.headers.get("content-type", "")
    content = upstream.content
    if request.method == "GET" and "text/html" in content_type and not path.startswith(SKIP_PREFIXES):
        content = (await filter_html(content.decode(upstream.encoding or "utf-8", errors="replace"), request.app.state.client)).encode()
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in {"content-length", "content-encoding", "transfer-encoding", "set-cookie"}
    }
    response = Response(content, status_code=upstream.status_code, headers=response_headers, media_type=None)
    # Re-emit each Set-Cookie as its own header. Collapsing them into one
    # comma-joined header breaks Safari: the comma inside cookie Expires
    # dates makes the combined value ambiguous to strict parsers.
    for cookie in upstream.headers.get_list("set-cookie"):
        response.headers.append("set-cookie", cookie)
    return response
