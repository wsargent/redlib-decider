from sqlite_utils import Database

import proxy.main as proxy_main
from proxy.main import decision_answers, filter_html


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
        proxy_main.persist_decision(key, True)
    db = Database(db_path)
    assert db["decisions"].count == 2
    assert [row["key"] for row in db["decisions"].rows_where(order_by="rowid")] == ["two", "three"]


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


async def test_filter_removes_low_quality_and_political_posts():
    html = '''<main id="posts"><div class="post" id="good"><h2 class="post_title">Good post</h2><div class="post_body post_preview">Useful detail</div></div><hr class="sep"/><div class="post" id="bad"><h2 class="post_title">Bad post</h2></div><hr class="sep"/><div class="post" id="politics"><h2 class="post_title">Politics post</h2></div></main>'''
    result = await filter_html(html, FakeClient())
    assert 'id="good"' in result
    assert 'id="bad"' not in result
    assert 'id="politics"' not in result


async def test_filter_removes_detail_page_post():
    html = '''<main><div class="post highlighted" id="detail"><h1 class="post_title">Cute cat after a breakup</h1><div class="post_body">A sad story</div></div></main>'''
    result = await filter_html(html, FakeClient())
    assert 'id="detail"' not in result


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
    assert 'id="bad"' not in result


async def test_filter_fails_closed_when_decider_is_unavailable():
    class UnavailableClient:
        async def post(self, url, json):
            raise RuntimeError("decider unavailable")

    html = '''<main id="posts"><div class="post" id="unknown"><h2 class="post_title">Unknown post</h2></div></main>'''
    result = await filter_html(html, UnavailableClient())
    assert 'id="unknown"' not in result
