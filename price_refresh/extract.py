"""LLM-driven price extraction: fetch a pricing page, prompt for strict cited JSON,
defensive-parse, and score against the golden fixtures.

Fetch-then-extract: this module fetches page text itself (httpx) and passes it to the
EXISTING llm.generate() seam — no tool-use. It is DB-free and llm-free at import:
fetch and generate_text are INJECTED callables, so the dev loop is deterministic and
the live path is wired in Phase 2 (which binds the real fetch + llm.generate and owns
persistence/logging). The structural contract is shared with validate.check_structure.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

import httpx

from price_refresh import validate

# Default per-fetch timeout (seconds). Pricing pages are small; a dead host fails clean.
DEFAULT_FETCH_TIMEOUT = 20.0

# Per-field score tolerance — absorbs trivial float-format jitter in extracted numbers.
# DISTINCT from PRICE_REFRESH cross_tol (cross-source agreement); a different concern,
# so it has its own name. Relative tolerance: a real price difference still fails.
EXTRACTION_SCORE_TOL = 1e-4

_FIELDS = ("input", "output")


class ExtractionError(Exception):
    """A parse/contract failure: unparseable JSON, a key-set mismatch, a non-positive
    rate, or a missing citation. Surfaced cleanly (never a raw JSONDecodeError/KeyError)
    so upstream (Phase 2) treats it as a REJECT, never a guess."""


@dataclass
class ExtractionResult:
    """One source's extraction outcome. `prices` is the cited payload on success."""

    provider: str
    source_url: str
    prices: dict | None
    ok: bool
    error: str | None = None
    raw: str = ""


@dataclass
class ScoreResult:
    """Extraction accuracy vs the hand-verified expected payload."""

    n_correct: int
    n_total: int
    mismatches: list = field(default_factory=list)   # (model, field, got, want)


class _TextExtractor(HTMLParser):
    """Collapse HTML to readable text: drop script/style bodies, keep visible text.
    Keeps the prompt from being raw-HTML token bloat without a bs4 dependency."""

    _DROP = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._DROP:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._DROP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        return "\n".join(self._parts)


def _html_to_text(html: str) -> str:
    """Stdlib HTML -> visible text (script/style stripped)."""
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


def fetch_page(url: str, *, timeout: float = DEFAULT_FETCH_TIMEOUT,
               user_agent: str | None = None, http_get=None) -> str:
    """GET `url` and return its visible text. `user_agent`, when set, is sent as the
    User-Agent header (several provider pricing pages 403 a default httpx client); it has
    NO literal default here, the value flows in from config. `http_get` is injectable (a
    `(url) -> response` with `.raise_for_status()` and `.text`); defaults to httpx with
    redirects. Any fetch/HTTP failure is wrapped as ExtractionError (never a raw httpx
    error). Note: no JS is executed, so sources must be server-rendered."""
    headers = {"User-Agent": user_agent} if user_agent else None

    def _default_get(target: str):
        return httpx.get(target, timeout=timeout, follow_redirects=True,
                         headers=headers)

    getter = http_get if http_get is not None else _default_get
    try:
        resp = getter(url)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:   # noqa: BLE001 — any fetch failure becomes a clean reject
        raise ExtractionError(f"fetch failed for {url}: {e}") from e
    return _html_to_text(html)


def extract_source(*, provider: str, expected_models: set[str], source_url: str,
                   fetch, generate_text) -> ExtractionResult:
    """Extract one source: fetch -> prompt -> generate_text -> parse. `fetch(url)->str`
    and `generate_text(prompt)->str` are injected; both are contracted to raise
    ExtractionError on failure (Phase 2's generate_text closure converts LLMError). A
    failure yields ok=False with the error captured — one bad source never aborts the
    others."""
    try:
        page = fetch(source_url)
        prompt = build_extraction_prompt(provider, set(expected_models), page,
                                         source_url)
        raw = generate_text(prompt)
        prices = parse_extraction(raw, set(expected_models), source_url)
    except ExtractionError as e:
        return ExtractionResult(provider, source_url, None, ok=False, error=str(e))
    return ExtractionResult(provider, source_url, prices, ok=True, raw=raw)


