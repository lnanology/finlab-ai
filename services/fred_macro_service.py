"""
FRED (Federal Reserve Economic Data) Macro Service -- 2026-08-09,
World Engine Phase 0 (XFINLAB_Final_Strategy.md section 5/7).

Closes a gap flagged (not filled) back in task #555/#558: FRED's Terms of
Use were verified commercial-use-clean and an entry was added to
services/license_registry.py on 2026-07-31, but the entry explicitly said
"NOT YET integrated into any service" pending this file. This is that
file.

Why FRED on top of the existing services/macro_data_service.py (World
Bank): World Bank's GDP/inflation/unemployment figures are ANNUAL and lag
6-18 months -- fine as a baseline for the ~190 countries World Bank
covers, but genuinely stale for the US specifically, where FRED (the St.
Louis Fed's own data warehouse) publishes several of the same concepts
at monthly/weekly frequency, plus market-relevant series World Bank
doesn't carry at all (yield curve spread, Fed funds rate, initial jobless
claims). This service is US-only and is layered ON TOP of
macro_data_service.py's get_macro_snapshot("us") as an optional richer
replacement, never a replacement for the other 9 regions World Bank
still uniquely covers.

Attribution requirement (from license_registry.py's "fred" entry, terms
verified 2026-07-31 at fred.stlouisfed.org/docs/api/terms_of_use.html):
"This product uses the FRED (R) API but is not endorsed or certified by
the Federal Reserve Bank of St. Louis." -- surfaced via the `attribution`
field on every successful response so any caller (site, API docs page,
MCP tool description) can render it without hunting for this docstring.

Same dormant-until-configured convention as services/youtube_upload_
service.py and js/support-widget.js: FRED requires a free API key (one
signup at fred.stlouisfed.org/docs/api/api_key.html, no cost, no review
wait -- unlike the YouTube OAuth flow). Until FRED_API_KEY is set,
is_available() is False and every function returns an honest
{"available": False} rather than silently falling back to fabricated
numbers.

Honesty contract (same standard as every other data service in this
codebase): FRED represents a missing observation as the literal string
"." rather than null/None. This module treats "." as a genuine missing
value (excluded from `indicators`, never coerced to 0 or interpolated).

2026-08-26 (AJ's "Data Factory" batch, Step 2 -- first real migration
onto services/data_source_registry.py): before this change, every
observation this module ever fetched lived only in the in-memory
`_cache` dict with a 6h TTL -- a Railway restart (deploy, crash, dyno
recycle) meant every series went back to "no data yet" until the next
live fetch succeeded. That's fine for a display widget, not fine as a
"Data Factory" source other engines are meant to build on.

Fix: every successful fetch now also upserts into a small local
`fred_macro_observations` table (latest known value per series_id+date,
same xfinlab.db as everything else) so a restart falls back to
last-known-good data instead of nothing. This is intentionally NOT a
full point-in-time/vintage store (it does not keep every historical
revision FRED has ever published for a given date -- CPI/GDP get
revised after the fact and this table just keeps whatever value was
most recently seen for that date). A true vintage store is a
Step 3+ upgrade if/when something actually needs "what did we believe
on date X, as of date X" rather than "best known value for date X".

Also now self-registers with services.data_source_registry as source_key
"fred_macro" -- admin can see run/error counts and disable it from the
Data Factory panel. When disabled, live HTTP fetches are skipped and
only cached/persisted data is served (existing consumers keep working
off stale data instead of erroring).
"""

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import requests

from services.outbound_http import get_with_backoff
from services.data_source_registry import (
    register_source, is_source_enabled, record_run_start,
    record_run_success, record_run_error,
)

logger = logging.getLogger(__name__)

FRED_API_KEY_ENV = "FRED_API_KEY"
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
ATTRIBUTION = "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis."

SOURCE_KEY = "fred_macro"
register_source(SOURCE_KEY, "FRED US Macro & Liquidity", "macro")

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "xfinlab.db")


