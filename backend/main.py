import sys
import os
import logging

# 2026-08-11: root cause found while verifying the Alpaca-first OHLC
# routing in services/technical_analysis_service.py actually fires in
# production -- this app never called logging.basicConfig() anywhere,
# so every logger.info()/.debug() call across the entire codebase
# (services/*, api/*) was silently swallowed by Python's default
# "handler of last resort" (WARNING+ only, stderr). Uvicorn's own
# "INFO:     ...GET... 200 OK" access-log lines were never affected by
# this -- uvicorn configures its own separate loggers regardless -- so
# the app *looked* like it was logging normally while every app-level
# .info() call (including diagnostics like "Alpaca served OHLC for
# %s") was invisible in Railway's log viewer the whole time. Pure
# logging-visibility fix -- does not change any request/response
# behavior, only what gets written to stdout/stderr.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from services.safe_json import SafeJSONResponse
from services.ai_provenance import (
    is_ai_content_route,
    MARKING_HEADER_NAME,
    MARKING_HEADER_VALUE,
)
from api.market import router as market_router
from api.analyze import router as analyze_router
from api.event import router as event_router
from api.full_analysis_v3 import router as full_analysis_router
from api.screener import router as screener_router
from api.portfolio import router as portfolio_router
from api.anomaly import router as anomaly_router
from api.pairs_scan import router as pairs_scan_router
from api.research import router as research_router
from api.report import router as report_router
from auth.auth import router as auth_router
from api.quota import router as quota_router
from api.points import router as points_router
from api.referral import router as referral_router
from api.analytics import router as analytics_router
from api.ai_analysis import router as ai_analysis_router
from api.news_denoise import router as news_denoise_router
from api.company_compare import router as company_compare_router
from api.stress_lab import router as stress_lab_router
from api.chart_analysis import router as chart_analysis_router
from api.chat import router as chat_router
from auth.password_reset import router as password_reset_router
from api.watchlist import router as watchlist_router
from api.admin import router as admin_router
from api.pipeline_api import router as pipeline_router
from api.feedback import router as feedback_router
from api.onboarding import router as onboarding_router
from api.i18n import router as i18n_router
from auth.email_verification import router as email_verification_router
from api.public_stats import router as public_stats_router
from api.public_demo import router as public_demo_router
from api.market_pulse import router as market_pulse_router
from api.hero_showcase import router as hero_showcase_router
from api.push import router as push_router
from api.email_digest import router as email_digest_router
from api.widgets import router as widgets_router
from api.trending import router as trending_router
from api.ticker_search import router as ticker_search_router
from api.sparkline import router as sparkline_router
from api.smart_route import router as smart_route_router
from api.backtest import router as backtest_router
from api.rss_news import router as rss_news_router
from api.global_macro import router as global_macro_router
from api.decision_journal import router as decision_journal_router
from api.agent_debate import router as agent_debate_router
from api.intelligence import router as intelligence_router
from api.mcp_server import router as mcp_router
from api.free_tools_demo import router as free_tools_demo_router
from api.research_card import router as research_card_router
from api.broker_affiliates import router as broker_affiliates_router
from api.historical_analog import router as historical_analog_router
from api.captcha import router as captcha_router
from auth.social_login import router as social_login_router
from auth.whatsapp_auth import router as whatsapp_auth_router
from api.telegram_webhook import router as telegram_webhook_router
from api.webhooks_paddle import router as webhooks_paddle_router
from api.webhooks_stripe import router as webhooks_stripe_router
from api.video import router as video_router
from api.formulas import router as formulas_router


app = FastAPI(
    title="XFINLAB API",
    version="1.0.0",
    # See services/safe_json.py: makes every endpoint that returns a plain
    # dict/list immune to the NaN/Infinity-crashes-JSON-encoding bug
    # confirmed live on GET /api/pipeline/{ticker} (2026-07-25).
    default_response_class=SafeJSONResponse,
)

# One-time, idempotent: merges any users stranded in the legacy
# backend/xfinlab.db (a DB_PATH bug, now fixed) into the canonical,
# Litestream-backed root xfinlab.db. See services/db_migration.py.
from services.db_migration import (
    ensure_wal_mode,
    migrate_legacy_backend_db,
    migrate_audit_logs_nullable_user_id,
    ensure_avatar_gender_column,
    reset_admin_password_if_requested,
)
# Must run first — Litestream can only replicate writes once the DB is in
# WAL mode. See ensure_wal_mode()'s docstring in services/db_migration.py.
ensure_wal_mode()
migrate_legacy_backend_db()
migrate_audit_logs_nullable_user_id()
ensure_avatar_gender_column()
reset_admin_password_if_requested()