def extract_prices(source_map: dict, expected_by_provider: dict, *,
                   fetch, generate_text) -> list[ExtractionResult]:
    """Extract the ONE official source per provider -> one ExtractionResult per provider.
    The scrape is the value-of-record, cross-checked against a third-party validator feed
    (not a second scrape), so `source_map` is provider -> a single URL string.
    `expected_by_provider` maps provider -> its canonical model set. One failed provider
    never aborts the others (its result is ok=False with the error captured)."""
    return [
        extract_source(
            provider=provider,
            expected_models=expected_by_provider.get(provider, set()),
            source_url=url, fetch=fetch, generate_text=generate_text)
        for provider, url in source_map.items()
    ]


def _strip_fences(text: str) -> str:
    """Return the JSON body, unwrapping a ```json ... ``` (or bare ```) fence if the
    model added one despite the JSON-only instruction. Falls back to the stripped text.
    """
    candidate = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    return match.group(1).strip() if match else candidate


def build_extraction_prompt(provider: str, expected_models: set[str],
                            page_text: str, source_url: str) -> str:
    """The strict CONTRACT prompt: JSON only, keyed by exactly these canonical models,
    STANDARD per-MTok rates (batch/cached/image/fine-tuning explicitly forbidden), with
    a verbatim quote per number. The model reads MEANING; the quote keeps it checkable.
    """
    keys = "\n".join(f"  - {m}" for m in sorted(expected_models))
    return (
        "You are a precise pricing-data extractor. Read the pricing page text below "
        "and report the STANDARD list price for each requested model.\n\n"
        "Return JSON ONLY — no prose, no markdown, no code fences. The JSON is an "
        "object keyed by EXACTLY these canonical model identifiers (no extras, none "
        f"missing):\n{keys}\n\n"
        'Each value is an object {"input": <number>, "output": <number>, '
        '"quote": <string>}. `input` and `output` are the STANDARD price in US dollars '
        "per 1,000,000 tokens (per-MTok). `quote` is the exact snippet of page text the "
        "numbers came from, so each number is checkable.\n\n"
        "Use ONLY the standard/list per-token rates. Do NOT use batch, cached / "
        "prompt-caching, image, audio, or fine-tuning prices — those are named failure "
        "modes. If several tiers are shown, pick the standard one.\n\n"
        f"Provider: {provider}\nSource URL: {source_url}\n\n"
        f"PAGE TEXT:\n{page_text}\n"
    )


def parse_extraction(text: str, expected_models: set[str],
                     source_url: str) -> dict:
    """Defensively parse the model's response into a cited payload keyed by
    canonical provider:model -> {input, output, quote, source_url}. Strips a stray
    fence, json.loads (-> ExtractionError on failure), reuses validate.check_structure
    for the exact-key-set + positive-number contract, then requires a non-empty `quote`
    per entry and STAMPS the known source_url. Any failure -> ExtractionError.
    """
    try:
        payload = json.loads(_strip_fences(text))
    except (json.JSONDecodeError, ValueError) as e:
        raise ExtractionError(f"unparseable JSON from model: {e}") from e

    structure = validate.check_structure(payload, set(expected_models))
    if not structure.ok:
        raise ExtractionError(structure.reason)

    out: dict = {}
    for model, entry in payload.items():
        quote = entry.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            raise ExtractionError(f"{model}: missing or empty 'quote' citation")
        out[model] = {
            "input": float(entry["input"]),
            "output": float(entry["output"]),
            "quote": quote.strip(),
            "source_url": source_url,   # WE stamp provenance; the model supplies quote
        }
    return out


def score(extracted: dict, expected: dict, *, tol: float) -> ScoreResult:
    """Compare an extracted payload to the hand-verified expected one, per model per
    field (input/output): correct when within `tol` (math.isclose). A missing model or
    a non-numeric value counts as incorrect. n_correct == n_total is the green bar."""
    n_correct = 0
    n_total = 0
    mismatches: list = []
    for model, want in expected.items():
        for fld in _FIELDS:
            n_total += 1
            got = extracted.get(model, {}).get(fld)
            if (isinstance(got, (int, float)) and not isinstance(got, bool)
                    and math.isclose(got, want[fld], rel_tol=tol)):
                n_correct += 1
            else:
                mismatches.append((model, fld, got, want[fld]))
    return ScoreResult(n_correct, n_total, mismatches)