def _init_persistence_table():
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fred_macro_observations (
            series_id TEXT NOT NULL,
            date TEXT NOT NULL,
            value REAL NOT NULL,
            fetched_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (series_id, date)
        )
    """)
    conn.commit()
    conn.close()


_init_persistence_table()


def _persist_observations(series_id: str, observations: list):
    """Best-effort -- a DB write failure must never break a live fetch
    that otherwise succeeded, so this never raises out."""
    if not observations:
        return
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.executemany(
            """
            INSERT INTO fred_macro_observations (series_id, date, value, fetched_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(series_id, date) DO UPDATE SET value=excluded.value, fetched_at=excluded.fetched_at
            """,
            [(series_id, o["date"], o["value"]) for o in observations],
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.info("fred_macro_service: failed to persist %s: %s", series_id, e)


def _load_persisted(series_id: str, n_obs: int) -> Optional[list]:
    """Fallback read used when there's no live fetch and no in-memory
    cache (e.g. right after a restart) -- oldest-first, same shape as
    _fetch_series' normal return."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        rows = conn.execute(
            "SELECT date, value FROM fred_macro_observations WHERE series_id=? ORDER BY date DESC LIMIT ?",
            (series_id, n_obs),
        ).fetchall()
        conn.close()
        if not rows:
            return None
        return [{"date": d, "value": v} for d, v in reversed(rows)]
    except Exception:
        return None

# Series chosen for market relevance + update frequency (all free, all
# public, no "Copyright"-marked series used -- see license_registry.py's
# fred entry on why that check matters for a few series FRED hosts on
# behalf of third parties).
_SERIES = {
    "fed_funds_rate_pct": "FEDFUNDS",     # monthly, effective federal funds rate
    "cpi_inflation_yoy_pct": "CPIAUCSL",  # monthly, CPI (index -- converted to YoY % below)
    "unemployment_pct": "UNRATE",         # monthly
    "yield_curve_10y2y_pct": "T10Y2Y",    # daily, 10Y-2Y treasury spread (recession-watch indicator)
    "jobless_claims_initial": "ICSA",     # weekly, initial jobless claims (level, not %)
}

_CACHE_TTL_SECONDS = 6 * 3600  # 6h -- monthly/weekly series don't need faster refresh
_cache: Dict[str, Dict] = {}  # series_id -> {"fetched_at": epoch, "observations": [...]}


def is_available() -> bool:
    return bool(os.getenv(FRED_API_KEY_ENV))


