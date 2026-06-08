from datetime import datetime

import swipefile

# A far-past cutoff so the window gate never fires except where tested explicitly.
PAST_CUTOFF = datetime.fromisoformat("2000-01-01T00:00:00+00:00")
RECENT = "2026-06-07T00:00:00Z"


def fake_video(video_id="v1", duration="PT1M", view_count="200000",
               published_at=RECENT, category_id="22", audio="en",
               made_for_kids=False):
    """A clean, qualifying Short by default; pass overrides to trip one gate.
    Mirrors the YouTube videos.list item shape."""
    snippet = {
        "title": "t", "channelId": "c1", "channelTitle": "ch",
        "publishedAt": published_at, "description": "d",
        "categoryId": category_id,
        "thumbnails": {"high": {"url": "http://x"}}, "tags": ["a"],
    }
    if audio is not None:
        snippet["defaultAudioLanguage"] = audio
    stats = {}
    if view_count is not None:
        stats["viewCount"] = view_count
    return {
        "id": video_id,
        "snippet": snippet,
        "statistics": stats,
        "contentDetails": {"duration": duration, "definition": "hd", "caption": "true"},
        "status": {"madeForKids": made_for_kids},
    }


def filt(videos):
    return swipefile.filter_videos(videos, 100_000, 180, PAST_CUTOFF)


# --- regression: the crash --------------------------------------------------

def test_missing_duration_excluded_not_crashing():
    # contentDetails present but no "duration" key — the exact KeyError case.
    v = fake_video()
    del v["contentDetails"]["duration"]
    kept, drops = filt([v])
    assert kept == []
    assert drops["missing_duration"] == 1
    assert sum(drops.values()) == 1


def test_missing_contentdetails_entirely_excluded():
    v = fake_video()
    del v["contentDetails"]
    kept, drops = filt([v])
    assert kept == []
    assert drops["missing_duration"] == 1


def test_hidden_views_excluded_under_missing_views():
    v = fake_video(view_count=None)        # statistics has no viewCount
    kept, drops = filt([v])
    assert kept == []
    assert drops["missing_views"] == 1
    assert drops["missing_duration"] == 0  # kept distinct from missing_duration


# --- per-gate attribution ---------------------------------------------------

def test_clean_video_kept():
    kept, drops = filt([fake_video()])
    assert len(kept) == 1
    assert sum(drops.values()) == 0


def test_each_gate_attributes_correctly():
    cases = {
        "made_for_kids": fake_video(made_for_kids=True),
        "category": fake_video(category_id="10"),       # blocked (Music)
        "language": fake_video(audio="fr"),
        "duration": fake_video(duration="PT4M"),        # 240s > 180
        "views": fake_video(view_count="50000"),        # < 100k
    }
    for gate, video in cases.items():
        kept, drops = filt([video])
        assert kept == [], gate
        assert drops[gate] == 1, gate
        assert sum(drops.values()) == 1, gate


def test_window_gate_drops_out_of_window():
    recent_cut = datetime.fromisoformat("2026-06-06T00:00:00+00:00")
    old = fake_video(published_at="2026-06-01T00:00:00Z")   # before cutoff
    kept, drops = swipefile.filter_videos([old], 100_000, 180, recent_cut)
    assert kept == []
    assert drops["window"] == 1


def test_missing_published_at_fails_closed_to_window():
    v = fake_video()
    del v["snippet"]["publishedAt"]
    recent_cut = datetime.fromisoformat("2026-06-06T00:00:00+00:00")
    kept, drops = swipefile.filter_videos([v], 100_000, 180, recent_cut)
    assert kept == []
    assert drops["window"] == 1


# --- sum invariant on a mixed batch -----------------------------------------

def test_sum_invariant_mixed_batch():
    recent_cut = datetime.fromisoformat("2026-06-06T00:00:00+00:00")
    missing_dur = fake_video("md")
    del missing_dur["contentDetails"]["duration"]
    batch = [
        fake_video("clean"),                                  # kept
        fake_video("kids", made_for_kids=True),               # made_for_kids
        fake_video("cat", category_id="24"),                  # category
        fake_video("lang", audio="es"),                       # language
        fake_video("long", duration="PT5M"),                  # duration
        fake_video("low", view_count="1000"),                 # views
        missing_dur,                                          # missing_duration
        fake_video("hidden", view_count=None),                # missing_views
        fake_video("old", published_at="2026-05-01T00:00:00Z"),  # window
    ]
    kept, drops = swipefile.filter_videos(batch, 100_000, 180, recent_cut)
    assert len(kept) == 1
    assert sum(drops.values()) == len(batch) - len(kept)
    assert drops["window"] == 1 and drops["missing_duration"] == 1 and drops["missing_views"] == 1


# --- independent views-only diagnostic --------------------------------------

def test_views_below_min_is_independent_of_other_gates():
    batch = [
        fake_video("a", view_count="1000"),                       # below
        fake_video("b", view_count="2000", published_at="2026-05-01T00:00:00Z"),  # below AND out-of-window
        fake_video("c", view_count=None),                         # hidden
        fake_video("d", view_count="500000"),                     # above (not counted)
    ]
    below, hidden = swipefile.views_below_min(batch, 100_000)
    assert below == 2     # counts the out-of-window one too — independent of window gate
    assert hidden == 1