# --- Rate limiting (Security & Operations Layer, Phase 2) ---
# Blanket per-IP safety net against abuse/scraping bursts. This is separate
# from services/quota_middleware.py, which limits per-user *feature* usage
# by subscription plan — this limits raw request volume regardless of who's
# calling. In-memory backend is fine for our current single-instance Railway
# deployment; would need a Redis backend if we ever scale to multiple
# instances (limits would then be per-instance, not global).
# 2026-08-24: the Limiter object itself now lives in services/rate_limiter.py
# so AI-cost-heavy routers (api/ai_analysis.py, api/chat.py) can import the
# SAME instance for a stricter per-endpoint @limiter.limit(...) on top of
# this 100/minute blanket default, without importing backend.main back into
# them (circular).
from services.rate_limiter import limiter
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    # Must stay synchronous (not async def) — slowapi's SlowAPIMiddleware
    # calls exception handlers from a sync context and silently falls back
    # to its own default (English) message if the handler is a coroutine.
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "message": "請求太頻繁，請稍後再試。",
        },
    )


# Starlette's add_middleware() inserts at position 0, so the middleware
# added LAST ends up processing requests FIRST (outermost). We need CORS to
# be outermost so it still sees/decorates the 429 response that
# SlowAPIMiddleware returns early (without calling further inward) — so
# SlowAPIMiddleware must be added BEFORE CORSMiddleware here.
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://xfinlab.com", "https://www.xfinlab.com", "http://localhost:3001", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Mainstream FastAPI hardening: standard security response headers.
# Deliberately NOT including Content-Security-Policy here -- the existing
# HTML pages rely on inline <script>/<style> throughout, so a CSP would need
# a per-page audit first or it would break the site (violates "keep go
# build"). These four are safe defaults with no such dependency.
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
    )
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
    return response


# 2026-09-07 (security review fix): admin.html used to send its admin JWT
# as a `?token=...` URL query parameter on every one of api/admin.py's
# ~58 endpoints -- that leaks the token into Railway's access logs, the
# admin's own browser history, and any Referer header a request happens
# to carry. The honest fix is to send it as an `Authorization: Bearer`
# header instead (admin.html was updated in the same change to do
# exactly that, and no longer puts the token in any admin URL).
#
# Every route in api/admin.py still individually declares `token: str`
# as its own parameter, though -- rewriting all ~58 of those signatures
# (several with other positional/default params interleaved, which
# would also force reordering every one to keep valid Python syntax) was
# judged too much blast-radius for a single pass on a live admin panel.
# Instead, this middleware bridges the gap for every request BEFORE
# FastAPI/Starlette's routing resolves query params: if an
# `Authorization: Bearer <token>` header is present and the URL doesn't
# already carry its own `token` query param, it rewrites the request's
# query string to include one, so every existing route's `verify_admin
# (token, ...)` call keeps working completely unchanged. In practice,
# once admin.html only ever sends the header, no admin request leaks a
# token into a URL anymore -- the query-param path stays live purely as
# a backward-compatible fallback (e.g. a saved/bookmarked admin URL, or
# any external script still using the old style), not as the primary
# path.
@app.middleware("http")
async def bridge_admin_token_header_to_query(request: Request, call_next):
    if request.url.path.startswith("/api/admin/"):
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and "token=" not in (request.scope.get("query_string") or b"").decode("latin-1"):
            token = auth[7:].strip()
            if token:
                from urllib.parse import parse_qsl, urlencode

                existing = dict(parse_qsl(request.scope.get("query_string", b"").decode("latin-1")))
                existing["token"] = token
                request.scope["query_string"] = urlencode(existing).encode("latin-1")
    return await call_next(request)


# EU AI Act Article 50(2) good-faith machine-readable marking (see
# services/ai_provenance.py for the full rationale + deadline context).
# Applies only to the explicit allow-list of AI-content-generating routes so
# purely mechanical endpoints (auth/quota/points/push/watchlist CRUD etc.)
# don't carry a misleading "AI-generated" marker.
@app.middleware("http")
async def add_ai_content_marking(request: Request, call_next):
    response = await call_next(request)
    if is_ai_content_route(request.url.path):
        response.headers.setdefault(MARKING_HEADER_NAME, MARKING_HEADER_VALUE)
    return response


# Market
app.include_router(market_router, prefix="/api", tags=["Market"])

# Analysis
app.include_router(analyze_router, prefix="/api", tags=["Analysis"])

# Event
app.include_router(event_router, prefix="/api", tags=["Event"])

# Full AI Analysis (P0 Core)
app.include_router(full_analysis_router, prefix="/api", tags=["Full Analysis"])