def _fetch_series(series_id: str, n_obs: int = 13) -> Optional[list]:
    """Returns up to n_obs most recent observations, oldest-first, as
    [{"date": "2026-06-01", "value": 5.33}, ...] -- "." (missing) rows
    are dropped, never coerced. None if no live, cached, or persisted
    data is available at all.

    Fallback order when a fresh HTTP fetch doesn't happen or doesn't
    return data: in-memory cache (fast, same-process, lost on restart)
    -> fred_macro_observations table (survives restart, may be stale)
    -> None.

    2026-09-08 fix (AJ noticed a ~13% error rate on this source in the
    Data Factory panel, e.g. "RRPONTSYD: ...Read timed out"): FRED's own
    API occasionally takes longer than 10s to respond, and
    get_with_backoff()'s retry only covers HTTP 429/503 -- a connection/
    read timeout is a raised exception, not a status code, so it never
    got retried before. Timeout raised 10s -> 20s, plus one short local
    retry (2s pause) scoped to just this function, rather than changing
    outbound_http.py's shared retry behavior for every other caller of
    get_with_backoff() across the codebase."""
    now = datetime.now(timezone.utc).timestamp()
    cached = _cache.get(series_id)
    if cached and (now - cached["fetched_at"]) < _CACHE_TTL_SECONDS:
        return cached["observations"]

    if not is_source_enabled(SOURCE_KEY):
        # Admin has paused this source from the Data Factory panel --
        # serve whatever's already known instead of making new HTTP calls.
        return (cached["observations"] if cached else None) or _load_persisted(series_id, n_obs)

    params = {
        "series_id": series_id,
        "api_key": os.getenv(FRED_API_KEY_ENV),
        "file_type": "json",
        "sort_order": "desc",
        "limit": n_obs,
    }
    record_run_start(SOURCE_KEY)
    payload = None
    for attempt in range(2):
        try:
            res = get_with_backoff(FRED_BASE_URL, params=params, timeout=20)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt == 0:
                logger.info("fred_macro_service: %s timed out, retrying once: %s", series_id, e)
                time.sleep(2)
                continue
            logger.info("fred_macro_service: failed to fetch %s: %s", series_id, e)
            record_run_error(SOURCE_KEY, f"{series_id}: {e}")
            return (cached["observations"] if cached else None) or _load_persisted(series_id, n_obs)
        except Exception as e:
            logger.info("fred_macro_service: failed to fetch %s: %s", series_id, e)
            record_run_error(SOURCE_KEY, f"{series_id}: {e}")
            return (cached["observations"] if cached else None) or _load_persisted(series_id, n_obs)

        if res.status_code != 200:
            logger.info("fred_macro_service: %s returned HTTP %s", series_id, res.status_code)
            record_run_error(SOURCE_KEY, f"{series_id}: HTTP {res.status_code}")
            return (cached["observations"] if cached else None) or _load_persisted(series_id, n_obs)
        payload = res.json()
        break

    rows = payload.get("observations") or []
    observations = []
    for row in reversed(rows):  # API gave newest-first; store oldest-first
        raw_value = row.get("value")
        if raw_value in (None, ".", ""):
            continue  # genuine missing observation -- never fabricate a fill-in
        try:
            observations.append({"date": row.get("date"), "value": round(float(raw_value), 3)})
        except (TypeError, ValueError):
            continue

    if observations:
        _cache[series_id] = {"fetched_at": now, "observations": observations}
        _persist_observations(series_id, observations)
        record_run_success(SOURCE_KEY)
        return observations
    record_run_error(SOURCE_KEY, f"{series_id}: fetch returned zero usable observations")
    return (cached["observations"] if cached else None) or _load_persisted(series_id, n_obs)


def get_us_snapshot() -> Dict:
    """
    Returns:
        {"available": True, "as_of": "...", "attribution": "...",
         "indicators": {
            "fed_funds_rate_pct": {"date": "2026-07-01", "value": 4.33},
            "cpi_inflation_yoy_pct": {"date": "2026-07-01", "value": 2.9},  # derived YoY, see below
            "unemployment_pct": {"date": "2026-07-01", "value": 4.1},
            "yield_curve_10y2y_pct": {"date": "2026-08-08", "value": 0.52},
            "jobless_claims_initial": {"date": "2026-08-02", "value": 224000.0},
         }}
        {"available": False, "message": "..."} -- FRED_API_KEY not configured,
            or every single series failed to fetch (transient FRED outage).
            Partial failures (some series OK, some not) still return
            available:True with only the successful series populated --
            same graceful-degradation convention as macro_data_service.py.
    """
    if not is_available():
        return {"available": False, "message": f"{FRED_API_KEY_ENV} 未設定，FRED美國宏觀數據暫時未開放。"}

    indicators: Dict[str, Optional[Dict]] = {}

    # CPI needs YoY transformation (FRED gives the raw index, not a % --
    # unlike macro_data_service.py's World Bank figure which is already a
    # % annual). Pull 13 monthly points so we can diff month[-1] against
    # month[-13] (~12 months back) for a true year-over-year rate.
    cpi_obs = _fetch_series(_SERIES["cpi_inflation_yoy_pct"], n_obs=13)
    if cpi_obs and len(cpi_obs) >= 2:
        latest, year_ago = cpi_obs[-1], cpi_obs[0]
        if year_ago["value"]:
            yoy_pct = round((latest["value"] - year_ago["value"]) / year_ago["value"] * 100, 2)
            indicators["cpi_inflation_yoy_pct"] = {"date": latest["date"], "value": yoy_pct}
    if "cpi_inflation_yoy_pct" not in indicators:
        indicators["cpi_inflation_yoy_pct"] = None

    for key, series_id in _SERIES.items():
        if key == "cpi_inflation_yoy_pct":
            continue  # handled above
        obs = _fetch_series(series_id, n_obs=1)
        indicators[key] = obs[-1] if obs else None

    if all(v is None for v in indicators.values()):
        return {"available": False, "message": "FRED暫時未能回應（可能係短暫故障）。"}

    return {
        "available": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "attribution": ATTRIBUTION,
        "indicators": indicators,
    }


