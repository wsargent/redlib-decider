import asyncio

from sqlite_utils import Database

import httpx
import proxy.main as proxy_main
from fastapi.testclient import TestClient
from proxy.main import app, decision_answers, filter_html


class FakeResponse:
    def __init__(self, allowed):
        self.allowed = allowed

    def raise_for_status(self):
        pass

    def json(self):
        return {"answers": {"allowed": {"noul": self.allowed}}}


class OpenAIResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"answers": [{"name": "allowed", "type": "predicate", "probability": 0.75}]}


class OpenAIClient:
    def __init__(self):
        self.url = None
        self.request = None

    async def post(self, url, headers, json):
        self.url = url
        self.request = json
        return OpenAIResponse()


class FakeClient:
    async def post(self, url, json):
        title = json["state"].lower()
        return FakeResponse(
            allowed=0.1 if any(term in title for term in ("bad", "politics", "breakup", "cat")) else 0.9,
        )


def test_persistent_cache_is_bounded(tmp_path, monkeypatch):
    db_path = tmp_path / "decisions.sqlite3"
    monkeypatch.setattr(proxy_main, "DECISION_DB", str(db_path))
    monkeypatch.setattr(proxy_main, "CACHE_SIZE", 2)
    monkeypatch.setattr(proxy_main, "PERSISTENT_CACHE", True)
    for key in ("one", "two", "three"):
        proxy_main.persist_decision(key, True, "rules-1")
    db = Database(db_path)
    assert db["decisions"].count == 2
    rows = list(db["decisions"].rows_where(order_by="rowid"))
    assert [row["key"] for row in rows] == ["two", "three"]
    assert all(row["rules"] == "rules-1" for row in rows)


def test_persistent_cache_scopes_rows_by_rules(tmp_path, monkeypatch):
    db_path = tmp_path / "decisions.sqlite3"
    monkeypatch.setattr(proxy_main, "DECISION_DB", str(db_path))
    monkeypatch.setattr(proxy_main, "CACHE_SIZE", 10)
    monkeypatch.setattr(proxy_main, "PERSISTENT_CACHE", True)
    stale_rules = "0" * 32
    current_rules = proxy_main.rules_fingerprint(True)
    proxy_main.persist_decision("stale", True, stale_rules)
    proxy_main.persist_decision("fresh", False, current_rules)
    proxy_main.cache.values.clear()
    proxy_main.load_persistent_cache()
    # Verdicts from other rule sets are neither loaded nor kept.
    assert proxy_main.cache.get("stale") is None
    assert proxy_main.cache.get("fresh") is False
    assert Database(db_path)["decisions"].count == 1


def test_legacy_table_without_rules_is_recreated(tmp_path, monkeypatch):
    db_path = tmp_path / "decisions.sqlite3"
    monkeypatch.setattr(proxy_main, "DECISION_DB", str(db_path))
    monkeypatch.setattr(proxy_main, "PERSISTENT_CACHE", True)
    db = Database(db_path)
    db["decisions"].insert({"key": "legacy", "allowed": 1}, pk="key")
    proxy_main.load_persistent_cache()
    table = Database(db_path)["decisions"]
    assert "rules" in table.columns_dict
    assert table.count == 0


async def test_cloudflare_decisions_payload(monkeypatch):
    monkeypatch.setattr("proxy.main.DECISION_PROVIDER", "cloudflare")
    monkeypatch.setattr("proxy.main.CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setattr("proxy.main.CLOUDFLARE_API_TOKEN", "token")

    class CloudflareResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"answers": {"allowed": {"noul": 0.8}}}}

    class CloudflareClient:
        async def post(self, url, headers, json):
            assert url.endswith("/accounts/account/ai/run/@cf/cloudflare/clef-flash")
            assert headers["Authorization"] == "Bearer token"
            assert json["model"] == "clef-flash"
            assert set(json["questions"]) == {"allowed"}
            return CloudflareResponse()

    answers = await decision_answers("A post to classify", CloudflareClient())
    assert answers["allowed"] == 0.8