# P1 Screener Engine
app.include_router(screener_router, prefix="/api", tags=["Screener"])

# P1 Portfolio Engine
app.include_router(portfolio_router, prefix="/api", tags=["Portfolio"])

# P1 Anomaly Engine
app.include_router(anomaly_router, prefix="/api", tags=["Anomaly"])
app.include_router(pairs_scan_router, prefix="/api", tags=["Pairs Scan"])
app.include_router(research_router, prefix="/api", tags=["Research"])
app.include_router(report_router, prefix="/api", tags=["Report"])
app.include_router(auth_router, prefix="/api", tags=["Auth"])
app.include_router(quota_router, prefix="/api", tags=["Quota"])
app.include_router(points_router, prefix="/api", tags=["Points"])
app.include_router(referral_router, prefix="/api", tags=["Referral"])
app.include_router(analytics_router, prefix="/api", tags=["Analytics"])
app.include_router(ai_analysis_router, prefix="/api", tags=["AI Analysis"])
app.include_router(news_denoise_router, prefix="/api", tags=["News"])
app.include_router(company_compare_router, prefix="/api", tags=["Compare"])
app.include_router(stress_lab_router, prefix="/api", tags=["Stress Lab"])
app.include_router(chart_analysis_router, prefix="/api", tags=["Chart Analysis"])
app.include_router(chat_router, prefix="/api", tags=["Chat"])
app.include_router(password_reset_router, prefix="/api", tags=["Password Reset"])
app.include_router(watchlist_router, prefix="/api", tags=["Watchlist"])
app.include_router(admin_router, prefix="/api", tags=["Admin"])
app.include_router(pipeline_router, prefix="/api", tags=["Pipeline"])
app.include_router(feedback_router, prefix="/api", tags=["Feedback"])
app.include_router(onboarding_router, prefix="/api", tags=["Onboarding"])
app.include_router(i18n_router, prefix="/api", tags=["i18n"])
app.include_router(email_verification_router, prefix="/api", tags=["Email Verification"])
app.include_router(public_stats_router, prefix="/api", tags=["Public Stats"])
app.include_router(public_demo_router, prefix="/api", tags=["Public Demo"])
app.include_router(market_pulse_router, prefix="/api", tags=["Market Pulse"])
app.include_router(hero_showcase_router, prefix="/api", tags=["Hero Showcase"])
app.include_router(push_router, prefix="/api", tags=["Push"])
app.include_router(email_digest_router, prefix="/api", tags=["Email Digest"])
app.include_router(widgets_router, prefix="/api", tags=["Widgets"])
app.include_router(trending_router, prefix="/api", tags=["Trending"])
app.include_router(ticker_search_router, prefix="/api", tags=["Ticker Search"])
app.include_router(sparkline_router, prefix="/api", tags=["Sparkline"])
app.include_router(smart_route_router, prefix="/api", tags=["Smart Route"])
app.include_router(backtest_router, prefix="/api", tags=["Backtest"])
app.include_router(rss_news_router, prefix="/api", tags=["RSS News"])
app.include_router(global_macro_router, prefix="/api", tags=["Global Macro"])
app.include_router(decision_journal_router, prefix="/api", tags=["Decision Journal"])
app.include_router(agent_debate_router, prefix="/api", tags=["Agent Debate"])
app.include_router(intelligence_router, prefix="/api", tags=["Intelligence API"])
app.include_router(mcp_router, prefix="/api", tags=["MCP Server"])
app.include_router(free_tools_demo_router, prefix="/api", tags=["Free Tools Demo"])
app.include_router(research_card_router, prefix="/api", tags=["Research Card"])
app.include_router(broker_affiliates_router, prefix="/api", tags=["Broker Affiliates"])
app.include_router(historical_analog_router, prefix="/api", tags=["Historical Analog"])
app.include_router(captcha_router, prefix="/api", tags=["Captcha"])
app.include_router(social_login_router, prefix="/api", tags=["Social Login"])
app.include_router(whatsapp_auth_router, prefix="/api", tags=["WhatsApp OTP"])
app.include_router(telegram_webhook_router, prefix="/api", tags=["Telegram Webhook"])
app.include_router(webhooks_paddle_router, prefix="/api", tags=["Paddle Webhook"])
app.include_router(webhooks_stripe_router, prefix="/api", tags=["Stripe Payments"])
app.include_router(video_router, prefix="/api", tags=["Video Engine"])
app.include_router(formulas_router, prefix="/api", tags=["Formula Engine"])


