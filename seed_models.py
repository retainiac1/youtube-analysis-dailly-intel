"""seed_models.py — live-VERIFY the configured xAI model string (operator-run).

db.init_db already OFFLINE-seeds the baseline models, including the canonical
`xai:grok-4.3`, so the app is never broken on a fresh DB. This one-time script does
the LIVE check the offline seed cannot: it confirms the CANONICAL configured grok
(`config.SEED_MODELS`) is offered by xAI's /v1/models and runs ONE real generation to
prove the string works end-to-end, then reconciles the registry to it (a no-op when
the configured string is already seeded). It also read-only cross-checks the Anthropic
string against the live invocation log (the Phase-0 cross-check, expected to confirm
no drift).

This VERIFIES the configured model; it never auto-picks a different grok. Registry
models are user-managed on the /models page (the add-model flow), so this tool must
never silently switch the registry to another string.

Operator run-order (see docs/model-price-plan.MD):
  1. VACUUM INTO backup of the live swipefile.db; verify it.
  2. db.init_db(DB_PATH)  (migrate to v7 + offline-seed; the app already works).
  3. python seed_models.py   (this script) — then STOP and review.

Network + a tiny PAID xAI call happen here, which is exactly why this is NOT in
init_db. All writes go through run_with_db_retry + transaction.
"""
import sys

import config
import db
import llm

# Fallback price for a newly-confirmed grok string: the xAI API does not return
# prices, so a corrected model is seeded at the canonical grok rate and the operator
# adjusts it on the /models page (Phase 2). A no-op correction (string unchanged)
# keeps the already-seeded price untouched.
XAI_PRICE_FALLBACK = config.DEFAULT_PRICES["xai:grok-4.3"]


def canonical_xai_model() -> str:
    """The single canonical xai model string from config.SEED_MODELS (the registry
    bootstrap). The live gate CONFIRMS this string is offered; it never auto-picks a
    different grok. Registry models are user-managed on the /models page, so the
    canonical string is whatever the bootstrap declares. Raises ValueError if the
    bootstrap does not carry exactly one xai model (a malformed config, surfaced
    loudly rather than silently resolved)."""
    xai = [m["model"] for m in config.SEED_MODELS if m["provider"] == "xai"]
    if len(xai) != 1:
        raise ValueError(
            f"expected exactly one xai model in config.SEED_MODELS, got {xai}"
        )
    return xai[0]