async def test_openai_decisions_payload(monkeypatch):
    monkeypatch.setattr("proxy.main.DECISION_PROVIDER", "openai")
    monkeypatch.setattr("proxy.main.OPENAI_API_KEY", "test-key")
    client = OpenAIClient()
    answers = await decision_answers("A post to classify", client)
    assert client.url.endswith("/decisions")
    assert client.request["model"] == "gpt-6-luna"
    assert {q["name"] for q in client.request["questions"]} == {"allowed"}
    assert answers["allowed"] == 0.75


async def test_filter_topics_are_configurable(monkeypatch):
    monkeypatch.setattr("proxy.main.DECISION_PROVIDER", "cloudflare")
    monkeypatch.setattr("proxy.main.CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setattr("proxy.main.CLOUDFLARE_API_TOKEN", "token")
    monkeypatch.setattr("proxy.main.FILTER_EXCLUDED_TOPICS", "spoilers,crypto scams")

    class CloudflareClient:
        async def post(self, url, headers, json):
            instructions = json["questions"]["allowed"]["instructions"]
            assert "spoilers" in instructions
            assert "crypto scams" in instructions
            assert "politics" not in instructions
            return type("Response", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"result": {"answers": {"allowed": {"noul": 0.8}}}},
            })()

    answers = await decision_answers("A post to classify", CloudflareClient())
    assert answers["allowed"] == 0.8


def test_circuit_breaker_opens_and_bypasses(monkeypatch):
    monkeypatch.setattr(proxy_main, "SAMPLING_COUNT", 1)
    monkeypatch.setattr(proxy_main.DECISION_BREAKER, "failure_threshold", 2)
    monkeypatch.setattr(proxy_main.DECISION_BREAKER, "cooldown_seconds", 60)
    proxy_main.DECISION_BREAKER.record_success()

    class DownClient:
        def __init__(self):
            self.calls = 0

        async def post(self, url, json):
            self.calls += 1
            raise RuntimeError("provider down")

    client = DownClient()
    first = asyncio.run(proxy_main.is_allowed("Title: breaker one", client))
    # Fail-open: once the breaker trips, posts bypass filtering.
    assert first == "allowed"
    calls_after_open = client.calls
    assert calls_after_open == 2  # threshold reached on the 2nd failure
    second = asyncio.run(proxy_main.is_allowed("Title: breaker two", client))
    assert second == "allowed"
    assert client.calls == calls_after_open  # no provider calls while open
    proxy_main.DECISION_BREAKER.record_success()


def test_circuit_breaker_recovers_after_cooldown(monkeypatch):
    import time as time_mod

    monkeypatch.setattr(proxy_main, "SAMPLING_COUNT", 1)
    monkeypatch.setattr(proxy_main.DECISION_BREAKER, "failure_threshold", 2)
    monkeypatch.setattr(proxy_main.DECISION_BREAKER, "cooldown_seconds", 60)
    proxy_main.DECISION_BREAKER.record_success()

    class DownClient:
        async def post(self, url, json):
            raise RuntimeError("provider down")

    asyncio.run(proxy_main.is_allowed("Title: breaker trip", DownClient()))
    assert proxy_main.DECISION_BREAKER.opened_at is not None
    # Simulate the cooldown fully elapsing (half-open trial).
    proxy_main.DECISION_BREAKER.opened_at = time_mod.monotonic() - 120

    class OkClient:
        async def post(self, url, json):
            return FakeResponse(allowed=0.9)

    verdict = asyncio.run(proxy_main.is_allowed("Title: breaker recovery", OkClient()))
    assert verdict == "allowed"
    assert proxy_main.DECISION_BREAKER.consecutive_failures == 0
    assert proxy_main.DECISION_BREAKER.opened_at is None
    proxy_main.DECISION_BREAKER.record_success()