# Real scheduled job for the daily Free Signals push (replaces relying
# solely on the lazy "first request of the day recomputes the cache"
# fallback in api/market_pulse.py -- that fallback still exists and
# stays in place, this just makes the push fire at a predictable time
# even if nobody visits the site right after midnight). Runs in-process
# via APScheduler's BackgroundScheduler (non-blocking, no extra worker
# dyno/process needed) -- safe as long as this app runs as a single
# process (the Procfile's `uvicorn backend.main:app` does not pass
# --workers, so this holds today; if that ever changes, this needs to
# move to a single dedicated worker to avoid duplicate sends -- though
# _notify_free_signals_ready's push_send_log guard makes duplicate
# sends merely wasteful, not incorrect).
from apscheduler.schedulers.background import BackgroundScheduler
from api.market_pulse import refresh_free_signals_and_notify

_push_scheduler = BackgroundScheduler(timezone="Asia/Hong_Kong")
_push_scheduler.add_job(
    refresh_free_signals_and_notify,
    "cron",
    hour=8,
    minute=0,
    id="daily_free_signals_push",
    replace_existing=True,
)

# 2026-07-23 (task #326): the security watch previously only ran from an
# external Cowork scheduled task calling scripts/security_scan.py against
# THIS repo's local checkout -- its findings never touched the live
# Railway server or its real xfinlab.db, so there was no way to see a
# scan's results from the admin panel. Running it here too means the
# scan executes on the actual live server, against the actual live site,
# and persists into the same Litestream-backed DB the admin API reads
# from (services/security_scan_service.py). The external scheduled task
# can stay as a separate periodic chat-facing digest; this is what
# powers the in-app "Security Scan" admin page.
def _run_security_scan_job():
    try:
        from services.security_scan_service import run_and_save
        run_and_save()
    except Exception:
        pass

_push_scheduler.add_job(
    _run_security_scan_job,
    "cron",
    hour="*/6",
    minute=15,
    id="security_scan_watch",
    replace_existing=True,
)

# 2026-07-23: growth/anomaly_alerts.py's check_watchlist_anomalies() existed
# and worked, but was only ever wired into growth/scheduler.py -- a
# standalone script (hardcoded local Mac paths, meant to be run via
# `python growth/scheduler.py`) that Railway never starts (the Procfile
# only runs `uvicorn backend.main:app`). That meant nobody's watchlist
# anomaly emails were ever actually sent in production. Wiring the real
# function in here directly, same in-process BackgroundScheduler pattern
# as the two jobs above, at the same 30-minute cadence the dead script
# used to target.
def _run_watchlist_anomaly_job():
    try:
        from growth.anomaly_alerts import check_watchlist_anomalies
        check_watchlist_anomalies()
    except Exception:
        pass

_push_scheduler.add_job(
    _run_watchlist_anomaly_job,
    "interval",
    minutes=30,
    id="watchlist_anomaly_check",
    replace_existing=True,
)

# 2026-08-01 ("自動化可做那裡" audit): plan_expires_at (task #479) already
# auto-demotes a lapsed Pro/annual-Pro user back to Free in
# quota_middleware.resolve_real_plan(), but nobody was ever warned it was
# about to happen -- a pure, avoidable renewal/revenue leak. This finds
# users expiring within 3 days and emails + (if subscribed) pushes them
# once per expiry date. See services/plan_expiry_reminder_service.py.
def _run_plan_expiry_reminder_job():
    try:
        from services.plan_expiry_reminder_service import check_and_notify_expiring_plans
        check_and_notify_expiring_plans()
    except Exception:
        pass

_push_scheduler.add_job(
    _run_plan_expiry_reminder_job,
    "cron",
    hour=9,
    minute=0,
    id="plan_expiry_reminder",
    replace_existing=True,
)

# 2026-08-10 (P1 of the Quant Research Factory roadmap): grades every
# Prediction Ledger row whose horizon has plausibly elapsed -- see
# services/prediction_ledger_service.py's grade_pending_predictions()
# docstring for the exact calendar-day-buffer methodology. Runs once
# daily, well after markets close everywhere XFINLAB covers, so
# "yesterday's" or older predictions have a settled close price to
# grade against.
def _run_prediction_ledger_grading_job():
    try:
        from services.prediction_ledger_service import grade_pending_predictions
        grade_pending_predictions()
    except Exception:
        pass

_push_scheduler.add_job(
    _run_prediction_ledger_grading_job,
    "cron",
    hour=23,
    minute=30,
    id="prediction_ledger_grading",
    replace_existing=True,
)

