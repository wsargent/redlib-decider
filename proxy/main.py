from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Response

REDLIB_URL = os.getenv("REDLIB_URL", "http://redlib:8080").rstrip("/")
DECIDER_URL = os.getenv("DECIDER_URL", "http://decider:8099").rstrip("/")
QUALITY_THRESHOLD = float(os.getenv("QUALITY_THRESHOLD", "0.55"))
POLITICS_THRESHOLD = float(os.getenv("POLITICS_THRESHOLD", "0.50"))
DEATH_THRESHOLD = float(os.getenv("DEATH_THRESHOLD", "0.50"))
RELATIONSHIP_THRESHOLD = float(os.getenv("RELATIONSHIP_THRESHOLD", "0.50"))
CUTE_ANIMAL_THRESHOLD = float(os.getenv("CUTE_ANIMAL_THRESHOLD", "0.50"))
DECIDER_TIMEOUT = float(os.getenv("DECIDER_TIMEOUT", "20"))
CACHE_SIZE = int(os.getenv("DECISION_CACHE_SIZE", "512"))

SKIP_PREFIXES = ("/static/", "/css/", "/js/", "/favicon")


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


def post_text(post) -> str:
    title = post.select_one(".post_title")
    body = post.select_one(".post_body.post_preview")
    subreddit = post.select_one(".post_subreddit")
    score = post.select_one(".post_score")
    comments = post.select_one(".post_comments")
    parts = [
        f"Title: {title.get_text(' ', strip=True) if title else ''}",
        f"Community: {subreddit.get_text(' ', strip=True) if subreddit else ''}",
        f"Preview: {body.get_text(' ', strip=True) if body else ''}",
        f"Score: {score.get_text(' ', strip=True) if score else ''}",
        f"Comments: {comments.get_text(' ', strip=True) if comments else ''}",
    ]
    return "\n".join(parts)[:6000]


async def is_allowed(text: str, client: httpx.AsyncClient) -> bool:
    key = hashlib.sha256(text.encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return cached

    request = {
        "state": text,
        "questions": {
            "quality": {
                "type": "noul",
                "instructions": "Is this Reddit post worth showing to a reader seeking informative, original, or engaging content?",
                "criteria": {
                    "true": "The post has meaningful substance, useful information, a clear question, original insight, or genuine discussion value.",
                    "false": "The post is low-effort, repetitive, spammy, engagement bait, or has little useful content.",
                },
            },
            "political": {
                "type": "noul",
                "instructions": "Is this Reddit post substantially about politics or political controversy?",
                "criteria": {
                    "true": "The post concerns elections, political parties, politicians, government policy, legislation, political movements, geopolitical conflict, or political commentary.",
                    "false": "The post is not substantially political, even if it briefly mentions a public figure, country, or current event.",
                },
            },
            "death": {
                "type": "noul",
                "instructions": "Is this Reddit post substantially about death, dying, bereavement, grief, funerals, or serious terminal illness?",
                "criteria": {
                    "true": "The post substantially concerns someone or an animal dying, a death, bereavement, grief, a funeral, or terminal illness.",
                    "false": "The post is not substantially about death or grief.",
                },
            },
            "relationship": {
                "type": "noul",
                "instructions": "Is this Reddit post substantially about divorce, separation, a breakup, or a failing romantic relationship?",
                "criteria": {
                    "true": "The post concerns divorce, separation, a breakup, infidelity, or serious romantic relationship conflict.",
                    "false": "The post is not substantially about divorce, a breakup, or romantic relationship conflict.",
                },
            },
            "cute_animal": {
                "type": "noul",
                "instructions": "Is this Reddit post primarily presenting a cute, adorable, wholesome, or amusing animal?",
                "criteria": {
                    "true": "The post primarily presents a cute, adorable, wholesome, or amusing animal, including a pet photo or animal video.",
                    "false": "The post is not primarily cute-animal content.",
                },
            },
        },
    }
    try:
        response = await client.post(f"{DECIDER_URL}/v1/systemone", json=request)
        response.raise_for_status()
        answers = response.json()["answers"]
        quality = float(answers["quality"]["noul"])
        political = float(answers["political"]["noul"])
        death = float(answers["death"]["noul"])
        relationship = float(answers["relationship"]["noul"])
        cute_animal = float(answers["cute_animal"]["noul"])
        result = (
            quality >= QUALITY_THRESHOLD
            and political < POLITICS_THRESHOLD
            and death < DEATH_THRESHOLD
            and relationship < RELATIONSHIP_THRESHOLD
            and cute_animal < CUTE_ANIMAL_THRESHOLD
        )
    except (httpx.HTTPError, KeyError, TypeError, ValueError, RuntimeError):
        # Do not silently bypass explicit exclusion rules when the model is
        # unavailable. The Compose dependency keeps normal startup gated on
        # Decider readiness; this protects later model failures.
        result = False
    cache.put(key, result)
    return result


async def filter_html(html: str, client: httpx.AsyncClient) -> str:
    soup = BeautifulSoup(html, "html.parser")
    posts = soup.select("#posts .post") or soup.select(".post")
    for post in posts:
        if not await is_allowed(post_text(post), client):
            separator = post.find_next_sibling("hr", class_="sep")
            post.decompose()
            if separator:
                separator.decompose()
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
    upstream = await request.app.state.client.request(
        request.method, target, content=body, headers=headers
    )
    content_type = upstream.headers.get("content-type", "")
    content = upstream.content
    if request.method == "GET" and "text/html" in content_type and not path.startswith(SKIP_PREFIXES):
        content = (await filter_html(content.decode(upstream.encoding or "utf-8", errors="replace"), request.app.state.client)).encode()
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in {"content-length", "content-encoding", "transfer-encoding"}
    }
    return Response(content, status_code=upstream.status_code, headers=response_headers, media_type=None)
