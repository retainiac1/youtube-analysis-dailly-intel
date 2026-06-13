# Price-refresh golden fixtures

Each pricing source has two committed files — the **oracle** for extraction:

- `<provider>_<label>.txt` — a saved snapshot of the source page's **stripped text**
  (what `extract.fetch_page` produces; no JS, no HTML tags). The deterministic input.
- `<provider>_<label>.expected.json` — the hand-verified oracle:

  ```json
  {
    "prices": { "<provider:model>": {"input": N, "output": N, "quote": "...", "source_url": "..."} },
    "baselines": { "<provider:model>": { "min_correct": N } }
  }
  ```

  - `prices` — the correct STANDARD per-MTok values (not batch/cached/image), with a
    verbatim `quote` and the page `source_url`.
  - `baselines` — the live-accuracy pass threshold, **keyed by extraction model**. The
    live gate reads `baselines[config.EXTRACTION_MODEL].min_correct` and asserts
    `n_correct >= min_correct`. A model with no entry **fails loud** — record one.

## Capture ritual (build-time, periodic)

1. `extract.fetch_page(url)` against the live source; save its return to
   `<provider>_<label>.txt`.
2. Hand-verify the prices into `<provider>_<label>.expected.json` (`prices`).
3. Run the live scorer for the **active `EXTRACTION_MODEL`** and record/update its
   `baselines[model].min_correct`. Swapping the model requires a fresh per-model
   baseline — a model never runs against another model's threshold.

`anthropic_sample.*` is a synthetic starter fixture (clear standard-vs-batch text) that
proves the loop offline; replace/augment it with real captures for all four providers.
