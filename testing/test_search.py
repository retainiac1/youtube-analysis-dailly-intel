import swipefile


class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Search:
    def __init__(self):
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return _Req({"items": []})


class FakeYouTube:
    def __init__(self):
        self._search = _Search()

    def search(self):
        return self._search


def test_search_videos_biases_to_english_no_region_filter():
    """Lock the existing English soft-bias on search.list. relevanceLanguage='en'
    is already present (a weak bias the foreign-flood run proved insufficient — the
    motivation for the change-B detector); this guards it from being silently
    dropped. regionCode must NOT be added: English content comes from US/UK/CA/AU,
    so a region filter would wrongly drop non-US English creators."""
    yt = FakeYouTube()
    swipefile.search_videos(yt, "build habits")

    assert yt._search.calls, "search().list(...) was never called"
    params = yt._search.calls[0]
    assert params.get("relevanceLanguage") == "en"
    assert "regionCode" not in params
    assert params["q"] == "build habits"
    assert params["type"] == "video"
