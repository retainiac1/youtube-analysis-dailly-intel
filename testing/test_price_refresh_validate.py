"""Pure validation layers for the price-refresh agent (no DB, no network)."""
import pytest

from price_refresh import validate as v

# A clean two-model payload: each value carries positive-float input/output rates.
GOOD = {
    "anthropic:claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "openai:gpt-5.4-nano": {"input": 0.20, "output": 1.25},
}
CANON = set(GOOD)


# --- check_structure --------------------------------------------------------

def test_check_structure_clean_payload_passes():
    assert v.check_structure(GOOD, CANON).ok


def test_check_structure_missing_key_fails_naming_key():
    payload = {"openai:gpt-5.4-nano": {"input": 0.20, "output": 1.25}}
    res = v.check_structure(payload, CANON)
    assert not res.ok
    assert "anthropic:claude-haiku-4-5" in res.reason


def test_check_structure_extra_key_fails_naming_key():
    payload = {**GOOD, "xai:grok-4.3": {"input": 0.20, "output": 0.50}}
    res = v.check_structure(payload, CANON)
    assert not res.ok
    assert "xai:grok-4.3" in res.reason


def test_check_structure_non_float_value_fails_naming_key():
    payload = {**GOOD,
               "openai:gpt-5.4-nano": {"input": "cheap", "output": 1.25}}
    res = v.check_structure(payload, CANON)
    assert not res.ok
    assert "openai:gpt-5.4-nano" in res.reason


def test_check_structure_non_positive_value_fails():
    payload = {**GOOD,
               "openai:gpt-5.4-nano": {"input": 0.0, "output": 1.25}}
    res = v.check_structure(payload, CANON)
    assert not res.ok
    assert "openai:gpt-5.4-nano" in res.reason


# --- check_magnitude --------------------------------------------------------

def test_check_magnitude_in_band_passes():
    assert v.check_magnitude(GOOD, lo=0.02, hi=200.0).ok


def test_check_magnitude_1000x_unit_error_fails():
    # A 1000x extraction error (dollars-per-token read as per-MTok) blows the ceiling.
    payload = {**GOOD,
               "anthropic:claude-haiku-4-5": {"input": 1000.0, "output": 5000.0}}
    assert not v.check_magnitude(payload, lo=0.02, hi=200.0).ok


def test_check_magnitude_per_token_value_fails():
    # A per-token value (e.g. $0.000001/token) falls under the per-MTok floor.
    payload = {**GOOD,
               "openai:gpt-5.4-nano": {"input": 0.0000002, "output": 0.0000012}}
    assert not v.check_magnitude(payload, lo=0.02, hi=200.0).ok


# --- check_output_gt_input (SOFT) -------------------------------------------

def test_check_output_gt_input_flags_inversion_not_reject():
    # Output below input is unusual but not impossible; it FLAGS, never rejects.
    payload = {"m": {"input": 5.0, "output": 1.0}}
    flagged = v.check_output_gt_input(payload)
    assert "m" in flagged


def test_check_output_gt_input_normal_no_flags():
    assert v.check_output_gt_input(GOOD) == []


# --- classify_delta ---------------------------------------------------------

def test_classify_delta_small_move_is_auto():
    res = v.classify_delta(1.07, 1.0, 0.10)
    assert res.outcome == v.AUTO
    assert res.direction == "up"
    assert res.pct == pytest.approx(0.07)


def test_classify_delta_large_move_is_review():
    res = v.classify_delta(1.40, 1.0, 0.10)
    assert res.outcome == v.REVIEW
    assert res.direction == "up"


def test_classify_delta_large_drop_is_review_down():
    res = v.classify_delta(0.60, 1.0, 0.10)
    assert res.outcome == v.REVIEW
    assert res.direction == "down"
    assert res.pct == pytest.approx(-0.40)


# --- check_cross_source (THREE outcomes) ------------------------------------

def test_cross_source_agree_passes():
    # Both sources concur (even on a moved value): agree, carry the agreed number.
    res = v.check_cross_source(5.0, 5.0, last_kept=4.0, tol=0.05)
    assert res.outcome == v.CROSS_AGREE
    assert res.value == pytest.approx(5.0)


def test_cross_source_lagging_source_passes_moved_value_through():
    # a moved to 5.0, b still matches last_kept 1.0 -> NOT a reject; pass the MOVED
    # value (a) through to the delta band, recording which source moved.
    res = v.check_cross_source(5.0, 1.0, last_kept=1.0, tol=0.05)
    assert res.outcome == v.CROSS_PASSTHROUGH
    assert res.value == pytest.approx(5.0)
    assert res.moved_source == "a"


def test_cross_source_neither_anchors_is_conflict():
    # Two independent sources disagree AND neither matches last_kept -> garbage.
    res = v.check_cross_source(5.0, 3.0, last_kept=1.0, tol=0.05)
    assert res.outcome == v.CROSS_CONFLICT
    assert res.value is None