# 2026-08-24 (referral redesign, "5友付BASIC" 浮動式解鎖 -- full reasoning
# in chat history): re-affirms services/referral_service.py's floating
# Basic referral grant daily. Deliberately its own cron, not folded into
# _run_prediction_ledger_grading_job above, so a failure in one never
# blocks the other (same "each job wrapped in its own try/except,
# tentative-then-committed scheduling" posture every job on this page
# already follows).
def _run_referral_floating_basic_job():
    try:
        from services.referral_service import run_floating_basic_grants
        run_floating_basic_grants()
    except Exception:
        pass

_push_scheduler.add_job(
    _run_referral_floating_basic_job,
    "cron",
    hour=22,
    minute=15,
    id="referral_floating_basic",
    replace_existing=True,
)

# 2026-08-10 (P2+P3 of the Quant Research Factory roadmap, task #792):
# keeps services/formula_composer_service.py's and services/regime_
# router_service.py's persisted leaderboards fresh WITHOUT requiring a
# user to visit a page first (both /formula-composer/{ticker}/scan and
# /regime-router/{ticker}/scan are otherwise only ever triggered
# manually via admin.html or an API call). Scans the exact same fixed
# 8-ticker Market Pulse basket track_record_service.py already uses
# (_BASKET = SPY/QQQ/DIA/IWM/XLK/XLF/XLE/BTC-USD) -- same "reuse the
# existing basket, don't invent a new one" reasoning as that module.
# Each run is 35 walk-forward validations (composer) + 35 full-history
# simulations (regime router) PER symbol, so this is deliberately once-
# daily, off-peak, and continues past any single symbol's failure
# (network hiccup / provider outage) rather than aborting the whole
# batch -- the leaderboards are additive/upsert (ON CONFLICT DO UPDATE),
# so a partial run still leaves yesterday's data for symbols it didn't
# reach.
_QRF_SCAN_BASKET = ["SPY", "QQQ", "DIA", "IWM", "XLK", "XLF", "XLE", "BTC-USD"]


def _run_quant_research_factory_scan_job():
    from services.formula_composer_service import run_scan as _composer_run_scan
    from services.regime_router_service import run_regime_scan as _regime_run_scan

    for symbol in _QRF_SCAN_BASKET:
        try:
            _composer_run_scan(symbol)
        except Exception:
            pass
        try:
            _regime_run_scan(symbol)
        except Exception:
            pass

_push_scheduler.add_job(
    _run_quant_research_factory_scan_job,
    "cron",
    hour=2,
    minute=0,
    id="quant_research_factory_basket_scan",
    replace_existing=True,
)

# 2026-08-24 (Capital Flow Engine, task following on from Gann/à la carte
# work): services/capital_flow_engine.py's snapshot (7-region + 11-sector
# rotation + FRED liquidity, ~18 basket tickers) MUST be computed off the
# request path -- confirmed live that a cache-miss inline compute inside
# get_technical_analysis() made the first /chart-search request after
# every cache expiry time out (>180s). This cron is now the ONLY thing
# that ever calls the slow path (refresh_capital_flow_cache ->
# get_capital_flow_snapshot(force_refresh=True)); every live request just
# reads whatever's already cached, same interval-trigger pattern as
# _run_watchlist_anomaly_job above.
def _run_capital_flow_refresh_job():
    try:
        from services.capital_flow_engine import refresh_capital_flow_cache
        refresh_capital_flow_cache()
    except Exception:
        pass

_push_scheduler.add_job(
    _run_capital_flow_refresh_job,
    "interval",
    minutes=30,
    id="capital_flow_engine_refresh",
    replace_existing=True,
)

# 2026-08-26 (Data Factory Step 5, AJ: "自動排程" -- all 3 collectors built
# this batch (FRED macro persistence migration, CFTC COT, SEC 13F
# ownership) were previously only ever refreshed on-demand: FRED/CFTC via
# lazy in-memory-cache-miss on whatever request happened to ask, SEC 13F
# only via the admin panel's manual "Fetch Latest 13F Now" button. That
# meant xfinlab.db's persisted history (the entire point of the Step 2
# migration -- surviving a Railway restart) could go stale indefinitely
# if nothing happened to trigger a live fetch. Same in-process
# BackgroundScheduler + try/except-per-job posture as every job above;
# each collector's own is_source_enabled()/record_run_* bookkeeping
# (services/data_source_registry.py) still applies whether the fetch was
# triggered by this cron or by a live request, so the Data Factory admin
# panel's run/error counts capture both equally.
def _run_fred_macro_refresh_job():
    try:
        from services import fred_macro_service
        if fred_macro_service.is_available():
            fred_macro_service.get_us_snapshot()
            fred_macro_service.get_liquidity_snapshot()
    except Exception:
        pass