def test_cache_key_changes_with_filter_topics(monkeypatch):
    monkeypatch.setattr("proxy.main.FILTER_EXCLUDED_TOPICS", "politics")
    first = proxy_main.cache_key("same post")
    monkeypatch.setattr("proxy.main.FILTER_EXCLUDED_TOPICS", "spoilers")
    assert proxy_main.cache_key("same post") != first


def test_cache_key_changes_with_nsfw_mode():
    assert proxy_main.cache_key("same post", True) != proxy_main.cache_key("same post", False)


def test_cache_key_changes_with_cloudflare_model(monkeypatch):
    monkeypatch.setattr(proxy_main, "CLOUDFLARE_MODEL", "clef-flash")
    flash = proxy_main.cache_key("same post")
    monkeypatch.setattr(proxy_main, "CLOUDFLARE_MODEL", "clef")
    assert proxy_main.cache_key("same post") != flash


def test_sampling_retries_until_it_has_n_successes(monkeypatch):
    monkeypatch.setattr(proxy_main, "SAMPLING_COUNT", 2)

    class QueueClient:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = 0

        async def post(self, url, json):
            self.calls += 1
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return FakeResponse(allowed=item)

    text = "Title: Averaging test post\nCommunity: r/test\nPreview: "
    key = proxy_main.cache_key(text)
    client = QueueClient([RuntimeError("transient"), 0.0, 0.8])
    verdict = asyncio.run(proxy_main.is_allowed(text, client))
    # A failed sample does not count toward the quota: it retries until it
    # holds two successful samples, then averages them (0.0 + 0.8) / 2.
    assert verdict == "filtered"
    assert client.calls == 3
    assert proxy_main.cache.get(key) is False


def test_sampling_caps_attempts_and_serves_no_response(monkeypatch):
    monkeypatch.setattr(proxy_main, "SAMPLING_COUNT", 2)

    class DownClient:
        def __init__(self):
            self.calls = 0

        async def post(self, url, json):
            self.calls += 1
            raise RuntimeError("provider down")

    text = "Title: Provider down test post\nCommunity: r/test\nPreview: "
    failures_before = proxy_main.METRICS["decision_failures"]
    client = DownClient()
    verdict = asyncio.run(proxy_main.is_allowed(text, client))
    # Attempt cap is twice the sample count; nothing is cached so the post
    # re-classifies on its next load.
    assert verdict == "no response"
    assert client.calls == 4
    assert proxy_main.METRICS["decision_failures"] == failures_before + 4
    assert proxy_main.cache.get(proxy_main.cache_key(text)) is None


def test_instructions_follow_nsfw_cookie(monkeypatch):
    allowed = proxy_main.allowed_instructions(True)
    hidden = proxy_main.allowed_instructions(False)
    assert "adult communities" in allowed
    assert "adult communities" not in hidden
    assert "does not want to see sexual or explicit content" in hidden

    # If explicit content is configured as an excluded topic, it is filtered
    # for every reader and the cookie changes nothing.
    monkeypatch.setattr("proxy.main.FILTER_EXCLUDED_TOPICS", "nsfw")
    assert "adult communities" not in proxy_main.allowed_instructions(True)
    assert "sexually explicit" not in proxy_main.allowed_instructions(True)


async def test_ready_probes_redlib(monkeypatch):
    class ReadyClient:
        async def get(self, url):
            assert url.endswith("/settings")
            return type("Response", (), {"raise_for_status": lambda self: None})()

    app.state.client = ReadyClient()
    response = await app.routes[[route.path for route in app.routes].index("/ready")].endpoint(type("Request", (), {"app": app})())
    assert response.status_code == 200