# --- classify_price (precedence) --------------------------------------------

def test_classify_price_small_change_is_auto():
    res = v.classify_price(5.2, 5.0, lo=0.02, hi=200.0, threshold=0.10)
    assert res.outcome == v.AUTO


def test_classify_price_large_change_is_review():
    res = v.classify_price(7.0, 5.0, lo=0.02, hi=200.0, threshold=0.10)
    assert res.outcome == v.REVIEW
    assert res.direction == "up"


def test_classify_price_unit_error_wins_over_delta_band():
    # A value that is BOTH a >10% move AND a unit error must REJECT: magnitude runs
    # BEFORE the delta band, so nonsense is discarded, not mistaken for a big move.
    res = v.classify_price(0.0001, 5.0, lo=0.02, hi=200.0, threshold=0.10)
    assert res.outcome == v.REJECT


def test_classify_price_soft_flag_upgrades_auto_to_review():
    # A soft flag (inversion / lagging-source passthrough) upgrades an otherwise-AUTO
    # small move to REVIEW, but never rescues a REJECT.
    res = v.classify_price(5.2, 5.0, lo=0.02, hi=200.0, threshold=0.10, flagged=True)
    assert res.outcome == v.REVIEW


# --- to_per_1m (unit normalization, the ONE conversion path) ----------------

def test_to_per_1m_page_value_unchanged():
    # Provider pages already quote per-1M-tokens.
    assert v.to_per_1m(1.25, v.UNIT_PER_1M) == pytest.approx(1.25)


def test_to_per_1m_per_token_float_scales_to_1m():
    # LiteLLM quotes a per-token float.
    assert v.to_per_1m(1.25e-6, v.UNIT_PER_TOKEN) == pytest.approx(1.25)


def test_to_per_1m_per_token_string_scales_identically():
    # OpenRouter quotes per-token as a STRING; it normalizes to the same per-1M number.
    assert v.to_per_1m("0.00000125", v.UNIT_PER_TOKEN) == pytest.approx(1.25)


def test_to_per_1m_all_three_shapes_agree():
    # The whole point: page / LiteLLM-float / OpenRouter-string land on one value.
    page = v.to_per_1m(2.5, v.UNIT_PER_1M)
    litellm = v.to_per_1m(2.5e-6, v.UNIT_PER_TOKEN)
    openrouter = v.to_per_1m("0.0000025", v.UNIT_PER_TOKEN)
    assert page == pytest.approx(litellm) == pytest.approx(openrouter)


def test_to_per_1m_rejects_bool():
    with pytest.raises(ValueError):
        v.to_per_1m(True, v.UNIT_PER_1M)


def test_to_per_1m_rejects_non_numeric():
    with pytest.raises(ValueError):
        v.to_per_1m("cheap", v.UNIT_PER_TOKEN)


def test_to_per_1m_unknown_unit_raises():
    with pytest.raises(ValueError, match="unit"):
        v.to_per_1m(1.0, "per_kilo")


# --- classify_cell (match / drift / mismatch for one field pair) ------------

def test_classify_cell_within_tolerance_is_match():
    # 1% apart, tolerance 2% -> match.
    assert v.classify_cell(
        1.00, 1.01, tolerance_pct=0.02, review_band_pct=0.10) == v.CELL_MATCH


def test_classify_cell_outside_tolerance_within_band_is_drift():
    # 5% apart: beyond the 2% tolerance, within the 10% band -> drift.
    assert v.classify_cell(
        1.00, 1.05, tolerance_pct=0.02, review_band_pct=0.10) == v.CELL_DRIFT


def test_classify_cell_beyond_review_band_is_mismatch():
    # 20% apart -> mismatch (a real conflict, the alarm state).
    assert v.classify_cell(
        1.00, 1.20, tolerance_pct=0.02, review_band_pct=0.10) == v.CELL_MISMATCH


def test_classify_cell_fields_classify_independently():
    # The input cell and output cell are scored separately: a clean input and a
    # conflicting output never contaminate each other (the per-field requirement).
    input_flag = v.classify_cell(
        1.00, 1.00, tolerance_pct=0.02, review_band_pct=0.10)
    output_flag = v.classify_cell(
        5.00, 6.50, tolerance_pct=0.02, review_band_pct=0.10)
    assert input_flag == v.CELL_MATCH
    assert output_flag == v.CELL_MISMATCH


def test_cell_states_are_five_distinct_constants():
    # match / drift / mismatch / unverified / no-key-mapping are all distinct; the last
    # two are deliberately NOT the same as mismatch.
    states = {v.CELL_MATCH, v.CELL_DRIFT, v.CELL_MISMATCH,
              v.CELL_UNVERIFIED, v.CELL_NO_KEY_MAPPING}
    assert len(states) == 5