_push_scheduler.add_job(
    _run_fred_macro_refresh_job,
    "cron",
    hour=3,
    minute=0,
    id="fred_macro_refresh",
    replace_existing=True,
)


# 2026-08-30 (Real Estate Intelligence, first of 3 cross-industry
# expansion candidates AJ picked "由1開始順住做" -- start with real
# estate): same warm-the-cache-even-without-traffic reasoning as
# fred_macro_refresh above. Only 4 FRED series, fetched once regardless
# of how many housing-linked tickers get queried that day (all tickers
# share the same national indicators, see services/real_estate_service
# .py's _SERIES).
def _run_real_estate_refresh_job():
    try:
        from services import real_estate_service
        if real_estate_service.is_available():
            for meta in real_estate_service._SERIES.values():
                real_estate_service._fetch_series(meta["series_id"], n_obs=1)
    except Exception:
        pass

_push_scheduler.add_job(
    _run_real_estate_refresh_job,
    "cron",
    hour=3,
    minute=15,
    id="real_estate_refresh",
    replace_existing=True,
)


# 2026-08-31 -- same pre-warm reasoning as _run_real_estate_refresh_job
# above, for services/supply_chain_service.py's 5 series.
def _run_supply_chain_refresh_job():
    try:
        from services import supply_chain_service
        if supply_chain_service.is_available():
            for meta in supply_chain_service._SERIES.values():
                supply_chain_service._fetch_series(meta["series_id"], n_obs=1)
    except Exception:
        pass

_push_scheduler.add_job(
    _run_supply_chain_refresh_job,
    "cron",
    hour=3,
    minute=20,
    id="supply_chain_refresh",
    replace_existing=True,
)


# 2026-08-31 -- same pre-warm reasoning as _run_real_estate_refresh_job
# above, for services/consumer_demand_service.py's 4 series.
def _run_consumer_demand_refresh_job():
    try:
        from services import consumer_demand_service
        if consumer_demand_service.is_available():
            for meta in consumer_demand_service._SERIES.values():
                consumer_demand_service._fetch_series(meta["series_id"], n_obs=1)
    except Exception:
        pass

_push_scheduler.add_job(
    _run_consumer_demand_refresh_job,
    "cron",
    hour=3,
    minute=25,
    id="consumer_demand_refresh",
    replace_existing=True,
)


def _run_cftc_cot_refresh_job():
    try:
        from services.cftc_cot_service import get_snapshot
        get_snapshot()
    except Exception:
        pass

# CFTC publishes COT every Friday ~3:30pm ET (~4:30am Sat in Asia/Hong_Kong
# during EDT, ~3:30am during EST) -- Saturday morning gives a safe buffer
# past either case instead of chasing the exact DST-dependent minute.
_push_scheduler.add_job(
    _run_cftc_cot_refresh_job,
    "cron",
    day_of_week="sat",
    hour=10,
    minute=0,
    id="cftc_cot_refresh",
    replace_existing=True,
)


def _run_sec_13f_refresh_job():
    try:
        from services.sec_ownership_service import refresh_all
        refresh_all()
    except Exception:
        pass

# 13F-HR is filed quarterly (within 45 days of quarter-end) -- monthly is
# deliberately more frequent than strictly necessary rather than trying
# to predict each filer's exact filing date; refresh_filer's upsert on
# (filer_cik, period_of_report, cusip) makes an unchanged re-fetch a
# harmless no-op, so the extra runs cost a few HTTP calls, nothing else.
_push_scheduler.add_job(
    _run_sec_13f_refresh_job,
    "cron",
    day=1,
    hour=4,
    minute=0,
    id="sec_13f_refresh",
    replace_existing=True,
)

def _run_sec_xbrl_refresh_job():
    try:
        from services.sec_xbrl_service import get_company_facts
        from services.sec_13d_13g_service import _distinct_known_tickers
        for ticker in _distinct_known_tickers():
            get_company_facts(ticker)
    except Exception:
        pass

# 2026-08-28 (Data Factory batch, AJ: "咁你一次過起"): fundamentals only
# change once a quarter (a new 10-K), so once a day is more than enough.
# Reuses sec_13d_13g_service's organically-grown known-ticker list rather
# than inventing a third "watched tickers" universe -- same rationale as
# that module's own refresh_all().
_push_scheduler.add_job(
    _run_sec_xbrl_refresh_job,
    "cron",
    hour=6,
    minute=0,
    id="sec_xbrl_refresh",
    replace_existing=True,
)

