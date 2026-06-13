"""Extraction agent: fetch -> strict-JSON prompt -> defensive parse -> scorer."""
import json
from pathlib import Path

import pytest

from price_refresh import extract, validate

FIXTURES = Path(__file__).resolve().parent.parent / "price_refresh" / "fixtures"


def _load_oracle(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.expected.json").read_text())


def _load_snapshot(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text()

CANON = {"anthropic:claude-haiku-4-5"}
URL = "https://example/official"
GOOD = {
    "anthropic:claude-haiku-4-5": {
        "input": 1.0, "output": 5.0,
        "quote": "Input $1.00 / MTok, Output $5.00 / MTok",
        "source_url": URL,
    }
}


def _json(payload) -> str:
    return json.dumps(payload)


# --- parse_extraction -------------------------------------------------------

def test_parse_strips_fences_and_parses():
    text = "```json\n" + _json(GOOD) + "\n```"
    out = extract.parse_extraction(text, CANON, URL)
    assert out["anthropic:claude-haiku-4-5"]["input"] == 1.0
    assert out["anthropic:claude-haiku-4-5"]["output"] == 5.0


def test_parse_stamps_source_url():
    # The model supplies the quote (its evidence); WE stamp the known source_url.
    text = _json({"anthropic:claude-haiku-4-5":
                  {"input": 1.0, "output": 5.0, "quote": "q"}})
    out = extract.parse_extraction(text, CANON, URL)
    assert out["anthropic:claude-haiku-4-5"]["source_url"] == URL


def test_parse_prose_raises():
    with pytest.raises(extract.ExtractionError):
        extract.parse_extraction("Sure! Here are the prices you asked for.", CANON, URL)


def test_parse_broken_json_raises():
    with pytest.raises(extract.ExtractionError):
        extract.parse_extraction('{"anthropic:claude-haiku-4-5": {"input": 1.0,',
                                  CANON, URL)


def test_parse_missing_model_key_raises():
    with pytest.raises(extract.ExtractionError, match="claude-haiku"):
        extract.parse_extraction(_json({}), CANON, URL)


def test_parse_extra_model_key_raises():
    text = _json({**GOOD,
                  "openai:gpt-5.4-nano": {"input": 0.2, "output": 1.25, "quote": "q"}})
    with pytest.raises(extract.ExtractionError):
        extract.parse_extraction(text, CANON, URL)


def test_parse_non_positive_number_raises():
    text = _json({"anthropic:claude-haiku-4-5":
                  {"input": 0.0, "output": 5.0, "quote": "q"}})
    with pytest.raises(extract.ExtractionError):
        extract.parse_extraction(text, CANON, URL)


def test_parse_missing_quote_raises():
    text = _json({"anthropic:claude-haiku-4-5": {"input": 1.0, "output": 5.0}})
    with pytest.raises(extract.ExtractionError, match="quote"):
        extract.parse_extraction(text, CANON, URL)


def test_parse_empty_quote_raises():
    text = _json({"anthropic:claude-haiku-4-5":
                  {"input": 1.0, "output": 5.0, "quote": "  "}})
    with pytest.raises(extract.ExtractionError, match="quote"):
        extract.parse_extraction(text, CANON, URL)


# --- build_extraction_prompt (contract) -------------------------------------

def test_prompt_lists_keys_and_forbids_discount_rates():
    prompt = extract.build_extraction_prompt(
        "anthropic", CANON, "THE PAGE TEXT", URL)
    assert "anthropic:claude-haiku-4-5" in prompt
    assert "THE PAGE TEXT" in prompt
    assert "json" in prompt.lower()
    low = prompt.lower()
    for forbidden in ("batch", "cached", "image", "fine-tun"):
        assert forbidden in low, forbidden


# --- score ------------------------------------------------------------------

def test_score_all_correct():
    res = extract.score(GOOD, GOOD, tol=extract.EXTRACTION_SCORE_TOL)
    assert res.n_correct == res.n_total == 2   # input + output
    assert res.mismatches == []


def test_score_one_wrong_names_mismatch():
    extracted = {"anthropic:claude-haiku-4-5":
                 {"input": 1.0, "output": 9.0, "quote": "q", "source_url": URL}}
    res = extract.score(extracted, GOOD, tol=extract.EXTRACTION_SCORE_TOL)
    assert res.n_correct == 1 and res.n_total == 2
    assert any(m[0] == "anthropic:claude-haiku-4-5" and m[1] == "output"
               for m in res.mismatches)


def test_score_uses_tolerance_not_exact_equality():
    extracted = {"anthropic:claude-haiku-4-5":
                 {"input": 1.0000001, "output": 5.0, "quote": "q", "source_url": URL}}
    res = extract.score(extracted, GOOD, tol=extract.EXTRACTION_SCORE_TOL)
    assert res.n_correct == res.n_total == 2


def test_score_missing_model_counts_as_incorrect():
    res = extract.score({}, GOOD, tol=extract.EXTRACTION_SCORE_TOL)
    assert res.n_correct == 0 and res.n_total == 2


# --- fetch_page (httpx + stdlib HTML strip) ---------------------------------

class _FakeResp:
    def __init__(self, text, *, raise_exc=None):
        self.text = text
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc


def test_html_to_text_strips_tags_and_scripts():
    html = ("<html><head><style>.x{color:red}</style></head><body>"
            "<script>var secret = 1</script>"
            "<h1>Pricing</h1><p>Input $1.00 / MTok</p></body></html>")
    text = extract._html_to_text(html)
    assert "Pricing" in text and "$1.00 / MTok" in text
    assert "secret" not in text and "color:red" not in text
    assert "<h1>" not in text


def test_fetch_page_returns_stripped_text():
    html = "<body><h1>Prices</h1></body>"
    out = extract.fetch_page("https://x", http_get=lambda u: _FakeResp(html))
    assert "Prices" in out and "<h1>" not in out


def test_fetch_page_wraps_network_error():
    def boom(_url):
        raise RuntimeError("connection reset")
    with pytest.raises(extract.ExtractionError, match="fetch failed"):
        extract.fetch_page("https://x", http_get=boom)


def test_fetch_page_wraps_http_status_error():
    resp = _FakeResp("", raise_exc=RuntimeError("404"))
    with pytest.raises(extract.ExtractionError, match="fetch failed"):
        extract.fetch_page("https://x", http_get=lambda u: resp)


# --- extract_source (fetch -> prompt -> generate -> parse) ------------------

def _good_generate(_prompt):
    return _json(GOOD)


def test_extract_source_happy_path():
    res = extract.extract_source(
        provider="anthropic", expected_models=CANON, source_url=URL,
        fetch=lambda u: "PAGE TEXT", generate_text=_good_generate)
    assert res.ok and res.prices["anthropic:claude-haiku-4-5"]["input"] == 1.0
    assert res.provider == "anthropic" and res.source_url == URL


def test_extract_source_fetch_failure_isolated():
    def bad_fetch(_u):
        raise extract.ExtractionError("fetch failed for X")
    res = extract.extract_source(
        provider="anthropic", expected_models=CANON, source_url=URL,
        fetch=bad_fetch, generate_text=_good_generate)
    assert res.ok is False and res.prices is None and "fetch failed" in res.error


def test_extract_source_bad_model_output_is_clean_failure():
    res = extract.extract_source(
        provider="anthropic", expected_models=CANON, source_url=URL,
        fetch=lambda u: "PAGE", generate_text=lambda p: "sorry, here's prose")
    assert res.ok is False and res.prices is None and res.error


def test_extract_source_generate_failure_isolated():
    # The injected generate_text is contracted to raise ExtractionError on LLM failure
    # (Phase 2 converts LLMError); extract_source must catch it, not propagate.
    def bad_generate(_p):
        raise extract.ExtractionError("anthropic: rate limit")
    res = extract.extract_source(
        provider="anthropic", expected_models=CANON, source_url=URL,
        fetch=lambda u: "PAGE", generate_text=bad_generate)
    assert res.ok is False and "rate limit" in res.error


# --- extract_prices (iterate providers x sources) ---------------------------

def test_extract_prices_one_result_per_source_and_isolates_failures():
    source_map = {"anthropic": ["https://a1", "https://a2"]}
    expected_by_provider = {"anthropic": CANON}

    def fetch(url):
        if url == "https://a2":
            raise extract.ExtractionError("fetch failed for a2")
        return "PAGE"

    results = extract.extract_prices(
        source_map, expected_by_provider, fetch=fetch,
        generate_text=_good_generate)
    assert len(results) == 2                       # one per source URL
    by_url = {r.source_url: r for r in results}
    assert by_url["https://a1"].ok is True
    assert by_url["https://a2"].ok is False        # failure did not abort the other


# --- golden fixtures: wellformed + deterministic dev loop -------------------

FIXTURE_NAME = "anthropic_sample"


def test_fixture_files_are_wellformed():
    oracle = _load_oracle(FIXTURE_NAME)
    assert "prices" in oracle and "baselines" in oracle
    res = validate.check_structure(oracle["prices"], set(oracle["prices"]))
    assert res.ok, res.reason
    for entry in oracle["prices"].values():
        assert entry["quote"].strip() and entry["source_url"].strip()
    assert _load_snapshot(FIXTURE_NAME).strip()    # snapshot is non-empty


def test_dev_loop_scores_perfect_on_fixture():
    # Deterministic plumbing gate: a "perfect" model (mocked) returns the oracle's
    # numbers + quotes; the full fetch->prompt->parse->score pipeline must score 100%.
    # No real model — the live accuracy gate (below) measures the real model.
    oracle = _load_oracle(FIXTURE_NAME)
    expected = oracle["prices"]
    snapshot = _load_snapshot(FIXTURE_NAME)
    source_url = next(iter(expected.values()))["source_url"]
    model_output = json.dumps({
        m: {"input": v["input"], "output": v["output"], "quote": v["quote"]}
        for m, v in expected.items()
    })

    res = extract.extract_source(
        provider="anthropic", expected_models=set(expected), source_url=source_url,
        fetch=lambda _u: snapshot, generate_text=lambda _p: model_output)

    assert res.ok, res.error
    s = extract.score(res.prices, expected, tol=extract.EXTRACTION_SCORE_TOL)
    assert s.n_correct == s.n_total, s.mismatches
    assert all(e["source_url"] == source_url for e in res.prices.values())


# --- live (excluded by default: pytest -m live) -----------------------------

def _live_generate_text(model: str):
    """Closure over llm.generate for the active model (flags from SEED_MODELS),
    converting LLMError -> ExtractionError per the extract_source contract. Imported
    lazily so the default suite never pulls llm."""
    import config
    import llm

    flags = next(m for m in config.SEED_MODELS if m["model"] == model)

    def generate_text(prompt: str) -> str:
        try:
            return llm.generate(
                model, prompt, temperature=0.0, seed=7,
                supports_temperature=bool(flags["supports_temperature"]),
                supports_seed=bool(flags["supports_seed"]),
                is_reasoning=bool(flags["is_reasoning"]),
                max_tokens=2000).text
        except llm.LLMError as e:
            raise extract.ExtractionError(str(e)) from e

    return generate_text


@pytest.mark.live
def test_live_accuracy_meets_recorded_baseline():
    import config
    oracle = _load_oracle(FIXTURE_NAME)
    expected = oracle["prices"]
    model = config.EXTRACTION_MODEL
    baselines = oracle.get("baselines", {})
    if model not in baselines:
        pytest.fail(
            f"no recorded baseline for model {model} on fixture {FIXTURE_NAME}; "
            "record one (run the scorer and write baselines[model].min_correct)")
    min_correct = baselines[model]["min_correct"]
    source_url = next(iter(expected.values()))["source_url"]

    res = extract.extract_source(
        provider="anthropic", expected_models=set(expected), source_url=source_url,
        fetch=lambda _u: _load_snapshot(FIXTURE_NAME),
        generate_text=_live_generate_text(model))

    assert res.ok, res.error
    s = extract.score(res.prices, expected, tol=extract.EXTRACTION_SCORE_TOL)
    assert s.n_correct >= min_correct, s.mismatches


@pytest.mark.live
def test_live_smoke_real_fetch_and_extract():
    import config
    model = config.EXTRACTION_MODEL
    provider = "anthropic"
    url = config.PRICE_SOURCES[provider][0]
    res = extract.extract_source(
        provider=provider, expected_models={"anthropic:claude-haiku-4-5"},
        source_url=url, fetch=extract.fetch_page,
        generate_text=_live_generate_text(model))
    assert res.ok, res.error
    entry = res.prices["anthropic:claude-haiku-4-5"]
    assert entry["input"] > 0 and entry["output"] > 0 and entry["quote"].strip()
