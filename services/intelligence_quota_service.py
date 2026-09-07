"""2026-07-30: usage counter for the new Intelligence API (api/intelligence.py).

Deliberately mirrors services/quota_service.py's sqlite counter pattern
(separate table, same INSERT ... ON CONFLICT DO UPDATE idiom) rather than
reusing it directly -- that service's FREE_LIMITS/plan strings are specific
to the logged-in-user AI-feature quotas (full_analysis/research/report) and
shouldn't be overloaded with an unrelated per-API-key concept.

TIER_LIMITS below reflect a RECOMMENDED pricing structure (2026-07-31),
reasoned from each endpoint's real relative cost (see ENDPOINT_WEIGHT
below -- events/sentiment are cheap RSS+one-model-call lookups, debate is
4 sequential LLM calls, intel is up to 2 LLM calls plus real OHLC/quant/
cross-asset lookups per cluster, the most expensive path in this router).
The corresponding $ prices are shown on intelligence-api.html (Free $0 /
Pro $49/mo / Enterprise custom) -- this is a RECOMMENDATION pending the
business owner's actual approval, not a unilaterally finalized decision;
adjust both this dict and intelligence-api.html's plan cards together if
the approved numbers differ.
"""
import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "xfinlab.db")

# RECOMMENDED (2026-07-31) -- maps to intelligence-api.html: Free $0,
# Pro $49/month, Enterprise custom. "unlimited" tiers are represented as
# -1 (checked explicitly below), matching services/quota_service.py's
# convention for pro/starter. This is a recommendation, not a unilateral
# final decision -- see this module's docstring.
TIER_LIMITS = {
    "free": 300,
    "pro": 5000,
    "enterprise": -1,
}

# 2026-08-25 (AJ growth batch: free-tier quota raised 100->300 to match/
# beat comparable free tiers -- FMP 250/day, Twelve Data 800/day, Alpha
# Vantage 25/day -- researched live via web search rather than guessed).
# The 6 non-LLM endpoints (events/sentiment/technical/stress_test/regime/
# forecast) cost ~$0 marginal (free market data + this server's own CPU),
# so a 3x pool raise is free there. debate/intel are the two that actually
# touch a paid/shared-limited LLM path (debate is forced onto DeepSeek V4
# Flash via DeepInfra -- real $ but ~$0.0005/call; intel defaults to Groq's
# FREE tier via ai/ai_router.py's AI_PROVIDER default, whose real risk
# isn't $ but its request-rate limit being SHARED across this whole site,
# including paying customers' chat/analysis features elsewhere). Rather
# than let a generous 300-call pool translate into unlimited debate/intel
# exposure, these two get their own separate, much lower daily ceiling
# regardless of how much of the 300 pool is left -- checked in api/
# intelligence.py's _check_and_spend_quota BEFORE the pool check. This is
# also the intended free->paid conversion lever AJ asked about ("FREEKEY
# 點樣延續人付費"): a developer hits this cap on the two most compelling
# features (AI debate, AI intelligence feed) long before they'd ever
# exhaust the 300-call pool on the cheap endpoints, and that's the exact
# moment _maybe_send_endpoint_upgrade_nudge() in api/intelligence.py fires
# an upgrade email -- same "highest-intent moment" reasoning as the
# existing pool-exhaustion nudge, just triggered by a more realistic,
# earlier event now that the pool itself is generous.
FREE_TIER_ENDPOINT_DAILY_CAP = {
    "debate": 15,
    "intel": 15,
}

