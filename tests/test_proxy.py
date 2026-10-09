from proxy.main import decision_answers, filter_html


class FakeResponse:
    def __init__(self, quality, political, death=0.1, relationship=0.1, cute_animal=0.1):
        self.quality = quality
        self.political = political
        self.death = death
        self.relationship = relationship
        self.cute_animal = cute_animal

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "answers": {
                "quality": {"noul": self.quality},
                "political": {"noul": self.political},
                "death": {"noul": self.death},
                "relationship": {"noul": self.relationship},
                "cute_animal": {"noul": self.cute_animal},
            }
        }


class OpenAIResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"answers": [{"name": name, "type": "predicate", "probability": 0.25} for name in ("quality", "political", "death", "relationship", "cute_animal")]}


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
            quality=0.1 if "bad" in title else 0.9,
            political=0.9 if "politics" in title else 0.1,
            relationship=0.9 if "breakup" in title else 0.1,
            cute_animal=0.9 if "cat" in title else 0.1,
        )


async def test_openai_decisions_payload(monkeypatch):
    monkeypatch.setattr("proxy.main.DECISION_PROVIDER", "openai")
    monkeypatch.setattr("proxy.main.OPENAI_API_KEY", "test-key")
    client = OpenAIClient()
    answers = await decision_answers("A post to classify", client)
    assert client.url.endswith("/decisions")
    assert client.request["model"] == "gpt-6-luna"
    assert {q["name"] for q in client.request["questions"]} == {"quality", "political", "death", "relationship", "cute_animal"}
    assert answers["cute_animal"] == 0.25


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


async def test_filter_fails_closed_when_decider_is_unavailable():
    class UnavailableClient:
        async def post(self, url, json):
            raise RuntimeError("decider unavailable")

    html = '''<main id="posts"><div class="post" id="unknown"><h2 class="post_title">Unknown post</h2></div></main>'''
    result = await filter_html(html, UnavailableClient())
    assert 'id="unknown"' not in result