def correct_xai(conn, confirmed_model: str, *, input_per_1m: float,
                output_per_1m: float, now: str) -> dict:
    """Reconcile the registry to the live-confirmed xai model string.

    No-op when the confirmed string already exists (re-enabled if it had been soft-
    deleted, never duplicated). Otherwise every OTHER xai:* model is soft-deleted
    (enabled=0, deleted=1) so the stale string leaves the dropdown while its price
    window stays for backward pricing, and the confirmed model is inserted (grok
    honors both temperature and seed) with one open price window. Does not commit —
    the caller wraps it in transaction(). Returns {"confirmed", "retired"}."""
    if not confirmed_model.startswith("xai:"):
        raise ValueError(f"expected an xai: model, got {confirmed_model!r}")

    retired = [
        r["model"] for r in conn.execute(
            "SELECT model FROM models WHERE model LIKE 'xai:%' AND deleted = 0 "
            "AND model <> ?", (confirmed_model,)
        ).fetchall()
    ]
    for m in retired:
        conn.execute(
            "UPDATE models SET enabled = 0, deleted = 1, "
            "notes = 'retired: replaced by ' || ? WHERE model = ?",
            (confirmed_model, m),
        )

    existing = conn.execute(
        "SELECT 1 FROM models WHERE model = ?", (confirmed_model,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO models (model, provider, enabled, supports_temperature, "
            "supports_seed, is_reasoning, deleted, added_at, notes) "
            "VALUES (?, 'xai', 1, 1, 1, 0, 0, ?, 'live-verified')",
            (confirmed_model, now),
        )
    else:
        # An existing row (possibly previously soft-deleted) is re-enabled, never
        # duplicated.
        conn.execute(
            "UPDATE models SET enabled = 1, deleted = 0 WHERE model = ?",
            (confirmed_model,),
        )

    has_window = conn.execute(
        "SELECT 1 FROM model_prices WHERE model = ? AND valid_to IS NULL "
        "AND deleted = 0 LIMIT 1", (confirmed_model,)
    ).fetchone()
    if has_window is None:
        conn.execute(
            "INSERT INTO model_prices (model, input_per_1m, output_per_1m, "
            "valid_from, valid_to, deleted, recorded_at) "
            "VALUES (?, ?, ?, ?, NULL, 0, ?)",
            (confirmed_model, input_per_1m, output_per_1m, now[:10], now),
        )
    return {"confirmed": confirmed_model, "retired": retired}


def verify_anthropic(conn) -> tuple[str | None, str | None]:
    """Read-only cross-check: (registry anthropic string, most-recently-logged
    anthropic string). Equal means no drift — the Phase-0 cross-check, expected to
    confirm `anthropic:claude-haiku-4-5` is current. Either may be None (no row)."""
    reg = conn.execute(
        "SELECT model FROM models WHERE model LIKE 'anthropic:%' AND deleted = 0 "
        "ORDER BY model LIMIT 1"
    ).fetchone()
    logged = conn.execute(
        "SELECT model FROM llm_invocations WHERE model LIKE 'anthropic:%' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return (reg["model"] if reg else None, logged["model"] if logged else None)


def verify_xai_live() -> str:
    """LIVE: confirm the CANONICAL config xai model is offered by xAI's /v1/models
    and runs ONE real generation end-to-end. Returns that canonical 'xai:<id>'
    string. Does NOT auto-pick: the registry model is user-managed (the /models
    page), so this only VERIFIES the configured string and never switches to a
    different grok. Raises llm.LLMError on any provider failure or if the canonical
    model is not in the live list (a clean, provider-named message)."""
    import openai

    model = canonical_xai_model()
    bare = model.split(":", 1)[1]
    key = llm._require_key("XAI_API_KEY", "xai")
    client = openai.OpenAI(api_key=key, base_url="https://api.x.ai/v1")
    try:
        ids = [m.id for m in client.models.list().data]
    except openai.OpenAIError as e:
        raise llm.LLMError(f"xai: {e}") from e
    if bare not in ids:
        raise llm.LLMError(
            f"xai: canonical model {bare!r} not offered by /v1/models"
        )
    # One real generation confirms the string actually works (the whole point of the
    # live gate); grok honors both temperature and seed.
    llm.generate(model, "Reply with the single word OK", temperature=0.5, seed=7,
                 supports_temperature=True, supports_seed=True)
    return model


def main() -> None:
    db_path = config.DB_PATH
    print(f"seed_models: live-verifying the xAI string against {db_path} ...")
    confirmed = verify_xai_live()
    now = config.now_local_iso()

    conn = db.get_connection(db_path)
    try:
        def _apply():
            with db.transaction(conn):
                return correct_xai(
                    conn, confirmed,
                    input_per_1m=XAI_PRICE_FALLBACK["input"],
                    output_per_1m=XAI_PRICE_FALLBACK["output"], now=now,
                )
        result = db.run_with_db_retry(_apply)
        reg_anth, logged_anth = verify_anthropic(conn)
    finally:
        conn.close()

    print(f"  xAI confirmed live: {confirmed}")
    if result["retired"]:
        print(f"  retired (soft-deleted) stale xAI rows: {result['retired']}")
        print(f"  NOTE: priced {confirmed} at the canonical fallback "
              f"{XAI_PRICE_FALLBACK}; verify/adjust on the /models page.")
    else:
        print("  no change: the offline-seeded xAI string was already current.")
    drift = "OK, no drift" if reg_anth == logged_anth else "DRIFT — review"
    print(f"  Anthropic registry={reg_anth} logged={logged_anth} ({drift})")
    print("STOP: review the changes before trusting the DB.")


if __name__ == "__main__":
    sys.exit(main())