# Debate is 4 sequential LLM calls per run (see services/agent_debate_service.py)
# -- far more expensive than a headline/sentiment lookup. Weight it heavier
# in the counter so a free-tier key can't cheaply exhaust the same "100
# calls" budget on the priciest endpoint. Recommended weighting, same
# pending-approval caveat as TIER_LIMITS above.
ENDPOINT_WEIGHT = {
    "events": 1,
    "sentiment": 1,
    "debate": 5,
    # 2026-07-31: AI Intelligence Engine feed (api/intelligence.py's
    # /v1/intel/latest + /v1/intel/{ticker}) -- each call can cluster
    # several headlines into up to `max_clusters` AI_NEWS_OBJECTs, and
    # each cluster may trigger up to 2 AI calls (Phase 1 summary + Phase 3
    # narrative) plus real OHLC/market-structure/historical-analog lookups
    # per affected ticker (Phase 2). Weighted heavier than `debate` (which
    # is a fixed 4-call cost) since this endpoint's cost scales with
    # max_clusters -- still a placeholder pending real cost-based pricing,
    # same caveat as every other number in this dict.
    "intel": 8,
    # 2026-07-31 (monetization batch, task #598): two more endpoints
    # exposing already-built engines as the "Decision/Market-Structure API"
    # direction from chat. No new AI calls -- `technical` is one yfinance/
    # Alpaca OHLC fetch + pure numpy/pandas computation (confluence, MACD,
    # market structure, chart patterns), `stress_test` is one OHLC fetch +
    # a vectorized numpy Monte Carlo resample (services/monte_carlo_service
    # .py already caps cost via MAX_HORIZON_DAYS/MAX_N_SIMULATIONS). Both
    # cheaper than `debate`/`intel` (no LLM call) but heavier than a plain
    # RSS lookup, since they do a real network fetch + nontrivial compute.
    "technical": 3,
    "stress_test": 3,
    # 2026-08-09 (World Engine Phase 0): /v1/world/market-map fans out
    # across up to 10 regions x (1 macro fetch + 1 headline fetch + 1
    # FinBERT batch call) + 1 global GDELT fetch -- no LLM calls (unlike
    # `intel`), but real multi-source network I/O that scales with region
    # count. Weighted between `technical` (single-asset, no fan-out) and
    # `intel` (LLM-backed) to reflect that shape. Same
    # pending-business-approval caveat as every other number here.
    "world_map": 6,
    # 2026-08-10 (P3 of the Quant Research Factory roadmap, "Regime-Aware
    # Signal" productization) -- wraps services/regime_router_service.py's
    # get_current_regime() (one OHLC fetch + causal indicator computation +
    # RegimeDetector.classify(), no LLM call) plus a read of the persisted
    # regime_router_candidates leaderboard (cheap sqlite SELECT). Same cost
    # shape as `technical` (single-asset fetch + compute, no fan-out, no AI
    # call) so weighted the same. Same pending-business-approval caveat as
    # every other number in this dict.
    "regime": 3,
    # 2026-08-24 (Capital Flow Engine roadmap, Layer 7 -- "Probabilistic
    # K-Line Path"): wraps services/probabilistic_forecast_service.py's
    # get_probabilistic_forecast() -- one OHLC fetch + a vectorized numpy
    # bootstrap resample (same cost shape as `stress_test`) PLUS an
    # optional direction_probability_service model load/predict and a
    # free capital_flow_engine cache read. Weighted one above stress_test
    # to reflect the extra (cheap but real) ML inference step. Same
    # pending-business-approval caveat as every other number in this dict.
    "forecast": 4,
    # 2026-08-27 (Data Factory -> Intelligence API monetization batch):
    # four endpoints wrapping the newest Data Factory collectors. All are
    # single real network fetch (each with its own on-server cache: 24h
    # for insider/short_interest, shorter for energy/exchange) + light
    # parsing -- no LLM call, no fan-out, so weighted with the other
    # single-fetch endpoints (`technical`/`stress_test`/`regime`) rather
    # than the fan-out (`world_map`) or LLM-backed (`intel`/`debate`)
    # tiers. `insider` costs slightly more than the others since a cache
    # miss can mean parsing several Form 4 XML filings, not just one file
    # read. Same pending-business-approval caveat as every other number
    # in this dict.
    "insider": 3,
    # 2026-08-29 (Company Network, Phase 1 of "Company Intelligence"):
    # fans out to FOUR underlying lookups (13F ownership+conviction, 13D/13G
    # search, Form 4 insider, COT) plus a local snapshot read/write -- no
    # LLM call, but the widest single-ticker fan-out of the non-AI
    # endpoints, so weighted above insider/technical/forecast and just
    # under world_map (which fans out across regions, not just sources).
    #
    # 2026-08-30 (Phase 2/3, "起 Phase 2 3 一次過"): raised 5->7. Phase 2
    # (sec_business_text_service) adds this codebase's first raw-HTML
    # 10-K document fetch + BeautifulSoup parse (a full filing document,
    # not a small JSON payload) and Phase 3 (event_impact_service) adds a
    # live 1-year OHLC fetch on top of the original 4-source fan-out --
    # both are 24h-cached like the rest of this endpoint, but a cache-miss
    # call is now meaningfully heavier than the Phase 1-only version was.
    # Kept below world_map (multi-region fan-out) since this is still a
    # single-ticker, single-document-per-call cost shape. Same
    # pending-business-approval caveat as every other number in this dict.
    #
    # 2026-08-30 (Phase 4): smart_money_crossholdings added but weight NOT
    # raised further -- it's a local SQL re-query against sec_13f_holdings
    # rows already fetched for the same response's institutional_ownership
    # section, no new network round trip, negligible marginal cost.
    "company_network": 7,
    "short_interest": 2,
    "energy": 2,
    "exchange": 2,
    # 2026-08-28 (AJ: "最高效賺錢點接" -- monetize the new Data Factory
    # batch as Intelligence API endpoints first). fundamentals priced
    # highest of this batch -- it's the first real financial-statement
    # data in the API and the clearest paid-tier differentiator vs. free
    # stock-data sites. vix_term_structure is cheap to compute (4 small
    # CSV fetches, market-wide not per-ticker) so priced low despite
    # being a distinct, sophisticated signal. bank_health/agriculture
    # are single lightweight lookups against a small explicit ticker
    # map, similar cost tier to energy/exchange.
    "fundamentals": 3,
    "vix_term_structure": 1,
    "bank_health": 2,
    "agriculture": 2,
    # 2026-08-30 (Real Estate Intelligence, cross-industry expansion #1):
    # same cost shape as agriculture/energy -- 4 small FRED series fetches
    # against an explicit ticker map, 6h server-side cached. Priced the same.
    "real_estate": 2,
    # same shape/cost as real_estate above -- 5 small FRED series fetches
    # against an explicit ticker map, 6h server-side cached.
    "supply_chain": 2,
    # same shape/cost as real_estate above -- 4 small FRED series fetches
    # against an explicit ticker map, 6h server-side cached.
    "consumer_demand": 2,
    # market-wide, not per-ticker, but re-fetches ~19 series across 6
    # modules (each with n_obs=6 instead of 1) every call -- 2026-08-31
    # expansion added energy (EIA) + agriculture (USDA) on top of the
    # original 3 FRED industries + macro backdrop, so bumped from 4 to
    # 6 to track the real added fetch cost. Still below company_network's
    # 7 since an individual industry here can be a fast no-op (empty
    # indicators dict) when that source's own key isn't configured.
    "opportunity_radar": 6,
    # 2026-08-31 (openFDA/CPSC consumer-safety expansion): unlike the
    # FRED-shape modules above (one fetch per series against a warm
    # cache), these run a live multi-keyword search per ticker across
    # multiple sub-datasets each call (4 for consumer_safety: food/drug/
    # device recalls + food adverse events; 1 for product_recalls) --
    # priced closer to short_interest/insider's per-ticker cost than the
    # cheap single-series FRED lookups, reflecting the real fan-out.
    "consumer_safety": 3,
    "product_recalls": 2,
}


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_table():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intelligence_api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT NOT NULL,
            date TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            UNIQUE(api_key, date)
        )
    """)
    conn.commit()
    conn.close()


_init_table()


# ---------------------------------------------------------------------------
# 2026-08-25 (AJ: "referral雙方加quota"): a per-API-key BONUS added on top
# of TIER_LIMITS, earned by successful referrals (see services/referral_
# service.py's use_code(), which calls add_quota_bonus() for both the
# referrer's and the new user's own key). Mirrors services/quota_service.py
#'s existing quota_bonus table/grant_bonus() pattern (a different, older
# per-user/per-feature system for logged-in AI features, not API keys) --
# same additive ON CONFLICT idiom, same "separate table, never touches
# TIER_LIMITS itself" posture, applied here to keys instead. Capped so
# repeated referrals can't inflate a key's quota without bound.
# ---------------------------------------------------------------------------
MAX_QUOTA_BONUS = 500  # 10 referrals' worth at the default 50/referral


def _init_bonus_table():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_key_quota_bonus (
            api_key TEXT PRIMARY KEY,
            bonus INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


_init_bonus_table()


def _is_hash_shaped(value: str) -> bool:
    """A SHA-256 hex digest is always exactly 64 lowercase hex chars. A
    raw XFINLAB API key ("xfl_" + secrets.token_urlsafe(32)) is a
    different length and uses base64url's mixed-case/-/_ alphabet, so
    this is a cheap, reliable discriminator between "already migrated"
    and "still the raw key" without a DB round-trip. Used by
    _migrate_bonus_table_to_hashed_keys() below."""
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _migrate_bonus_table_to_hashed_keys():
    """2026-09-07 (security review: "API keys stored in plaintext at
    rest"): api_key_quota_bonus used to be keyed by the raw API key
    string -- same plaintext-at-rest concern as api_keys/
    self_serve_api_keys (see services/api_key_service.py's verify_key()
    docstring for that migration's full story). This table needed its
    OWN one-time migration since, unlike the credential tables, nothing
    naturally re-touches a bonus row on a key's next use -- a referral
    bonus is written once (at referral time) and only ever read after
    that, never rewritten.

    Every caller of add_quota_bonus()/get_quota_bonus() was updated in
    this same change to always pass a key_hash from now on (either via
    api_key_service.get_active_key_for_user(), which now returns a
    hash, or by hashing a raw request-time key inline -- see check()
    below) -- so this migration only ever needs to run once per legacy
    row; nothing writes a raw key into this table again after today.
    One-time, idempotent (re-running is a safe no-op once every row is
    hash-shaped), and best-effort: any failure here just means that
    row's bonus is temporarily unreachable until it's investigated, not
    a crash of quota checking generally, since get_quota_bonus() simply
    returns 0 for an unmatched row either way."""
    try:
        conn = _get_db()
        rows = conn.execute("SELECT api_key, bonus FROM api_key_quota_bonus").fetchall()
        for row in rows:
            raw_or_hash = row["api_key"]
            if _is_hash_shaped(raw_or_hash):
                continue
            import hashlib

            hashed = hashlib.sha256(raw_or_hash.encode("utf-8")).hexdigest()
            conn.execute(
                """
                INSERT INTO api_key_quota_bonus (api_key, bonus) VALUES (?, ?)
                ON CONFLICT(api_key) DO UPDATE SET bonus = MIN(?, bonus + excluded.bonus)
                """,
                (hashed, row["bonus"], MAX_QUOTA_BONUS),
            )
            conn.execute("DELETE FROM api_key_quota_bonus WHERE api_key=?", (raw_or_hash,))
        conn.commit()
        conn.close()
    except Exception:
        pass


_migrate_bonus_table_to_hashed_keys()


def get_quota_bonus(api_key_hash: str) -> int:
    """2026-09-07: renamed param to make the post-migration contract
    explicit -- this must be called with a key_hash (see
    api_key_service.get_active_key_for_user()/_hash_key()), never a raw
    API key, or it will silently and always return 0 (a lookup miss,
    not an error) since api_key_quota_bonus is now hash-keyed."""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT bonus FROM api_key_quota_bonus WHERE api_key=?", (api_key_hash,)
        ).fetchone()
        return row["bonus"] if row else 0
    finally:
        conn.close()


def add_quota_bonus(api_key_hash: str, amount: int):
    """Best-effort additive grant, capped at MAX_QUOTA_BONUS regardless of
    how many times this is called for the same key. Never raises -- called
    from referral_service.py's use_code(), which must never fail a
    registration/referral over a quota-bonus hiccup.

    2026-09-07: renamed param -- same key_hash-only contract as
    get_quota_bonus() above."""
    if not api_key_hash or amount <= 0:
        return
    try:
        conn = _get_db()
        conn.execute(
            """
            INSERT INTO api_key_quota_bonus (api_key, bonus) VALUES (?, ?)
            ON CONFLICT(api_key) DO UPDATE SET bonus = MIN(?, bonus + excluded.bonus)
            """,
            (api_key_hash, min(amount, MAX_QUOTA_BONUS), MAX_QUOTA_BONUS),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def check(api_key: str, tier: str) -> dict:
    """Read-only check -- does NOT increment. Call increment() separately
    after the request actually succeeds, mirroring quota_service.py's
    check()-then-increment() split (so a failed upstream call doesn't burn
    the caller's quota).

    2026-08-25: `limit` now includes any referral-earned bonus (see
    add_quota_bonus() above) on top of the flat TIER_LIMITS number --
    unlimited tiers (limit==-1) are returned untouched, a bonus is
    meaningless there.

    2026-09-07 (security review: hash keys at rest): `api_key` here is
    still the RAW key straight off the incoming request's X-API-Key
    header (that's the one place in this whole flow it can only ever be
    raw -- it's literally what the caller presented), used as-is for
    the usage-counter lookup below (intelligence_api_usage is an
    ephemeral per-day counter table, not a persistent credential store,
    so it wasn't in scope for the hashing migration). The bonus lookup
    specifically now hashes it first, since api_key_quota_bonus WAS
    migrated to be hash-keyed (see _migrate_bonus_table_to_hashed_keys()
    above) -- passing the raw key to get_quota_bonus() here would
    silently and always return 0."""
    base_limit = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
    if base_limit == -1:
        return {"allowed": True, "used": 0, "limit": -1, "remaining": -1, "tier": tier, "bonus": 0}

    import hashlib

    bonus = get_quota_bonus(hashlib.sha256(api_key.encode("utf-8")).hexdigest())
    limit = base_limit + bonus

    today = datetime.now().strftime("%Y-%m-%d")
    conn = _get_db()
    row = conn.execute(
        "SELECT count FROM intelligence_api_usage WHERE api_key=? AND date=?",
        (api_key, today),
    ).fetchone()
    conn.close()

    used = row["count"] if row else 0
    return {
        "allowed": used < limit,
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "tier": tier,
        "bonus": bonus,
    }


def increment(api_key: str, weight: int = 1):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = _get_db()
    conn.execute(
        """
        INSERT INTO intelligence_api_usage (api_key, date, count)
        VALUES (?, ?, ?)
        ON CONFLICT(api_key, date)
        DO UPDATE SET count = count + excluded.count
        """,
        (api_key, today, weight),
    )
    conn.commit()
    conn.close()


def weight_for(endpoint: str) -> int:
    return ENDPOINT_WEIGHT.get(endpoint, 1)


# ---------------------------------------------------------------------------
# 2026-08-18: dedup tracking for the "you hit your free-tier limit" email
# nudge (api/intelligence.py's _check_and_spend_quota). Separate tiny table
# rather than a column bolted onto intelligence_api_usage -- this tracks
# "was an email sent today", a different concern from "how many calls were
# made today", and keeping them apart means neither table's meaning gets
# muddied. Same UNIQUE(api_key, date) + INSERT...ON CONFLICT idiom as the
# rest of this file.
# ---------------------------------------------------------------------------

def _init_nudge_table():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intelligence_upgrade_nudges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT NOT NULL,
            date TEXT NOT NULL,
            UNIQUE(api_key, date)
        )
    """)
    conn.commit()
    conn.close()


_init_nudge_table()


# ---------------------------------------------------------------------------
# 2026-08-25: per-endpoint daily sub-cap (free tier only -- see
# FREE_TIER_ENDPOINT_DAILY_CAP above). Separate table from
# intelligence_api_usage (which tracks the overall weighted pool) since
# this tracks a completely different thing: raw call COUNT on one specific
# endpoint, independent of weight/multiplier. Same UNIQUE(...)+INSERT...ON
# CONFLICT idiom as every other counter in this file.
# ---------------------------------------------------------------------------

def _init_endpoint_table():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intelligence_api_endpoint_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            date TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            UNIQUE(api_key, endpoint, date)
        )
    """)
    conn.commit()
    conn.close()


_init_endpoint_table()


def check_endpoint_cap(api_key: str, tier: str, endpoint: str) -> dict:
    """Read-only, does NOT increment (same check-then-increment split as
    check() above). {"capped": False} for any tier/endpoint combination
    that has no sub-cap (paid tiers, or a free-tier endpoint not in
    FREE_TIER_ENDPOINT_DAILY_CAP) -- callers should only enforce a 429 when
    both capped=True and allowed=False."""
    if tier != "free":
        return {"capped": False, "allowed": True}
    cap = FREE_TIER_ENDPOINT_DAILY_CAP.get(endpoint)
    if cap is None:
        return {"capped": False, "allowed": True}

    today = datetime.now().strftime("%Y-%m-%d")
    conn = _get_db()
    row = conn.execute(
        "SELECT count FROM intelligence_api_endpoint_usage WHERE api_key=? AND endpoint=? AND date=?",
        (api_key, endpoint, today),
    ).fetchone()
    conn.close()
    used = row["count"] if row else 0
    return {"capped": True, "allowed": used < cap, "used": used, "limit": cap}


def increment_endpoint(api_key: str, endpoint: str):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = _get_db()
    conn.execute(
        """
        INSERT INTO intelligence_api_endpoint_usage (api_key, endpoint, date, count)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(api_key, endpoint, date) DO UPDATE SET count = count + 1
        """,
        (api_key, endpoint, today),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 2026-08-25 (AJ: "最吸引嗰兩個功能要小心佢係咁開EMAIL攞KEY,跟IP定有其他
# 策略?"): the per-key sub-cap above is trivially defeated by re-signing-up
# with a fresh email -- every new free signup gets a brand new key with a
# fresh 15/day debate/intel allowance (see backend/auth/auth.py's
# _on_user_registered_issue_free_api_key and api/intelligence.py's
# /intelligence/v1/signup). A second, independent cap keyed by the
# CALLING IP (not the signup IP -- the IP that actually made the debate/
# intel request) closes that loop: even if someone mints 10 fresh keys,
# every call from those keys still shares one combined 15/day ceiling per
# IP, as long as they're calling from the same network. This is the
# standard layered defense every comparable API (Alpha Vantage, Polygon,
# etc.) uses -- per-key AND per-IP, whichever is hit first wins. It is NOT
# bulletproof against someone rotating IPs/VPNs on top of rotating emails,
# but it raises the real cost of abuse well past "just re-register" with
# essentially zero legitimate-user friction (one office/NAT sharing one IP
# would need >15 debate calls/day combined to ever notice this exists).
# Separate table from the per-key one since these are two independent
# dimensions being checked, not a shared counter.
# ---------------------------------------------------------------------------

def _init_endpoint_ip_table():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intelligence_api_endpoint_usage_by_ip (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            date TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            UNIQUE(ip, endpoint, date)
        )
    """)
    conn.commit()
    conn.close()


_init_endpoint_ip_table()


def check_endpoint_cap_by_ip(ip: "str | None", endpoint: str) -> dict:
    """Same shape/semantics as check_endpoint_cap() but keyed by calling IP
    instead of API key, and NOT tier-gated (an IP has no single tier if
    multiple keys share it) -- only fires for endpoints that appear in
    FREE_TIER_ENDPOINT_DAILY_CAP at all. {"capped": False} for a missing/
    unknown IP or a non-capped endpoint, same "only enforce when capped=True
    and allowed=False" contract as the per-key version."""
    if not ip or ip == "unknown":
        return {"capped": False, "allowed": True}
    cap = FREE_TIER_ENDPOINT_DAILY_CAP.get(endpoint)
    if cap is None:
        return {"capped": False, "allowed": True}

    today = datetime.now().strftime("%Y-%m-%d")
    conn = _get_db()
    row = conn.execute(
        "SELECT count FROM intelligence_api_endpoint_usage_by_ip WHERE ip=? AND endpoint=? AND date=?",
        (ip, endpoint, today),
    ).fetchone()
    conn.close()
    used = row["count"] if row else 0
    return {"capped": True, "allowed": used < cap, "used": used, "limit": cap}


def increment_endpoint_by_ip(ip: str, endpoint: str):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = _get_db()
    conn.execute(
        """
        INSERT INTO intelligence_api_endpoint_usage_by_ip (ip, endpoint, date, count)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(ip, endpoint, date) DO UPDATE SET count = count + 1
        """,
        (ip, endpoint, today),
    )
    conn.commit()
    conn.close()


def _init_endpoint_nudge_table():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intelligence_endpoint_nudges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            date TEXT NOT NULL,
            UNIQUE(api_key, endpoint, date)
        )
    """)
    conn.commit()
    conn.close()


_init_endpoint_nudge_table()


def should_send_endpoint_nudge(api_key: str, endpoint: str) -> bool:
    """Same one-per-key-per-day dedup as should_send_upgrade_nudge() below,
    but keyed by (api_key, endpoint) so hitting the debate cap and the
    intel cap on the same day can each still send their own nudge once."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM intelligence_endpoint_nudges WHERE api_key=? AND endpoint=? AND date=?",
            (api_key, endpoint, today),
        ).fetchone()
        return row is None
    finally:
        conn.close()