def _run_cboe_vix_refresh_job():
    try:
        from services.cboe_vix_service import get_snapshot
        snapshot = get_snapshot()
        # 2026-08-28 (AJ: "重有咩賺錢位" -> Intelligence API webhooks,
        # Pro-only): same refresh this job already did, now also checks
        # whether the structure actually changed and fires
        # vix_regime_change webhooks if so. Best-effort, own try/except --
        # a webhook-delivery problem must never break the underlying VIX
        # cache refresh this job exists for.
        if snapshot and snapshot.get("available"):
            try:
                from services.webhook_service import check_and_deliver_vix_regime_change
                check_and_deliver_vix_regime_change(snapshot.get("structure"))
            except Exception:
                pass
    except Exception:
        pass

# VIX term structure updates once per trading day (close) -- a single
# daily refresh after market close is enough.
_push_scheduler.add_job(
    _run_cboe_vix_refresh_job,
    "cron",
    hour=21,
    minute=30,
    id="cboe_vix_refresh",
    replace_existing=True,
)

def _run_fdic_bank_health_refresh_job():
    try:
        from services.fdic_banking_service import get_snapshot
        get_snapshot()
    except Exception:
        pass

# Call Report data updates quarterly -- a weekly refresh is already
# generous, avoids hammering FDIC's public API for data that barely moves.
_push_scheduler.add_job(
    _run_fdic_bank_health_refresh_job,
    "cron",
    day_of_week="mon",
    hour=6,
    minute=30,
    id="fdic_bank_health_refresh",
    replace_existing=True,
)

def _run_usda_agriculture_refresh_job():
    try:
        from services.usda_agriculture_service import get_snapshot
        get_snapshot()
    except Exception:
        pass

# USDA price-received series update monthly/annually -- daily refresh
# costs nothing (single cheap request/series) and keeps it always fresh.
_push_scheduler.add_job(
    _run_usda_agriculture_refresh_job,
    "cron",
    hour=6,
    minute=15,
    id="usda_agriculture_refresh",
    replace_existing=True,
)

def _run_sec_13d_13g_refresh_job():
    try:
        from services.sec_13d_13g_service import refresh_all
        results = refresh_all()
        # 2026-08-28 (AJ: "重有咩賺錢位" -> Intelligence API webhooks,
        # Pro-only): refresh_all() already returns {ticker: filing_count}
        # -- reuse it directly to detect a per-ticker count increase and
        # fire new_13d_filing webhooks, same best-effort posture as the
        # VIX job above.
        try:
            from services.webhook_service import check_and_deliver_new_13d_filings
            check_and_deliver_new_13d_filings(results)
        except Exception:
            pass
    except Exception:
        pass

# 2026-08-27 (AJ: "13D/13G加排程"): unlike 13F (quarterly, monthly job is
# plenty), a 13D/13G filing is event-driven and can land on any trading
# day -- an activist's stake crossing 5% is market-moving news the moment
# it's filed, so this runs daily like EIA/Treasury rather than monthly
# like 13F. refresh_all() only re-checks tickers already persisted from a
# real user lookup (see that function's own docstring for why there's no
# fixed "watched ticker" list here the way 13F has watched filers), so
# this job's cost grows with real usage, not a guessed universe.
_push_scheduler.add_job(
    _run_sec_13d_13g_refresh_job,
    "cron",
    hour=5,
    minute=15,
    id="sec_13d_13g_refresh",
    replace_existing=True,
)

def _run_eia_energy_refresh_job():
    try:
        from services.eia_energy_service import get_snapshot
        get_snapshot()
    except Exception:
        pass

# EIA spot prices update daily, storage weekly (Thursdays) -- once a day
# is enough for both without over-polling a series that only moves
# weekly.
_push_scheduler.add_job(
    _run_eia_energy_refresh_job,
    "cron",
    hour=5,
    minute=0,
    id="eia_energy_refresh",
    replace_existing=True,
)


def _run_treasury_fiscal_refresh_job():
    try:
        from services.treasury_fiscal_service import get_snapshot
        get_snapshot()
    except Exception:
        pass

# Both Treasury series update once per business day (end of day) -- a
# single daily refresh keeps this fresh without hammering a no-API-key
# public endpoint.
_push_scheduler.add_job(
    _run_treasury_fiscal_refresh_job,
    "cron",
    hour=5,
    minute=30,
    id="treasury_fiscal_refresh",
    replace_existing=True,
)