async def test_filter_marks_low_quality_and_political_posts():
    html = '''<main id="posts"><div class="post" id="good"><h2 class="post_title">Good post</h2><div class="post_body post_preview">Useful detail</div></div><hr class="sep"/><div class="post" id="bad"><a class="post_title" href="/r/test/comments/1/bad/">Bad post</a><div class="post_body post_preview">Awful detail</div></div><hr class="sep"/><div class="post" id="politics"><h2 class="post_title">Politics post</h2></div></main>'''
    result = await filter_html(html, FakeClient())
    assert 'id="good"' in result
    assert 'id="bad"' in result
    assert 'id="politics"' in result
    assert result.count("filtered") == 2
    # The marker keeps the original title in small text, linked to the post.
    assert 'href="/r/test/comments/1/bad/"' in result
    assert "Bad post" in result
    assert "Politics post" in result
    assert "<small" in result
    assert 'class="decider-spoiler"' in result
    # The hover-to-reveal stylesheet is injected once per filtered page.
    assert result.count(".decider-spoiler") >= 1
    # Tap-to-toggle reveal for touch devices is injected with the style.
    assert "addEventListener" in result
    # Post bodies must not survive the replacement.
    assert "Awful detail" not in result


async def test_filter_marks_detail_page_post():
    html = '''<main><div class="post highlighted" id="detail"><h1 class="post_title">Cute cat after a breakup</h1><div class="post_body">A sad story</div></div></main>'''
    result = await filter_html(html, FakeClient())
    assert 'id="detail"' in result
    assert "A sad story" not in result
    assert "filtered" in result
    assert "Cute cat after a breakup" in result
    assert "<small" in result


async def test_codex_batches_uncached_posts(monkeypatch):
    monkeypatch.setattr(proxy_main, "DECISION_PROVIDER", "codex")
    monkeypatch.setattr(proxy_main, "CODEX_ADAPTER_URL", "http://codex")

    class CodexClient:
        def __init__(self):
            self.calls = 0

        async def post(self, url, json):
            self.calls += 1
            assert url == "http://codex/decide"
            return type("Response", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"decisions": [
                    {"id": post["id"], "allowed": post["id"] == "0"}
                    for post in json["posts"]
                ]},
            })()

    client = CodexClient()
    html = '<main id="posts"><div class="post" id="good"><h2 class="post_title">Good</h2></div><div class="post" id="bad"><h2 class="post_title">Bad</h2></div></main>'
    result = await filter_html(html, client)
    assert client.calls == 1
    assert 'id="good"' in result
    assert 'id="bad"' in result
    assert "filtered" in result
    assert "Bad" in result
    assert "<small" in result


async def test_filter_marks_no_response_when_decider_is_unavailable():
    class UnavailableClient:
        async def post(self, url, json):
            raise RuntimeError("decider unavailable")

    html = '''<main id="posts"><div class="post" id="unknown"><h2 class="post_title">Unknown post</h2></div></main>'''
    result = await filter_html(html, UnavailableClient())
    assert 'id="unknown"' in result
    assert "no response" in result
    assert "Unknown post" in result
    assert "<small" in result
    assert 'class="decider-spoiler"' in result
    # Failures are not verdicts and must not be cached as "filtered".
    assert proxy_main.cache.get(proxy_main.cache_key("Unknown post")) is None


def test_proxy_preserves_multiple_set_cookie_headers():
    class Upstream:
        status_code = 200
        content = b"ok"
        encoding = "utf-8"
        headers = httpx.Headers(
            [
                ("content-type", "text/plain"),
                ("set-cookie", "theme=dark; Path=/; Expires=Tue, 12 Oct 2027 03:16:29 GMT"),
                ("set-cookie", "layout=card; Path=/"),
            ]
        )

    class FakeUpstream:
        async def request(self, method, url, content=None, headers=None, timeout=None):
            return Upstream()

    app.state.client = FakeUpstream()
    client = TestClient(app)
    response = client.get("/plain")
    assert response.status_code == 200
    # Each Set-Cookie must be re-emitted as its own header: joining them
    # with commas breaks Safari's parser (Expires dates contain commas).
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert any(cookie.startswith("theme=dark") for cookie in cookies)
    assert any(cookie.startswith("layout=card") for cookie in cookies)
