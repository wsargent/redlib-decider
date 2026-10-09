from proxy.main import filter_html


class FakeResponse:
    def __init__(self, quality, political):
        self.quality = quality
        self.political = political

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "answers": {
                "quality": {"noul": self.quality},
                "political": {"noul": self.political},
            }
        }


class FakeClient:
    async def post(self, url, json):
        title = json["state"].lower()
        return FakeResponse(
            quality=0.1 if "bad" in title else 0.9,
            political=0.9 if "politics" in title else 0.1,
        )


async def test_filter_removes_low_quality_and_political_posts():
    html = '''<main id="posts"><div class="post" id="good"><h2 class="post_title">Good post</h2><div class="post_body post_preview">Useful detail</div></div><hr class="sep"/><div class="post" id="bad"><h2 class="post_title">Bad post</h2></div><hr class="sep"/><div class="post" id="politics"><h2 class="post_title">Politics post</h2></div></main>'''
    result = await filter_html(html, FakeClient())
    assert 'id="good"' in result
    assert 'id="bad"' not in result
    assert 'id="politics"' not in result