# ---------------------------------------------------------------------------
# 2026-08-24 (AJ: "以資金流建預測不同資產K線走向ENGINE" -- Capital Flow Engine,
# see services/capital_flow_engine.py): US-only macro LIQUIDITY series,
# sibling to get_us_snapshot() above but a distinct concept -- rates/
# inflation/unemployment describe the economy, these describe how much
# cash is actually sloshing through the financial system, which is the
# genuine upstream driver of "capital flow" (the pasted design doc's
# Layer 2). Same dormant-until-FRED_API_KEY-set convention, same "." ->
# dropped-not-fabricated honesty contract as _SERIES above.
_LIQUIDITY_SERIES = {
    "m2_money_supply": "M2SL",       # monthly, billions USD, seasonally adjusted
    "fed_balance_sheet": "WALCL",    # weekly, millions USD, Fed total assets
    "reverse_repo": "RRPONTSYD",     # daily, billions USD, ON RRP facility usage
}


def get_liquidity_snapshot() -> Dict:
    """
    Returns:
        {"available": True, "as_of": "...", "attribution": "...",
         "indicators": {
            "m2_money_supply": {"date": "...", "value": ..., "mom_change_pct": ...},
            "fed_balance_sheet": {"date": "...", "value": ..., "mom_change_pct": ...},
            "reverse_repo": {"date": "...", "value": ..., "mom_change_pct": ...},
         },
         "liquidity_score": -100..100, "liquidity_direction": "擴張"/"收縮"/"持平"}
        {"available": False, "message": "..."}

    liquidity_score is a simple directional composite, NOT a statistical
    z-score (no long-run baseline maintained here) -- each series
    contributes +1/-1/0 based on whether it's expanding or contracting
    over its last ~30 observations, averaged and scaled to -100..100.
    Reverse repo is inverted before combining: rising RRP means cash is
    parked AT the Fed instead of flowing into markets, so a RISING RRP
    is a CONTRACTIONARY signal for risk assets, the opposite of M2/Fed
    balance sheet expanding.
    """
    if not is_available():
        return {"available": False, "message": f"{FRED_API_KEY_ENV} 未設定，FRED流動性數據暫時未開放。"}

    indicators: Dict[str, Optional[Dict]] = {}
    directional_votes = []

    for key, series_id in _LIQUIDITY_SERIES.items():
        obs = _fetch_series(series_id, n_obs=30)
        if not obs or len(obs) < 2:
            indicators[key] = None
            continue
        latest, earliest = obs[-1], obs[0]
        change_pct = None
        if earliest["value"]:
            change_pct = round((latest["value"] - earliest["value"]) / abs(earliest["value"]) * 100, 3)
        indicators[key] = {"date": latest["date"], "value": latest["value"], "period_change_pct": change_pct}
        if change_pct is not None and abs(change_pct) > 0.05:  # ignore noise-level moves
            vote = 1 if change_pct > 0 else -1
            if key == "reverse_repo":
                vote = -vote  # rising RRP = contractionary, see docstring
            directional_votes.append(vote)

    if all(v is None for v in indicators.values()):
        return {"available": False, "message": "FRED暫時未能回應（可能係短暫故障）。"}

    liquidity_score = round((sum(directional_votes) / len(directional_votes)) * 100, 1) if directional_votes else 0.0
    if liquidity_score >= 25:
        liquidity_direction = "擴張"
    elif liquidity_score <= -25:
        liquidity_direction = "收縮"
    else:
        liquidity_direction = "持平"

    return {
        "available": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "attribution": ATTRIBUTION,
        "indicators": indicators,
        "liquidity_score": liquidity_score,
        "liquidity_direction": liquidity_direction,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(get_us_snapshot(), indent=2, ensure_ascii=False))
    print(json.dumps(get_liquidity_snapshot(), indent=2, ensure_ascii=False))
