import swipefile
from config import CATEGORIES_QUOTA_COST


class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Categories:
    def __init__(self, result):
        self.calls = []
        self._result = result

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return _Req(self._result)


class FakeYouTube:
    def __init__(self, result):
        self._categories = _Categories(result)

    def videoCategories(self):
        return self._categories


_API_RESPONSE = {
    "items": [
        {"id": "26", "snippet": {"title": "Howto & Style", "assignable": True}},
        {"id": "24", "snippet": {"title": "Entertainment", "assignable": True}},
    ]
}


def _budget():
    # Ample headroom so the per-call guard never trips.
    return swipefile.QuotaBudget(baseline=0, cap=10_000)


def test_fetch_video_categories_requests_snippet_and_region():
    """The fetch must request part='snippet' and pass the configured regionCode —
    videoCategories.list requires a region, and snippet carries the title."""
    yt = FakeYouTube(_API_RESPONSE)
    swipefile.fetch_video_categories(yt, "US", _budget())

    assert yt._categories.calls, "videoCategories().list(...) was never called"
    params = yt._categories.calls[0]
    assert params["part"] == "snippet"
    assert params["regionCode"] == "US"


def test_fetch_video_categories_maps_id_title_region():
    """Each API item maps to a categories record carrying the numeric id, its
    human-readable title, and the sourcing region."""
    yt = FakeYouTube(_API_RESPONSE)
    records = swipefile.fetch_video_categories(yt, "US", _budget())

    assert records == [
        {"category_id": "26", "title": "Howto & Style", "region_code": "US"},
        {"category_id": "24", "title": "Entertainment", "region_code": "US"},
    ]


def test_fetch_video_categories_charges_one_unit():
    """A successful fetch charges exactly CATEGORIES_QUOTA_COST against the budget."""
    budget = _budget()
    swipefile.fetch_video_categories(FakeYouTube(_API_RESPONSE), "US", budget)
    assert budget.run_units == CATEGORIES_QUOTA_COST


def test_fetch_video_categories_guard_stop_returns_empty():
    """When the per-call guard refuses (no headroom), the fetch returns [] so the
    caller skips persistence rather than crashing — matching the channel fetch."""
    budget = swipefile.QuotaBudget(baseline=10_000, cap=10_000)  # nothing left
    records = swipefile.fetch_video_categories(FakeYouTube(_API_RESPONSE), "US", budget)
    assert records == []
    assert budget.guard_stopped is True
