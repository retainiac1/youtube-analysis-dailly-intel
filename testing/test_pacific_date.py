import config


# config.pacific_date derives the Pacific calendar date from an Eastern,
# offset-bearing timestamp. Eastern is 3 hours ahead of Pacific, so the Pacific
# day rolls over at 03:00 Eastern. These cases pin the boundary.

def test_afternoon_maps_same_day():
    # 14:30 Eastern is 11:30 Pacific — same calendar date.
    assert config.pacific_date("2026-06-07T14:30:00-04:00") == "2026-06-07"


def test_just_after_pacific_midnight_same_day():
    # 03:30 Eastern is 00:30 Pacific — already the new Pacific day.
    assert config.pacific_date("2026-06-07T03:30:00-04:00") == "2026-06-07"


def test_just_before_pacific_midnight_previous_day():
    # 02:30 Eastern is 23:30 Pacific the PREVIOUS day — the early-morning window
    # where the Eastern and Pacific dates disagree.
    assert config.pacific_date("2026-06-07T02:30:00-04:00") == "2026-06-06"


def test_exactly_pacific_midnight_rolls_over():
    # 03:00 Eastern == 00:00 Pacific exactly -> the new Pacific day.
    assert config.pacific_date("2026-06-07T03:00:00-04:00") == "2026-06-07"


def test_winter_offset_boundary():
    # Standard time (-05:00): 02:30 Eastern == 23:30 Pacific previous day.
    assert config.pacific_date("2026-01-15T02:30:00-05:00") == "2026-01-14"
    assert config.pacific_date("2026-01-15T03:30:00-05:00") == "2026-01-15"


def test_default_argument_uses_now(monkeypatch):
    # With no argument it derives from now_local_iso().
    monkeypatch.setattr(config, "now_local_iso", lambda: "2026-06-07T02:30:00-04:00")
    assert config.pacific_date() == "2026-06-06"