def _run_opportunity_radar_shift_check_job():
    try:
        from services.opportunity_radar_service import get_opportunity_radar
        radar = get_opportunity_radar()
        # 2026-08-31 (AJ: "加Webhook提醒" -- follow-up to the Opportunity
        # Radar energy/agriculture expansion): this job does NOT re-fetch
        # anything itself -- get_opportunity_radar() just re-reads
        # whatever the 6 refresh jobs above already cached/persisted
        # today, so running this last (after fred_macro/real_estate/
        # supply_chain/consumer_demand/eia_energy/usda_agriculture have
        # each had their own cron slot) gives a snapshot of the
        # freshest same-day data. Fires opportunity_radar_shift webhooks
        # only for an industry whose net improving/worsening lean
        # actually flipped since yesterday's check -- see
        # webhook_service.check_and_deliver_opportunity_radar_shift()'s
        # own docstring for the exact no-noise rule (a flip into/out of
        # "mixed" alone never fires).
        if radar and radar.get("available"):
            try:
                from services.webhook_service import check_and_deliver_opportunity_radar_shift
                check_and_deliver_opportunity_radar_shift(radar.get("industries") or {})
            except Exception:
                pass
    except Exception:
        pass

# Runs after all 6 underlying refresh jobs above (latest is
# usda_agriculture_refresh at 6:15) so it reads same-day-fresh data --
# see the job's own comment.
_push_scheduler.add_job(
    _run_opportunity_radar_shift_check_job,
    "cron",
    hour=6,
    minute=45,
    id="opportunity_radar_shift_check",
    replace_existing=True,
)


def _run_openfda_refresh_job():
    try:
        from services.openfda_service import get_consumer_safety_context_for_ticker, _TICKER_TO_KEYWORDS
        for ticker in _TICKER_TO_KEYWORDS:
            try:
                get_consumer_safety_context_for_ticker(ticker)
            except Exception:
                pass
    except Exception:
        pass

# 2026-08-31 (AJ: "感官與消費者科學" follow-up, "起"): openFDA needs no
# key and has no natural "current value" (event-driven recalls/adverse
# events, see that module's docstring) -- this job just warms the cache
# + persistence table for every mapped ticker daily, so a live user
# request never has to wait on ~4 datasets x several keywords of
# synchronous external search. Off-hours, no other job runs at this slot.
_push_scheduler.add_job(
    _run_openfda_refresh_job,
    "cron",
    hour=7,
    minute=0,
    id="openfda_refresh",
    replace_existing=True,
)


def _run_cpsc_refresh_job():
    try:
        from services.cpsc_service import get_recall_context_for_ticker, _TICKER_TO_KEYWORDS
        for ticker in _TICKER_TO_KEYWORDS:
            try:
                get_recall_context_for_ticker(ticker)
            except Exception:
                pass
    except Exception:
        pass

# Same warm-cache rationale as openfda_refresh above. Separate slot so a
# slow/erroring CPSC backend (see services/cpsc_service.py's live-
# reliability note) never delays the openFDA job.
_push_scheduler.add_job(
    _run_cpsc_refresh_job,
    "cron",
    hour=7,
    minute=15,
    id="cpsc_refresh",
    replace_existing=True,
)

_push_scheduler.start()


@app.get("/")
def root():
    return {
        "name": "XFINLAB API",
        "version": "1.0.0",
        "status": "running"
    }


@app.get("/llms.txt", include_in_schema=False)
@limiter.exempt
def llms_txt(request: Request):
    """2026-08-28 (AJ: 0-cost marketing -- AI agent discoverability):
    mirrors the static /llms.txt served on www.xfinlab.com (Vercel) so an
    agent that crawls api.xfinlab.com directly still finds it, without
    duplicating the text in two places -- reads the same repo-root file
    Vercel serves. Fails open with a minimal fallback rather than a 500 if
    the file is ever missing (e.g. a deploy path mismatch), matching this
    codebase's honest-empty convention elsewhere."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "llms.txt")
        with open(path, "r", encoding="utf-8") as f:
            return PlainTextResponse(f.read())
    except Exception:
        return PlainTextResponse(
            "# XFINLAB Intelligence API\n\n"
            "> Financial intelligence API for developers and AI agents.\n\n"
            "Docs: https://www.xfinlab.com/intelligence-api.html\n"
        )


@app.get("/health")
@limiter.exempt
def health(request: Request):
    """
    Lightweight, unauthenticated health check for Railway / uptime
    monitors. Deliberately separate from /api/admin/health, which needs an
    admin token and makes slow external network calls (market/news/crypto
    APIs) — not something a monitor should be hitting every 30 seconds.
    This one just confirms the process is up and the database is reachable.
    Exempt from rate limiting so frequent automated pings never 429.
    """
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "xfinlab.db")
        conn = sqlite3.connect(db_path)
        conn.execute("SELECT 1")
        conn.close()
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {str(e)[:100]}"

    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "database": db_status,
    }