def record_endpoint_nudge_sent(api_key: str, endpoint: str):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = _get_db()
    conn.execute(
        "INSERT INTO intelligence_endpoint_nudges (api_key, endpoint, date) VALUES (?, ?, ?) "
        "ON CONFLICT(api_key, endpoint, date) DO NOTHING",
        (api_key, endpoint, today),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 2026-08-25 (AJ: "咁FREE KEY比人用，我接到數據訓ENGINE或儲存之類嗎" --
# does giving away free keys get me any data back?). Honest answer at the
# time was NO: intelligence_api_usage above only ever counted raw call
# volume, never WHAT was queried. This is a deliberately lightweight fix --
# logs endpoint + ticker + timestamp only, never the response body/payload
# (no engine training signal here, just product-usage signal: which
# tickers/endpoints developers actually care about). Disclosed in
# api-terms.html's Data section for transparency, matching this site's
# standing "developers trust transparency, not marketing" posture. Every
# call site funnels through api/intelligence.py's single
# _check_and_spend_quota() choke point, so this is one integration point,
# not nine.
# ---------------------------------------------------------------------------

def _init_query_log_table():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intelligence_api_query_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            ticker TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


_init_query_log_table()


def log_query(api_key: str, endpoint: str, ticker: "str | None" = None):
    """Best-effort, never raises -- a logging failure must never turn a
    real API call into a 500. Called unconditionally (every tier, not just
    free) so trending-ticker signal reflects real usage across the board."""
    try:
        conn = _get_db()
        conn.execute(
            "INSERT INTO intelligence_api_query_log (api_key, endpoint, ticker) VALUES (?, ?, ?)",
            (api_key, endpoint, (ticker or None)),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_trending_tickers(days: int = 7, limit: int = 20) -> list:
    """Aggregate-only product signal for the admin panel -- which tickers
    API consumers actually query most, across all keys/tiers. Never
    exposes which key queried what (no api_key in the returned rows)."""
    conn = _get_db()
    try:
        rows = conn.execute(
            """
            SELECT ticker, COUNT(*) AS n
            FROM intelligence_api_query_log
            WHERE ticker IS NOT NULL AND ticker != ''
              AND created_at >= datetime('now', ?)
            GROUP BY ticker
            ORDER BY n DESC
            LIMIT ?
            """,
            (f"-{max(1, days)} days", max(1, limit)),
        ).fetchall()
        return [{"ticker": r["ticker"], "count": r["n"]} for r in rows]
    finally:
        conn.close()


def should_send_upgrade_nudge(api_key: str) -> bool:
    """True if no nudge has been recorded for this key today -- call BEFORE
    sending, then record_upgrade_nudge_sent() only after the email actually
    goes out (same check-then-record split as check()/increment() above),
    so a send failure doesn't silently mark the day as "already nudged"."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM intelligence_upgrade_nudges WHERE api_key=? AND date=?",
            (api_key, today),
        ).fetchone()
        return row is None
    finally:
        conn.close()


def record_upgrade_nudge_sent(api_key: str):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = _get_db()
    conn.execute(
        "INSERT INTO intelligence_upgrade_nudges (api_key, date) VALUES (?, ?) "
        "ON CONFLICT(api_key, date) DO NOTHING",
        (api_key, today),
    )
    conn.commit()
    conn.close()
