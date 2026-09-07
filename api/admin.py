import sqlite3
import os
import requests
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from backend.auth.jwt_handler import verify_token
from services.audit_log_service import log_action, get_recent_logs
from services.request_ip import get_client_ip

router = APIRouter()
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "xfinlab.db")
ADMIN_EMAIL = "abcoaj888@gmail.com"

# Optional defense-in-depth: if ADMIN_IP_ALLOWLIST is set (comma-separated
# IPs, e.g. "1.2.3.4,5.6.7.8"), admin endpoints reject any caller whose IP
# isn't on the list -- even with a valid, correctly-signed admin token.
# This protects against the scenario where JWT_SECRET or a live admin
# token leaks (e.g. via a compromised browser/device) but the attacker
# isn't calling from one of your own known IPs. Backwards compatible:
# unset (the default) means no restriction at all, matching prior
# behavior exactly -- this only activates if you opt in.
_ADMIN_IP_ALLOWLIST = [
    ip.strip() for ip in os.getenv("ADMIN_IP_ALLOWLIST", "").split(",") if ip.strip()
]

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# Default flag set + state. admin.html's toggle previously did nothing but
# flip a CSS class client-side -- looked functional, wasn't. This makes it
# real: values are persisted in a feature_flags table, seeded with these
# defaults the first time the table is touched. NOTE: persisting the value
# is as far as this goes for now -- none of the 7 actual endpoints
# (research_agent/portfolio/anomaly/screener/chart_analysis/telegram_bot/
# referral) currently check this table before serving a request, so
# toggling a flag off here does NOT yet disable the feature. Wiring real
# enforcement into each endpoint is separate, larger-scope work.
_DEFAULT_FLAGS = {
    "research_agent": True,
    "portfolio": True,
    "anomaly": True,
    "screener": True,
    "chart_analysis": True,
    "telegram_bot": True,
    "referral": True,
    # task #333: Google/LINE/WhatsApp login were all built at once per the
    # user's own instruction ("build all 3 first, decide which to show
    # later") -- default OFF so nothing appears on login.html until each
    # is explicitly toggled on here (and its env vars are actually
    # configured; see backend/auth/social_login.py + whatsapp_auth.py).
    "google_login": False,
    "line_login": False,
    "whatsapp_otp": False,
    # 2026-07-30 (Intelligence API v1 "Request Early Access" landing page):
    # lets the admin show/hide each pricing tier card on
    # intelligence-api.html without a redeploy, e.g. hiding "Enterprise"
    # until there's an actual reason to show it, or hiding "Free" once the
    # early-access phase ends and every signup should go through sales
    # conversation first. All default ON (matches this page's initial
    # 3-tier launch state) -- toggle off individually as needed.
    "intel_plan_free_visible": True,
    "intel_plan_pro_visible": True,
    "intel_plan_enterprise_visible": True,
    # Growth OS Phase 1 (2026-08-02): AI SEO Engine. Gates the
    # /admin/seo/generate endpoint below -- when off, generation is
    # refused even with a valid admin token, so the whole page-creation
    # pipeline can be paused instantly (e.g. mid-investigation of a bad
    # generated page) without touching code or env vars. Read-only
    # endpoints (/admin/seo/pages, /admin/seo/suggestions) are unaffected
    # since they can't modify anything.
    "seo_auto_engine": True,
    # Growth OS Phase 2 (2026-08-02): gates the EN/ES multi-language
    # social-content fan-out in services/content_repurpose_service.py's
    # generate_content_variants_multilang(), called from api/
    # market_pulse.py's daily job. Off = only the original "zh" variants
    # (twitter/threads/facebook/linkedin/instagram/discord/reddit/email/
    # push) are generated, matching pre-Phase-2 behavior exactly.
    "content_engine_multilang": True,
    # Growth OS Phase 3 (2026-08-02): gates the actual daily email send in
    # api/market_pulse.py (services/email_digest_service.py's
    # send_daily_digest, via the existing SMTP mailbox). Off = confirmed
    # subscribers simply don't get today's email; subscribe/confirm/
    # unsubscribe endpoints keep working regardless (only the daily send
    # step checks this flag).
    "email_digest_engine": True,
    # Growth OS Phase 4 (2026-08-02): gates api/widgets.py's two public
    # data endpoints (sentiment-index/heatmap). embed.js itself always
    # loads (so a third-party page embedding it never gets a 404) but
    # renders nothing meaningful while this is off.
    "widget_engine": True,
    # Growth OS Phase 7 (2026-08-04): gates the Video Engine's daily
    # short-video generation (services/video_engine_service.py). Default
    # OFF -- same "credential-dependent feature defaults off" pattern as
    # google_login/line_login/whatsapp_otp above, since this needs a real
    # GOOGLE_TTS_API_KEY set in Railway before it can do anything (the
    # service's own is_available() check degrades gracefully either way,
    # but there's no reason to let the admin panel's "Generate Now"
    # button attempt a call that's guaranteed to fail before that env var
    # is actually configured).
    "video_engine": False,
}

def init_feature_flags_table():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feature_flags (
            key TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    existing = {r["key"] for r in conn.execute("SELECT key FROM feature_flags").fetchall()}
    for key, default_enabled in _DEFAULT_FLAGS.items():
        if key not in existing:
            conn.execute(
                "INSERT INTO feature_flags (key, enabled) VALUES (?, ?)",
                (key, 1 if default_enabled else 0),
            )
    conn.commit()
    conn.close()

init_feature_flags_table()

def verify_admin(token: str, action: str = None, request: Request = None):
    """
    Verifies the caller is the admin. When `action` is supplied, also
    writes an audit_logs entry for it -- every admin endpoint below passes
    its own action name so there's a full trail of what the admin did.
    """
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.get("sub") != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Admin access required")

    ip = get_client_ip(request) if request else None
    if _ADMIN_IP_ALLOWLIST and ip not in _ADMIN_IP_ALLOWLIST:
        # Logged with user_id=None (like login_failed) so blocked attempts
        # are visible in the audit trail even though they never got in.
        log_action(None, f"admin_ip_blocked:{action or 'unknown'}", ip)
        raise HTTPException(status_code=403, detail="Admin access not permitted from this network")

    if action:
        log_action(payload.get("id"), f"admin:{action}", ip)
    return payload

@router.get("/admin/stats")
def get_stats(token: str, request: Request):
    verify_admin(token, "get_stats", request)
    conn = get_db()
    total_users = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    pro_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE plan='pro'").fetchone()["c"]
    total_events = conn.execute("SELECT COUNT(*) as c FROM user_analytics").fetchone()["c"]
    today_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE created_at >= date('now')").fetchone()["c"]

    # DAU
    dau = conn.execute(
        "SELECT COUNT(DISTINCT user_id) as c FROM user_analytics WHERE created_at >= date('now')"
    ).fetchone()["c"]

    # MAU
    mau = conn.execute(
        "SELECT COUNT(DISTINCT user_id) as c FROM user_analytics WHERE created_at >= date('now', '-30 days')"
    ).fetchone()["c"]

    # Today events breakdown
    today_analyses = conn.execute(
        "SELECT COUNT(*) as c FROM user_analytics WHERE event_type='search' AND created_at >= date('now')"
    ).fetchone()["c"]

    today_api_calls = conn.execute(
        "SELECT COUNT(*) as c FROM user_analytics WHERE created_at >= date('now')"
    ).fetchone()["c"]

    # Top searches
    top_searches = conn.execute("""
        SELECT event_data, COUNT(*) as c FROM user_analytics
        WHERE event_type='search'
        GROUP BY event_data ORDER BY c DESC LIMIT 10
    """).fetchall()

    # Trending stocks
    top_analysis = conn.execute("""
        SELECT event_data, COUNT(*) as c FROM user_analytics
        WHERE event_type='search'
        GROUP BY event_data ORDER BY c DESC LIMIT 5
    """).fetchall()

    conn.close()

    return {
        "total_users": total_users,
        "pro_users": pro_users,
        "free_users": total_users - pro_users,
        "today_new_users": today_users,
        "total_events": total_events,
        "dau": dau,
        "mau": mau,
        "today_analyses": today_analyses,
        "today_api_calls": today_api_calls,
        "top_searches": [dict(r) for r in top_searches],
        "top_analysis": [dict(r) for r in top_analysis],
    }

@router.get("/admin/health")
def get_health(token: str, request: Request):
    verify_admin(token, "get_health", request)
    results = {}

    # Market API
    try:
        import yfinance as yf
        t = yf.Ticker("AAPL")
        price = t.info.get("regularMarketPrice") or t.fast_info.last_price
        results["market_api"] = {"status": "online", "detail": f"AAPL ${price:.2f}"}
    except Exception as e:
        results["market_api"] = {"status": "offline", "detail": str(e)[:50]}

    # News API
    try:
        news_key = os.getenv("NEWS_API_KEY", "")
        res = requests.get(f"https://newsapi.org/v2/top-headlines?country=us&apiKey={news_key}&pageSize=1", timeout=5)
        results["news_api"] = {"status": "online" if res.status_code == 200 else "offline", "detail": f"Status {res.status_code}"}
    except Exception as e:
        results["news_api"] = {"status": "offline", "detail": str(e)[:50]}

    # Crypto API
    try:
        res = requests.get("https://api.coingecko.com/api/v3/ping", timeout=5)
        results["crypto_api"] = {"status": "online" if res.status_code == 200 else "offline", "detail": "CoinGecko"}
    except Exception as e:
        results["crypto_api"] = {"status": "offline", "detail": str(e)[:50]}

    # Groq AI
    try:
        groq_key = os.getenv("GROQ_API_KEY", "")
        results["groq_ai"] = {"status": "online" if groq_key else "offline", "detail": "API Key configured" if groq_key else "No API Key"}
    except Exception:
        results["groq_ai"] = {"status": "offline", "detail": "Error"}

    # Database
    try:
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()
        results["database"] = {"status": "online", "detail": "SQLite Connected"}
    except Exception as e:
        results["database"] = {"status": "offline", "detail": str(e)[:50]}

    # Litestream / WAL diagnostics (2026-07-11) -- added while debugging why
    # the admin account kept disappearing after Railway redeploys. Litestream
    # can only replicate writes to R2 when the DB is in WAL journal mode
    # (see services/db_migration.py's ensure_wal_mode() for the full story).
    # This surfaces that state directly instead of having to infer it
    # indirectly from total_users counts after the fact.
    try:
        conn = get_db()
        mode_row = conn.execute("PRAGMA journal_mode").fetchone()
        journal_mode = mode_row[0] if mode_row else "unknown"
        conn.close()

        wal_path = DB_PATH + "-wal"
        wal_exists = os.path.exists(wal_path)
        wal_size = os.path.getsize(wal_path) if wal_exists else 0
        db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        db_mtime = (
            datetime.fromtimestamp(os.path.getmtime(DB_PATH), tz=timezone.utc).isoformat()
            if os.path.exists(DB_PATH)
            else None
        )

        results["litestream_wal"] = {
            "status": "online" if journal_mode.lower() == "wal" else "offline",
            "detail": (
                f"journal_mode={journal_mode}, wal_file_exists={wal_exists}, "
                f"wal_size_bytes={wal_size}, db_size_bytes={db_size}, "
                f"db_last_modified={db_mtime}"
            ),
        }
    except Exception as e:
        results["litestream_wal"] = {"status": "offline", "detail": str(e)[:100]}

    return results

@router.get("/admin/users")
def get_users(token: str, request: Request, page: int = 1, limit: int = 20):
    verify_admin(token, "get_users", request)
    conn = get_db()
    offset = (page - 1) * limit
    users = conn.execute(
        "SELECT id, email, name, plan, email_verified, created_at FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset)
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    conn.close()
    return {"users": [dict(u) for u in users], "total": total, "page": page}

@router.post("/admin/users/{user_id}/upgrade")
def upgrade_user(user_id: int, token: str, request: Request):
    verify_admin(token, f"upgrade_user:{user_id}", request)
    conn = get_db()
    conn.execute("UPDATE users SET plan='pro' WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"User {user_id} upgraded to Pro"}

@router.post("/admin/users/{user_id}/downgrade")
def downgrade_user(user_id: int, token: str, request: Request):
    verify_admin(token, f"downgrade_user:{user_id}", request)
    conn = get_db()
    conn.execute("UPDATE users SET plan='free' WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"User {user_id} downgraded to Free"}

@router.post("/admin/users/{user_id}/mark-annual-pro")
def mark_annual_pro(user_id: int, token: str, request: Request):
    """2026-07-27: manual stand-in for a real payment webhook -- confirms
    `user_id` paid for an ANNUAL Pro subscription (there is no live Stripe/
    PayPal integration yet). Sets their real plan to Pro with a genuine
    1-year expiry, and -- if they were referred -- grants the referrer 1
    year of Pro (or Pro+, once they've referred REFERRAL_PROPLUS_THRESHOLD
    paying annual-Pro conversions) via services/referral_service.py. Once
    a real payment gateway exists, its webhook should call
    ReferralService.mark_annual_pro_payment() directly instead of this
    endpoint; the reward logic itself doesn't change."""
    verify_admin(token, f"mark_annual_pro:{user_id}", request)
    from services.referral_service import ReferralService
    conn = get_db()
    exists = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if not exists:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    result = ReferralService.mark_annual_pro_payment(user_id)
    return {"status": "ok", **result}

@router.get("/admin/content-variants/today")
def get_content_variants(token: str, request: Request):
    """2026-07-27 'Level 1 content leverage' growth batch: returns
    today's ready-to-copy-paste post text (X/Threads/LinkedIn/Facebook/
    email/push), generated daily by api/market_pulse.py's
    _notify_free_signals_ready() from the same real signals data behind
    free-signals.html and the Telegram push. Read-only -- does not post
    anywhere; AJ copies each field into that platform's own composer."""
    verify_admin(token, "get_content_variants", request)
    from services.content_repurpose_service import get_latest_variants
    return get_latest_variants()

@router.post("/admin/content-variants/regenerate")
def regenerate_content_variants(token: str, request: Request):
    """Manual on-demand regeneration -- bypasses the daily job's
    once-per-day idempotency guard (that guard exists to stop the
    automated cron from re-firing, not to stop an admin from refreshing
    on purpose, e.g. to test this feature or pull a fresh copy mid-day
    after signals have moved)."""
    verify_admin(token, "regenerate_content_variants", request)
    from datetime import date
    from api.market_pulse import _compute_free_signals
    from services.content_repurpose_service import (
        generate_content_variants,
        generate_content_variants_multilang,
        save_variants,
    )
    cache = _compute_free_signals()
    variants = generate_content_variants(cache)
    # Growth OS Phase 2: match the daily job's behavior (api/market_pulse.py)
    # so a manual regenerate doesn't silently drop the EN/ES fan-out --
    # same content_engine_multilang flag check.
    conn = get_db()
    row = conn.execute("SELECT enabled FROM feature_flags WHERE key='content_engine_multilang'").fetchone()
    conn.close()
    if row is None or row["enabled"]:
        try:
            variants["multilang"] = generate_content_variants_multilang(cache)
        except Exception:
            pass
    save_variants(date.today().isoformat(), variants)
    return variants

@router.get("/admin/seo/pages")
def seo_list_pages(token: str, request: Request):
    """Growth OS Phase 1 -- read-only: how many ticker/comparison SEO
    landing pages exist right now (glob of the repo root, see
    services/seo_page_generator.py), plus how many of those were created
    via this engine specifically (vs. the earlier hand-built batch)."""
    verify_admin(token, "seo_list_pages", request)
    from services.seo_page_generator import list_existing_pages
    return list_existing_pages()

@router.get("/admin/seo/suggestions")
def seo_suggestions(token: str, request: Request, limit: int = 30):
    """Growth OS Phase 1 -- read-only: assets from the site's own
    autocomplete.js ticker universe that don't have a landing page yet,
    ranked by their existing popularity score. Answers "what should I
    generate next" instead of guessing."""
    verify_admin(token, "seo_suggestions", request)
    from services.seo_page_generator import suggest_candidates
    return {"candidates": suggest_candidates(limit=min(limit, 100))}

@router.post("/admin/seo/generate")
def seo_generate(token: str, request: Request, body: dict = {}):
    """Growth OS Phase 1 -- creates one new ticker landing page + appends
    it to sitemap.xml. Gated by the seo_auto_engine feature flag so it can
    be paused instantly from the Feature Flags panel. Never overwrites an
    existing page (services/seo_page_generator.py's create_ticker_page
    raises FileExistsError instead)."""
    verify_admin(token, "seo_generate", request)
    conn = get_db()
    row = conn.execute("SELECT enabled FROM feature_flags WHERE key='seo_auto_engine'").fetchone()
    conn.close()
    if row is not None and not row["enabled"]:
        raise HTTPException(status_code=403, detail="SEO Auto Engine is currently disabled (Feature Flags)")

    ticker = (body.get("ticker") or "").strip()
    company_name = (body.get("company_name") or "").strip()
    category = (body.get("category") or "stock").strip()
    related = body.get("related") or []
    if not ticker or not company_name:
        raise HTTPException(status_code=400, detail="ticker and company_name are required")

    from services.seo_page_generator import create_ticker_page
    try:
        result = create_ticker_page(ticker, company_name, category, related)
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", **result}

@router.get("/admin/email/stats")
def email_digest_stats(token: str, request: Request):
    """Growth OS Phase 3 -- subscriber counts for the admin panel."""
    verify_admin(token, "email_digest_stats", request)
    from services.email_digest_service import get_stats
    return get_stats()

@router.post("/admin/email/send-now")
def email_digest_send_now(token: str, request: Request):
    """Manual on-demand send -- bypasses the daily job's once-per-day
    idempotency guard, same rationale as /admin/content-variants/
    regenerate above (e.g. testing this feature, or resending after
    fixing a bad subject line)."""
    verify_admin(token, "email_digest_send_now", request)
    from api.market_pulse import _compute_free_signals
    from services.content_repurpose_service import generate_content_variants
    from services.email_digest_service import send_daily_digest
    cache = _compute_free_signals()
    variants = generate_content_variants(cache)
    if not variants.get("available"):
        raise HTTPException(status_code=400, detail="No signals available today")
    result = send_daily_digest(variants["email_subject"], variants["email_body"])
    return {"status": "ok", **result}

@router.get("/admin/widgets/stats")
def widget_stats(token: str, request: Request):
    """Growth OS Phase 4 -- today's + all-time embed-view counts per
    widget type, from api/widgets.py's widget_embed_log table (created
    lazily on first embed load, so this tolerates the table not existing
    yet on a fresh deploy)."""
    verify_admin(token, "widget_stats", request)
    conn = get_db()
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        rows = conn.execute(
            "SELECT widget_type, SUM(views) as total, "
            "SUM(CASE WHEN log_date = ? THEN views ELSE 0 END) as today "
            "FROM widget_embed_log GROUP BY widget_type",
            (today,),
        ).fetchall()
        return {"widgets": [{"widget_type": r["widget_type"], "total_views": r["total"], "today_views": r["today"]} for r in rows]}
    except Exception:
        return {"widgets": []}
    finally:
        conn.close()

@router.get("/admin/intelligence/usage")
def intelligence_usage(token: str, request: Request):
    """Growth OS Phase 5 (2026-08-04) -- devrel view over the Intelligence
    API's real usage, joining three tables that were each built for a
    narrower purpose and never had a combined admin view before:
    api_keys (admin-issued), self_serve_api_keys (automated free-tier
    signup, services/api_key_service.py), and intelligence_api_usage
    (per-key per-day weighted-call counter, services/
    intelligence_quota_service.py). No new table -- purely a read-side
    join for visibility, same "packaging, not new infrastructure" posture
    as api/intelligence.py itself.

    Per-key `last_used_at` and issuance metadata come from whichever of
    the two key tables issued it; usage counts come from
    intelligence_api_usage keyed by the raw key string, matching how
    api/intelligence.py's _check_and_spend_quota() already writes to it.

    2026-09-07 (security review: hash keys at rest -- see services/
    api_key_service.py's verify_key() docstring for the full story):
    api_keys.key/self_serve_api_keys.key now hold a HASH for every
    migrated/freshly-issued row, not the raw key -- so this view's join
    against intelligence_api_usage (still raw-key-keyed; that table is
    an ephemeral per-day counter, not a persistent credential store, and
    was deliberately left out of the hashing migration) can no longer
    match on the key value directly. Fixed by hashing each usage row's
    raw api_key here instead, so the join happens hash-to-hash --
    intelligence_api_usage itself is untouched, this endpoint just reads
    it differently."""
    verify_admin(token, "intelligence_usage", request)
    conn = get_db()
    try:
        import hashlib
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Usage totals per key, computed once and looked up by key_hash
        # below rather than re-querying per row.
        usage_rows = conn.execute(
            "SELECT api_key, SUM(count) as total, "
            "SUM(CASE WHEN date = ? THEN count ELSE 0 END) as today "
            "FROM intelligence_api_usage GROUP BY api_key",
            (today,),
        ).fetchall()
        usage_by_key_hash = {
            hashlib.sha256(r["api_key"].encode("utf-8")).hexdigest(): {"today_calls": r["today"] or 0, "total_calls": r["total"] or 0}
            for r in usage_rows
        }

        def _preview_and_hash(key_col: str, key_hash_col, key_preview_col):
            # key_hash_col/key_preview_col are set for every migrated/
            # freshly-issued row; the key_col fallback only fires for a
            # legacy row that's never been verified even once since the
            # hash migration shipped (key_col still holds real plaintext
            # then, same as list_keys_for_email()'s equivalent fallback).
            key_hash = key_hash_col or hashlib.sha256(key_col.encode("utf-8")).hexdigest()
            preview = key_preview_col or (key_col[:8] + "..." + key_col[-4:])
            return preview, key_hash

        keys = []
        try:
            admin_rows = conn.execute(
                "SELECT ak.key as key, ak.key_hash as key_hash, ak.key_preview as key_preview, "
                "ak.tier as tier, ak.active as active, "
                "ak.created_at as created_at, ak.last_used_at as last_used_at, "
                "u.email as email "
                "FROM api_keys ak LEFT JOIN users u ON u.id = ak.user_id"
            ).fetchall()
            for r in admin_rows:
                preview, key_hash = _preview_and_hash(r["key"], r["key_hash"], r["key_preview"])
                keys.append({
                    "source": "admin_issued",
                    "email": r["email"],
                    "tier": r["tier"],
                    "active": bool(r["active"]),
                    "created_at": r["created_at"],
                    "last_used_at": r["last_used_at"],
                    "key_preview": preview,
                    **usage_by_key_hash.get(key_hash, {"today_calls": 0, "total_calls": 0}),
                })
        except Exception:
            pass  # api_keys table not present yet on a fresh deploy

        try:
            self_serve_rows = conn.execute(
                "SELECT key, key_hash, key_preview, email, tier, active, created_at, last_used_at "
                "FROM self_serve_api_keys"
            ).fetchall()
            for r in self_serve_rows:
                preview, key_hash = _preview_and_hash(r["key"], r["key_hash"], r["key_preview"])
                keys.append({
                    "source": "self_serve",
                    "email": r["email"],
                    "tier": r["tier"],
                    "active": bool(r["active"]),
                    "created_at": r["created_at"],
                    "last_used_at": r["last_used_at"],
                    "key_preview": preview,
                    **usage_by_key_hash.get(key_hash, {"today_calls": 0, "total_calls": 0}),
                })
        except Exception:
            pass  # self_serve_api_keys table not present yet on a fresh deploy

        # Sort most-active-today first -- the devrel-relevant ordering for
        # "who's actually using this right now".
        keys.sort(key=lambda k: k["today_calls"], reverse=True)

        overview = {
            "total_keys": len(keys),
            "active_keys": sum(1 for k in keys if k["active"]),
            "calls_today": sum(k["today_calls"] for k in keys),
            "calls_all_time": sum(k["total_calls"] for k in keys),
        }
        return {"overview": overview, "keys": keys}
    except Exception:
        return {"overview": {"total_keys": 0, "active_keys": 0, "calls_today": 0, "calls_all_time": 0}, "keys": []}
    finally:
        conn.close()

@router.get("/admin/referral/stats")
def referral_stats(token: str, request: Request):
    """Growth OS Phase 6 (2026-08-04) -- referral stats dashboard: total
    codes generated, total successful referrals, total paid (annual-Pro)
    conversions, and a top-referrers leaderboard. Also returns the
    current reward-amount config so the admin panel can render the
    overview and the editable reward form from one call."""
    verify_admin(token, "referral_stats", request)
    from services.referral_service import ReferralService
    return ReferralService.get_admin_dashboard()

@router.post("/admin/referral/config/{key}")
def set_referral_config(key: str, value: int, token: str = None, request: Request = None):
    """Growth OS Phase 6 -- toggle one referral reward amount (points
    bonuses, Pro-grant days, or the Pro+ threshold) live, no redeploy.
    `value` is a plain int query/body param (FastAPI accepts it as a
    query param here, matching this file's existing convention for
    simple scalar admin actions -- see issue_key()/mark_annual_pro()
    above). Validated against the known key whitelist in
    services/referral_service.py's set_config()."""
    verify_admin(token, f"set_referral_config:{key}", request)
    from services.referral_service import set_config
    result = set_config(key, value)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Invalid config key"))
    return result

@router.post("/admin/referral/floating-basic/run")
def run_floating_basic_grants_now(token: str, request: Request):
    """2026-08-24 -- manual "Run Now" trigger for the floating-Basic
    referral cron (backend/main.py's _run_referral_floating_basic_job,
    normally 22:15 daily), same purpose as /admin/security-scan/run
    above: lets AJ verify the mechanism works right after wiring a new
    referrer/referred-payment scenario instead of waiting for the next
    scheduled run."""
    verify_admin(token, "run_floating_basic_grants", request)
    from services.referral_service import run_floating_basic_grants
    return run_floating_basic_grants()

@router.get("/admin/video/status")
def video_engine_status(token: str, request: Request):
    """Growth OS Phase 7 -- admin-panel status check: is GOOGLE_TTS_API_KEY
    set, is ffmpeg present on this deploy, and metadata (status/duration/
    slide count) for the most recent generation attempt (success or
    failure), from services/video_engine_service.py's video_generation_log
    table. Always returns a usable dict even on a fresh deploy with no
    video ever generated."""
    verify_admin(token, "video_engine_status", request)
    from services.video_engine_service import get_status
    return get_status()

def _youtube_video_metadata(lang: str, topic: str = None) -> dict:
    """2026-08-09: shared title/description builder for the YouTube
    auto-upload option on both video endpoints below. Description always
    carries the same non-advice disclaimer + AI-disclosure link the rest
    of the site uses (see ai-disclaimer.html, tasks #653/#679-686) --
    YouTube descriptions are public-facing content just like any other
    XFINLAB output, so they get the same compliance floor."""
    if topic:
        title = f"XFINLAB: {topic.strip()[:80]}"
    else:
        from datetime import datetime, timezone
        title = f"XFINLAB Daily Market Update - {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    description = (
        (f"{topic.strip()}\n\n" if topic else "")
        + "AI-generated market commentary from XFINLAB (xfinlab.com). "
        "General information only, not investment advice. See "
        "https://www.xfinlab.com/ai-disclaimer.html and "
        "https://www.xfinlab.com/risk-warning.html for details."
    )
    return {"title": title, "description": description, "tags": ["XFINLAB", "market analysis", "AI"]}


@router.post("/admin/video/generate")
def video_engine_generate(token: str, lang: str = "zh-HK", aspect_ratio: str = "9:16",
                           theme: str = "dark", post_to_telegram: bool = False,
                           upload_to_youtube: bool = False, request: Request = None):
    """Growth OS Phase 7 -- on-demand "Generate Now" trigger for the admin
    panel, so this can be tested/verified before wiring any automatic
    daily schedule. Gated by the video_engine flag (default OFF) on top
    of the admin-token check -- this is the heaviest single operation in
    Growth OS (TTS calls + ffmpeg render), so it must never run from a
    stale/cached admin.html session if the feature's been intentionally
    turned off. Runs synchronously and returns the result dict as-is
    (including a graceful {"available": False, "message": ...} if TTS or
    ffmpeg isn't actually configured on this deploy).

    2026-08-04 v2: lang/aspect_ratio/theme are now real choices (see
    services/video_engine_service.py's _SCRIPT/_ASPECT_RATIOS/_THEMES).
    post_to_telegram defaults to False and is an explicit per-click admin
    opt-in, not automatic -- repeatedly clicking Generate Now while
    testing shouldn't quietly spam a live public channel every time;
    the admin has to tick the box each time they actually want that."""
    verify_admin(token, "video_engine_generate", request)
    flags = {r["key"]: r["enabled"] for r in get_db().execute(
        "SELECT key, enabled FROM feature_flags WHERE key='video_engine'"
    ).fetchall()}
    if not flags.get("video_engine", 0):
        raise HTTPException(status_code=403, detail="video_engine feature flag is off")
    from services.video_engine_service import generate_daily_video
    result = generate_daily_video(lang=lang, aspect_ratio=aspect_ratio, theme=theme)
    if post_to_telegram and result.get("available"):
        try:
            # 2026-08-08 fix: used to hard-code caption="XFINLAB Daily AI
            # Market Signal" for every language -- non-compliant wording
            # ("Signal") and never actually varied by lang. Let
            # push_video_to_telegram fall back to its own lang-aware,
            # compliant default (services/telegram_push_service.video_caption)
            # instead of passing a caption here at all.
            from services.telegram_push_service import push_video_to_telegram
            result["telegram_posted"] = push_video_to_telegram(lang, result["path"])
        except Exception:
            result["telegram_posted"] = False
    if upload_to_youtube and result.get("available"):
        try:
            from services.youtube_upload_service import upload_video
            meta = _youtube_video_metadata(lang)
            result["youtube"] = upload_video(result["path"], meta["title"], meta["description"], meta["tags"])
        except Exception as e:
            result["youtube"] = {"available": False, "message": str(e)}
    return result

@router.post("/admin/video/generate-custom")
async def video_engine_generate_custom(token: str, request: Request, body: dict = {}):
    """2026-08-09 (admin chat-to-video feature, requested as "Video Engine
    可以加個CHAT更彈性做任何影片嗎" -- confirmed admin-only, not public):
    sibling to /admin/video/generate above, but instead of the fixed
    today's-signals format, takes a free-text `prompt` (e.g. "make a video
    about NVDA earnings, in Spanish, square format") and generates
    narration/slides for that arbitrary topic via
    services/video_engine_service.generate_custom_video(). Same
    admin-token + video_engine feature-flag gating as the daily-video
    endpoint -- this is still the heaviest single operation in Growth OS
    (TTS + ffmpeg), a chat text box doesn't change that cost.

    Uses a JSON body (not query params) since `prompt` is free text that
    can run long, unlike the daily endpoint's short enum-like query params."""
    verify_admin(token, "video_engine_generate_custom", request)
    flags = {r["key"]: r["enabled"] for r in get_db().execute(
        "SELECT key, enabled FROM feature_flags WHERE key='video_engine'"
    ).fetchall()}
    if not flags.get("video_engine", 0):
        raise HTTPException(status_code=403, detail="video_engine feature flag is off")
    prompt = (body or {}).get("prompt", "")
    num_slides = int((body or {}).get("num_slides", 4) or 4)
    post_to_telegram = bool((body or {}).get("post_to_telegram", False))
    upload_to_youtube = bool((body or {}).get("upload_to_youtube", False))
    # 2026-08-13: optional explicit language override from the admin's
    # new dropdown -- empty string / "auto" / omitted all mean "let
    # parse_video_chat_request() keep guessing from the prompt text", so
    # only pass through a real value.
    lang_override = (body or {}).get("lang") or None
    from services.video_engine_service import generate_custom_video
    result = generate_custom_video(prompt, num_slides=num_slides, lang_override=lang_override)
    if post_to_telegram and result.get("available"):
        try:
            from services.telegram_push_service import push_video_to_telegram
            result["telegram_posted"] = push_video_to_telegram(result.get("lang", "en"), result["path"])
        except Exception:
            result["telegram_posted"] = False
    if upload_to_youtube and result.get("available"):
        try:
            from services.youtube_upload_service import upload_video
            meta = _youtube_video_metadata(result.get("lang", "en"), topic=result.get("topic"))
            result["youtube"] = upload_video(result["path"], meta["title"], meta["description"], meta["tags"])
        except Exception as e:
            result["youtube"] = {"available": False, "message": str(e)}
    return result

@router.post("/admin/widgets/branding")
def set_widget_branding(token: str, request: Request, body: dict = {}):
    """2026-08-09 (white-label Tier A, XFINLAB_Final_Strategy.md P2/P3):
    admin-only setter for a Pro/Enterprise client's embed-widget branding
    (services/widget_branding_service.py). No self-serve UI yet -- same
    "admin sets it up per paying client" convention as
    /intelligence/admin/issue-key, since Pro/Enterprise billing itself is
    still manual. body: {api_key, brand_name, accent_color, logo_url,
    badge_mode}. badge_mode='hidden' (fully remove the XFINLAB badge) is
    rejected unless the key's tier is 'enterprise' -- the service layer
    itself enforces this, this endpoint is just the auth gate."""
    verify_admin(token, "set_widget_branding", request)
    from services.widget_branding_service import set_branding

    body = body or {}
    result = set_branding(
        api_key=body.get("api_key"),
        brand_name=body.get("brand_name"),
        accent_color=body.get("accent_color"),
        logo_url=body.get("logo_url"),
        badge_mode=body.get("badge_mode", "default"),
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Failed to set branding"))
    return result


@router.delete("/admin/users/{user_id}")
def delete_user(user_id: int, token: str, request: Request):
    verify_admin(token, f"delete_user:{user_id}", request)
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"User {user_id} deleted"}

@router.post("/admin/push/telegram")
async def push_telegram(token: str, request: Request, body: dict = {}):
    channel = body.get("channel", "en")
    verify_admin(token, f"push_telegram:{channel}", request)
    try:
        import subprocess
        import sys
        scripts = {
            "en": "growth/channel_push.py",
            "zh": "growth/channel_push_zh.py",
            "es": "growth/channel_push_es.py"
        }
        script = scripts.get(channel, scripts["en"])
        # Was hardcoded to a local Mac path (/Users/aj/... + a specific
        # python3.9 binary) that only exists on the dev machine -- this
        # would fail silently in Railway's container. Use sys.executable
        # (whatever interpreter is actually running this app) and resolve
        # the script path relative to the repo root, same pattern as
        # DB_PATH elsewhere in this file.
        repo_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        script_path = os.path.join(repo_root, script)
        subprocess.Popen([sys.executable, script_path])
        return {"status": "ok", "message": f"Pushing to {channel} channel"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/admin/audit-logs")
def get_audit_logs(token: str, request: Request, limit: int = 100):
    """
    Security & Operations Layer, Phase 2 -- surfaces the audit_logs table
    (login/register/admin actions, written by services/audit_log_service.py)
    in the admin dashboard. Capped at 200 to keep the response light.
    """
    verify_admin(token, "get_audit_logs", request)
    return {"logs": get_recent_logs(limit=min(limit, 200))}

@router.get("/admin/feature-flags")
def get_feature_flags(token: str, request: Request):
    verify_admin(token, "get_feature_flags", request)
    conn = get_db()
    rows = conn.execute("SELECT key, enabled FROM feature_flags").fetchall()
    conn.close()
    return {"flags": {r["key"]: bool(r["enabled"]) for r in rows}}

@router.post("/admin/feature-flags/{key}")
def set_feature_flag(key: str, token: str, request: Request, body: dict = {}):
    verify_admin(token, f"set_feature_flag:{key}", request)
    if key not in _DEFAULT_FLAGS:
        raise HTTPException(status_code=404, detail=f"Unknown flag: {key}")
    enabled = bool(body.get("enabled", True))
    conn = get_db()
    conn.execute(
        "UPDATE feature_flags SET enabled = ?, updated_at = datetime('now') WHERE key = ?",
        (1 if enabled else 0, key),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "key": key, "enabled": enabled}


@router.get("/auth/login-methods")
def get_login_methods():
    """
    Public (no admin token) -- login.html calls this on load to decide
    which social/OTP login buttons to render (task #333). Only ever
    exposes the 3 boolean flags plus Google's public client_id (which is
    NOT a secret -- Google's own docs have every "Sign in with Google"
    web integration embed it directly in page HTML/JS). Never exposes
    GOOGLE_CLIENT_SECRET, LINE_CHANNEL_SECRET, or WHATSAPP_ACCESS_TOKEN.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT key, enabled FROM feature_flags WHERE key IN ('google_login','line_login','whatsapp_otp')"
    ).fetchall()
    conn.close()
    flags = {r["key"]: bool(r["enabled"]) for r in rows}
    return {
        "google_login": flags.get("google_login", False),
        "line_login": flags.get("line_login", False),
        "whatsapp_otp": flags.get("whatsapp_otp", False),
        "google_client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
    }


@router.get("/admin/prediction-ledger")
def get_prediction_ledger(token: str, request: Request, symbol: str = None, source: str = None):
    """
    P1 of the Quant Research Factory roadmap (2026-08-10): surfaces
    services/prediction_ledger_service.py's accuracy scoreboard --
    hit_rate_pct / avg_brier_score over every GRADED prediction, plus
    the most recent 50 rows (graded and pending) for spot-checking.
    Read-only, does not trigger a fresh grading pass (that runs on its
    own daily schedule via backend/main.py's APScheduler job) -- same
    fast/read-only pattern as get_security_scan() above.
    2026-08-24: added `source` (e.g. "capital_flow_forecast" vs the
    default "direction_probability_service") now that more than one
    engine logs into this same table, so each can be scored on its own
    -- e.g. /admin/prediction-ledger?source=capital_flow_forecast to
    pull the real hit-rate/Brier numbers for a Capital Flow landing page.
    """
    verify_admin(token, "get_prediction_ledger", request)
    from services.prediction_ledger_service import get_ledger_stats, get_recent_predictions
    return {
        "status": "ok",
        "stats": get_ledger_stats(symbol=symbol, source=source),
        "recent": get_recent_predictions(limit=50, symbol=symbol, source=source),
    }


@router.get("/admin/stripe-account-status")
def get_stripe_account_status(token: str, request: Request):
    """
    2026-08-24 (AJ: "點知面家個STRIPE係咪完全可收款提款無問題"): live check
    against Stripe's own Account.retrieve() -- see
    api/webhooks_stripe.py's get_account_status() for the full honesty
    note on why this is a separate call from the public /stripe/status
    (that one only checks env vars, this one asks Stripe directly
    whether the account can actually accept charges and pay out).
    """
    verify_admin(token, "get_stripe_account_status", request)
    from api.webhooks_stripe import get_account_status
    return get_account_status()


@router.get("/admin/fred-diagnostic")
def get_fred_diagnostic(token: str, request: Request):
    """
    2026-08-26 (debugging the persistent RRPONTSYD/M2SL/WALCL HTTP 400
    surfaced by the new Data Factory error tracking): AJ manually
    replicated the exact same request (same series_id, same api_key,
    same params) via his own browser and it succeeded -- meaning the
    request shape and the key value HE tested are both fine. Re-pasting
    the key into Railway's env var didn't clear the error either. This
    endpoint runs the IDENTICAL request server-side, from inside the
    actual Railway process, and returns FRED's real response instead of
    another layer of guessing. Never echoes the key itself -- only its
    length and whether it has leading/trailing whitespace (a very common
    real cause of this exact symptom: an invisible copy-paste artifact
    that works fine when a browser trims it but not when sent raw over
    HTTP), plus FRED's actual status code and response body for the
    RRPONTSYD call so we can see FRED's own error message text.
    """
    verify_admin(token, "get_fred_diagnostic", request)
    import os
    from services.outbound_http import get_with_backoff

    raw_key = os.getenv("FRED_API_KEY")
    key_info = {
        "key_set": bool(raw_key),
        "key_length": len(raw_key) if raw_key else 0,
        "has_leading_whitespace": bool(raw_key and raw_key != raw_key.lstrip()),
        "has_trailing_whitespace": bool(raw_key and raw_key != raw_key.rstrip()),
        "has_quote_chars": bool(raw_key and ('"' in raw_key or "'" in raw_key)),
    }

    live_test = {"attempted": False}
    if raw_key:
        try:
            res = get_with_backoff(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": "RRPONTSYD",
                    "api_key": raw_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 30,
                },
                timeout=10,
            )
            live_test = {
                "attempted": True,
                "status_code": res.status_code,
                "response_body": res.text[:1000],
            }
        except Exception as e:
            live_test = {"attempted": True, "error": str(e)}

    return {"key_info": key_info, "live_test": live_test}


@router.get("/admin/sec-13d13g-search")
def get_sec_13d13g_search(token: str, request: Request, ticker: str, force_refresh: bool = False):
    """
    2026-08-26 (Data Factory Step 6): admin-facing trigger + view for
    services/sec_13d_13g_service.py's on-demand per-ticker lookup.
    """
    verify_admin(token, f"sec_13d13g_search:{ticker}", request)
    from services.sec_13d_13g_service import search_recent_filings
    return search_recent_filings(ticker, force_refresh=force_refresh)


@router.get("/admin/sec-13d13g-debug")
def get_sec_13d13g_debug(token: str, request: Request, ticker: str):
    """
    Learned from the 13F info-table filename bug (multiple back-and-forth
    debugging rounds with AJ to pin down): this time, build the raw-
    response diagnostic upfront instead of reactively. Runs the exact
    same EFTS request services.sec_13d_13g_service.search_recent_filings
    makes, but returns SEC's raw JSON response instead of the parsed/
    filtered result, so a field-name mismatch in _extract_hit_fields can
    be spotted in one round-trip.
    """
    verify_admin(token, f"sec_13d13g_debug:{ticker}", request)
    from services.sec_13d_13g_service import (
        _load_ticker_title_map, _build_search_phrase, EFTS_SEARCH_URL, SEC_USER_AGENT, _LOOKBACK_DAYS,
    )
    from services.outbound_http import get_with_backoff
    from datetime import date, timedelta

    ticker = ticker.upper().strip()
    title_map = _load_ticker_title_map()
    title = title_map.get(ticker)
    if not title:
        return {"error": f"{ticker} not found in SEC ticker map", "ticker_map_size": len(title_map)}

    end_dt = date.today()
    start_dt = end_dt - timedelta(days=_LOOKBACK_DAYS)
    search_phrase = _build_search_phrase(title)
    params = {
        "q": f'"{search_phrase}"', "forms": "SC 13D,SC 13G", "dateRange": "custom",
        "startdt": start_dt.isoformat(), "enddt": end_dt.isoformat(), "size": 20,
    }
    try:
        res = get_with_backoff(EFTS_SEARCH_URL, params=params, headers={"User-Agent": SEC_USER_AGENT}, timeout=20)
        return {
            "resolved_title": title,
            "search_phrase_used": search_phrase,
            "params": params,
            "status_code": res.status_code,
            "raw_response": res.text[:3000],
        }
    except Exception as e:
        return {"resolved_title": title, "search_phrase_used": search_phrase, "params": params, "error": str(e)}


@router.get("/admin/api-trending-tickers")
def get_api_trending_tickers(token: str, request: Request, days: int = 7, limit: int = 20):
    """
    2026-08-25 (AJ: "咁FREE KEY比人用，我接到數據訓ENGINE或儲存之類嗎"): the
    honest answer at the time was "no signal at all" -- intelligence_api_
    usage only ever counted raw call volume. services/intelligence_quota_
    service.py's log_query()/get_trending_tickers() close that gap with a
    lightweight, aggregate-only query log (endpoint+ticker+timestamp, never
    the response payload) -- this is the admin-facing read of it. Answers
    "what are developers actually asking the API about" (a product signal
    on which tickers/features to prioritize), not "was the model right"
    (that requires the Prediction Ledger's own internal grading loop, a
    different question this endpoint deliberately doesn't claim to answer).
    """
    verify_admin(token, "get_api_trending_tickers", request)
    from services.intelligence_quota_service import get_trending_tickers
    return {"days": days, "trending": get_trending_tickers(days=days, limit=limit)}


@router.get("/admin/data-sources")
def get_data_sources(token: str, request: Request):
    """
    2026-08-26 (AJ's "Data Factory" batch, "起啦"): foundation layer for
    an auto-extensible set of external data collectors (FRED, SEC EDGAR
    ownership, CFTC COT, crypto exchanges, etc.). Unlike /admin/feature-
    flags (which needs every key hardcoded into _DEFAULT_FLAGS before it
    can be toggled), each collector self-registers via
    services.data_source_registry.register_source() the moment its module
    is imported -- this endpoint just reads whatever has registered so
    far, so a brand-new collector shows up here automatically with no
    admin.py edit required.
    """
    verify_admin(token, "get_data_sources", request)
    from services.data_source_registry import list_sources
    return {"sources": list_sources()}


@router.post("/admin/data-sources/{source_key}/toggle")
def toggle_data_source(source_key: str, token: str, request: Request, enabled: bool = True):
    verify_admin(token, f"toggle_data_source:{source_key}", request)
    from services.data_source_registry import set_source_enabled
    ok = set_source_enabled(source_key, enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="Unknown source_key")
    return {"success": True, "source_key": source_key, "enabled": enabled}


@router.get("/admin/cftc-cot-snapshot")
def get_cftc_cot_snapshot(token: str, request: Request):
    """
    2026-08-26 (Data Factory Step 3): debug/visibility endpoint for the
    new services/cftc_cot_service.py collector -- lets AJ actually see
    the positioning numbers being collected (net long/short per
    contract), not just the run/error counts the Data Factory panel
    shows. Same pattern as /admin/api-trending-tickers: a thin read-only
    wrapper, no new business logic here.
    """
    verify_admin(token, "get_cftc_cot_snapshot", request)
    from services.cftc_cot_service import get_snapshot
    return get_snapshot()


@router.get("/admin/sec-13f-holdings")
def get_sec_13f_holdings(token: str, request: Request, cik: int = None):
    """
    2026-08-26 (Data Factory Step 4): visibility endpoint for
    services/sec_ownership_service.py. Without a cik param, lists the
    watched-filer roster; with one, returns that filer's latest parsed
    13F holdings.
    """
    verify_admin(token, "get_sec_13f_holdings", request)
    from services.sec_ownership_service import list_watched_filers, get_latest_holdings
    if cik:
        return {"cik": cik, "holdings": get_latest_holdings(cik)}
    return {"watched_filers": list_watched_filers()}


@router.get("/admin/sec-13f-debug")
def get_sec_13f_debug(token: str, request: Request, cik: int):
    """
    2026-08-26 (debugging Berkshire's 0-holdings result -- confirmed
    reproducible: 3 manual refreshes, Berkshire fails all 3 times while
    Pershing Square/Scion succeed every time, so this is specific to
    Berkshire's filing, not a flaky network issue). Runs the exact same
    3-step pipeline services.sec_ownership_service.refresh_filer() uses
    (submissions.json -> filing index.json -> info table XML), but
    returns each intermediate result instead of just persisting/failing
    silently, so we can see exactly which step breaks for this CIK.
    """
    verify_admin(token, "get_sec_13f_debug", request)
    from services.sec_ownership_service import (
        _find_latest_13f_filing, _find_infotable_filename, _fetch_json,
        SEC_USER_AGENT, FILING_DOC_URL, FILING_INDEX_URL,
    )
    from services.outbound_http import get_with_backoff

    result = {"cik": cik, "step": "find_latest_13f_filing"}
    try:
        filing = _find_latest_13f_filing(cik)
        result["filing"] = filing
        if not filing:
            result["conclusion"] = "No 13F-HR (exact form type match) found in this CIK's recent filings list."
            return result
    except Exception as e:
        result["error"] = str(e)
        return result

    result["step"] = "find_infotable_filename"
    try:
        filename = _find_infotable_filename(cik, filing["accession_nodash"])
        result["infotable_filename"] = filename
        if not filename:
            result["conclusion"] = "All 3 detection strategies failed -- showing raw directory listing for manual inspection."
            try:
                idx = _fetch_json(FILING_INDEX_URL.format(cik=cik, accession_nodash=filing["accession_nodash"]))
                items = ((idx.get("directory") or {}).get("item")) or []
                result["directory_listing"] = [{"name": i.get("name"), "type": i.get("type")} for i in items]
            except Exception as e:
                result["directory_listing_error"] = str(e)
            return result
    except Exception as e:
        result["error"] = str(e)
        return result

    result["step"] = "fetch_infotable_document"
    try:
        doc_url = FILING_DOC_URL.format(cik=cik, accession_nodash=filing["accession_nodash"], filename=filename)
        res = get_with_backoff(doc_url, headers={"User-Agent": SEC_USER_AGENT}, timeout=30)
        result["doc_url"] = doc_url
        result["status_code"] = res.status_code
        result["body_preview"] = res.text[:500]
        result["conclusion"] = "Reached the final fetch step -- check status_code/body_preview above."
    except Exception as e:
        result["error"] = str(e)
    return result


@router.post("/admin/sec-13f-refresh")
def refresh_sec_13f(token: str, request: Request):
    """Manual trigger -- 13F filings only update quarterly so there's no
    scheduled job for this yet (see sec_ownership_service.refresh_all's
    docstring); this lets AJ kick off a real run and immediately check
    the Data Factory panel's run/error/last_success fields for
    'sec_13f_ownership' to confirm the live SEC round trip actually
    works from Railway (the one thing not verifiable from the sandboxed
    dev environment this was built in)."""
    verify_admin(token, "refresh_sec_13f", request)
    from services.sec_ownership_service import refresh_all
    return {"results": refresh_all()}


@router.get("/admin/binance-exchange-snapshot")
def get_binance_exchange_snapshot(token: str, request: Request):
    """
    2026-08-26 (Data Factory Step 7): debug/visibility endpoint for
    services/crypto_exchange_service.py, same pattern as
    /admin/cftc-cot-snapshot.
    """
    verify_admin(token, "get_binance_exchange_snapshot", request)
    from services.crypto_exchange_service import get_all_tickers
    return {"tickers": get_all_tickers()}


@router.get("/admin/eia-energy-snapshot")
def get_eia_energy_snapshot(token: str, request: Request):
    """
    2026-08-27 (Data Factory Step 8a, AJ: "一次過全加可以嗎"): debug/
    visibility endpoint for services/eia_energy_service.py, same pattern
    as /admin/cftc-cot-snapshot.
    """
    verify_admin(token, "get_eia_energy_snapshot", request)
    from services.eia_energy_service import get_snapshot, is_available, EIA_API_KEY_ENV
    if not is_available():
        return {"available": False, "message": f"{EIA_API_KEY_ENV} not set on this environment."}
    return get_snapshot()


@router.get("/admin/eia-energy-debug")
def get_eia_energy_debug(token: str, request: Request):
    """
    Upfront diagnostic (same lesson learned from the SEC 13F/13D-13G
    debugging sagas -- build this BEFORE a live bug is reported, not
    after) since the sandbox can't reach api.eia.gov directly. Runs one
    live request per configured series and returns the raw HTTP status +
    parsed row count for each, so if EIA has reorganized a route/series
    ID since this was written, the exact failing route/series shows up
    here immediately instead of a generic 'zero observations' error.
    """
    verify_admin(token, "get_eia_energy_debug", request)
    import os
    from services.eia_energy_service import _SERIES, EIA_BASE_URL, EIA_API_KEY_ENV
    from services.outbound_http import get_with_backoff

    key_set = bool(os.getenv(EIA_API_KEY_ENV))
    results = {}
    for series_key, meta in _SERIES.items():
        url = f"{EIA_BASE_URL}/{meta['route']}/data/"
        params = {
            "api_key": os.getenv(EIA_API_KEY_ENV, ""),
            "frequency": meta["frequency"],
            "data[0]": "value",
            "facets[series][]": meta["series_id"],
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "offset": 0,
            "length": 1,
        }
        try:
            res = get_with_backoff(url, params=params, timeout=15)
            entry = {"route": meta["route"], "series_id": meta["series_id"], "status_code": res.status_code}
            if res.status_code == 200:
                payload = res.json()
                rows = (payload.get("response") or {}).get("data") or []
                entry["row_count"] = len(rows)
                entry["sample_row"] = rows[0] if rows else None
            else:
                entry["body_snippet"] = res.text[:300]
            results[series_key] = entry
        except Exception as e:
            results[series_key] = {"route": meta["route"], "series_id": meta["series_id"], "error": str(e)}
    return {"key_set": key_set, "results": results}


@router.get("/admin/treasury-fiscal-snapshot")
def get_treasury_fiscal_snapshot(token: str, request: Request):
    """
    2026-08-27 (Data Factory Step 8b): debug/visibility endpoint for
    services/treasury_fiscal_service.py, same pattern as
    /admin/cftc-cot-snapshot. No API key gate -- Treasury's Fiscal Data
    API is open with no key required.
    """
    verify_admin(token, "get_treasury_fiscal_snapshot", request)
    from services.treasury_fiscal_service import get_snapshot
    return get_snapshot()


@router.get("/admin/treasury-fiscal-debug")
def get_treasury_fiscal_debug(token: str, request: Request):
    """Same upfront-diagnostic rationale as /admin/eia-energy-debug --
    one live request per configured Treasury dataset, raw status +
    row count, so a field-name or route drift surfaces immediately."""
    verify_admin(token, "get_treasury_fiscal_debug", request)
    from services.treasury_fiscal_service import _SERIES, TREASURY_BASE_URL
    from services.outbound_http import get_with_backoff

    results = {}
    for series_key, meta in _SERIES.items():
        value_fields = meta["value_field"] if isinstance(meta["value_field"], list) else [meta["value_field"]]
        url = f"{TREASURY_BASE_URL}/{meta['path']}"
        params = {
            "fields": f"{meta['date_field']},{','.join(value_fields)}",
            "sort": f"-{meta['date_field']}",
            "page[size]": 5,  # a few rows, not just 1 -- so a null-on-latest-day case (like TGA's close_today_bal) is visible instead of looking like a hard failure
        }
        if meta["extra_filter"]:
            params["filter"] = meta["extra_filter"]
        try:
            res = get_with_backoff(url, params=params, timeout=15)
            entry = {"path": meta["path"], "status_code": res.status_code}
            if res.status_code == 200:
                payload = res.json()
                rows = payload.get("data") or []
                entry["row_count"] = len(rows)
                entry["sample_rows"] = rows[:5]
            else:
                entry["body_snippet"] = res.text[:300]
            results[series_key] = entry
        except Exception as e:
            results[series_key] = {"path": meta["path"], "error": str(e)}
    return {"results": results}


@router.get("/admin/sec-xbrl-debug")
def get_sec_xbrl_debug(token: str, request: Request, ticker: str = "AAPL"):
    """
    2026-08-28 (Data Factory batch, AJ: "咁你一次過起"): debug/visibility
    endpoint for services/sec_xbrl_service.py -- returns the full parsed
    facts for one ticker (default AAPL) so a tag-alias miss or CIK lookup
    failure is visible immediately, same upfront-diagnostic rationale as
    /admin/eia-energy-debug.
    """
    verify_admin(token, "get_sec_xbrl_debug", request)
    from services.sec_xbrl_service import get_company_facts
    return get_company_facts(ticker) or {"available": False, "ticker": ticker.upper(), "message": "No CIK match or zero usable 10-K concepts."}


@router.get("/admin/cboe-vix-snapshot")
def get_cboe_vix_snapshot(token: str, request: Request):
    """
    2026-08-28 (Data Factory batch): debug/visibility endpoint for
    services/cboe_vix_service.py. No API key required.
    """
    verify_admin(token, "get_cboe_vix_snapshot", request)
    from services.cboe_vix_service import get_snapshot
    return get_snapshot()


@router.get("/admin/cboe-vix-debug")
def get_cboe_vix_debug(token: str, request: Request):
    """Same upfront-diagnostic rationale as /admin/eia-energy-debug --
    one live CSV fetch per index, raw status + parsed row shape, so a
    CBOE column-header drift surfaces immediately."""
    verify_admin(token, "get_cboe_vix_debug", request)
    from services.cboe_vix_service import _INDEX_URLS, _parse_latest_close
    from services.outbound_http import get_with_backoff

    results = {}
    for index_key, (url, label) in _INDEX_URLS.items():
        try:
            res = get_with_backoff(url, timeout=15)
            entry = {"url": url, "status_code": res.status_code}
            if res.status_code == 200:
                parsed = _parse_latest_close(res.text)
                entry["header"] = res.text.splitlines()[0] if res.text else None
                entry["parsed_latest"] = parsed
            else:
                entry["body_snippet"] = res.text[:300]
            results[index_key] = entry
        except Exception as e:
            results[index_key] = {"url": url, "error": str(e)}
    return {"results": results}


@router.get("/admin/fdic-bank-health-snapshot")
def get_fdic_bank_health_snapshot(token: str, request: Request):
    """
    2026-08-28 (Data Factory batch): debug/visibility endpoint for
    services/fdic_banking_service.py. No API key required.
    """
    verify_admin(token, "get_fdic_bank_health_snapshot", request)
    from services.fdic_banking_service import get_snapshot
    return get_snapshot()


@router.get("/admin/usda-agriculture-snapshot")
def get_usda_agriculture_snapshot(token: str, request: Request):
    """
    2026-08-28 (Data Factory batch): debug/visibility endpoint for
    services/usda_agriculture_service.py. Requires USDA_NASS_API_KEY.
    """
    verify_admin(token, "get_usda_agriculture_snapshot", request)
    from services.usda_agriculture_service import get_snapshot, is_available, USDA_API_KEY_ENV
    if not is_available():
        return {"available": False, "message": f"{USDA_API_KEY_ENV} not set on this environment."}
    return get_snapshot()


@router.get("/admin/usda-agriculture-debug")
def get_usda_agriculture_debug(token: str, request: Request):
    """Same upfront-diagnostic rationale as /admin/eia-energy-debug --
    one live request per configured USDA short_desc, raw status + row
    count, so a short_desc drift surfaces immediately."""
    verify_admin(token, "get_usda_agriculture_debug", request)
    import os
    from services.usda_agriculture_service import _SERIES, USDA_BASE_URL, USDA_API_KEY_ENV
    from services.outbound_http import get_with_backoff

    key_set = bool(os.getenv(USDA_API_KEY_ENV))
    results = {}
    for series_key, meta in _SERIES.items():
        params = {
            "key": os.getenv(USDA_API_KEY_ENV, ""),
            "short_desc": meta["short_desc"],
            "agg_level_desc": "NATIONAL",
            "freq_desc": "ANNUAL",
            "format": "JSON",
        }
        try:
            res = get_with_backoff(USDA_BASE_URL, params=params, timeout=20)
            entry = {"short_desc": meta["short_desc"], "status_code": res.status_code}
            if res.status_code == 200:
                payload = res.json()
                rows = payload.get("data") or []
                entry["row_count"] = len(rows)
                entry["sample_row"] = rows[0] if rows else None
            else:
                entry["body_snippet"] = res.text[:300]
            results[series_key] = entry
        except Exception as e:
            results[series_key] = {"short_desc": meta["short_desc"], "error": str(e)}
    return {"key_set": key_set, "results": results}


@router.get("/admin/coinbase-exchange-snapshot")
def get_coinbase_exchange_snapshot(token: str, request: Request):
    """
    2026-08-27 (Data Factory Step 8c): debug/visibility endpoint for
    services/coinbase_exchange_service.py, same pattern as
    /admin/binance-exchange-snapshot.
    """
    verify_admin(token, "get_coinbase_exchange_snapshot", request)
    from services.coinbase_exchange_service import get_all_tickers
    return {"tickers": get_all_tickers()}


@router.get("/admin/sec-form4-snapshot")
def get_sec_form4_snapshot(token: str, request: Request, ticker: str):
    """
    2026-08-27 (Data Factory Step 9a): debug/visibility endpoint for
    services/sec_form4_service.py, same pattern as /admin/cftc-cot-
    snapshot -- takes a ticker since (unlike CFTC/EIA) insider trading
    is inherently per-company, not a fixed small set of contracts.
    """
    verify_admin(token, f"get_sec_form4_snapshot:{ticker}", request)
    from services.sec_form4_service import get_recent_insider_transactions
    return get_recent_insider_transactions(ticker, force_refresh=True)


@router.get("/admin/sec-form4-debug")
def get_sec_form4_debug(token: str, request: Request, ticker: str):
    """
    Upfront diagnostic (same lesson learned from the SEC 13F/13D-13G/EIA
    debugging sagas -- build this BEFORE a live bug is reported): runs
    each step of the Form 4 pipeline separately and returns intermediate
    results, so if EDGAR's browse-edgar Atom feed shape (the one part of
    this pipeline that couldn't be verified from the sandbox) doesn't
    match what the parser expects, the raw feed text is visible here
    instead of just a silent 'no filings' result.
    """
    verify_admin(token, f"get_sec_form4_debug:{ticker}", request)
    from services.sec_form4_service import (
        _load_ticker_cik_map, _list_recent_form4_filings, _find_ownership_xml_filename,
        BROWSE_EDGAR_URL, SEC_USER_AGENT,
    )
    from services.outbound_http import get_with_backoff

    result = {"ticker": ticker.upper(), "step": "resolve_cik"}
    cik_map = _load_ticker_cik_map()
    cik = cik_map.get(ticker.upper())
    result["cik"] = cik
    if not cik:
        result["conclusion"] = "Ticker not found in SEC's company_tickers.json."
        return result

    result["step"] = "fetch_raw_atom_feed"
    try:
        raw_res = get_with_backoff(
            BROWSE_EDGAR_URL,
            params={"action": "getcompany", "CIK": cik, "type": "4", "dateb": "", "owner": "include", "count": 5, "output": "atom"},
            headers={"User-Agent": SEC_USER_AGENT}, timeout=20,
        )
        result["atom_status_code"] = raw_res.status_code
        result["atom_raw_snippet"] = raw_res.text[:2000] if raw_res.status_code == 200 else raw_res.text[:500]
    except Exception as e:
        result["atom_fetch_error"] = str(e)
        return result

    result["step"] = "parse_filings_list"
    try:
        filings = _list_recent_form4_filings(cik)
        result["filings"] = filings
    except Exception as e:
        result["parse_error"] = str(e)
        return result

    if not filings:
        result["conclusion"] = "Atom feed fetched but zero filings parsed out of it -- check atom_raw_snippet above against the parser's expected <entry>/<title>/<id>/<summary> shape."
        return result

    result["step"] = "find_ownership_xml_for_first_filing"
    try:
        fname = _find_ownership_xml_filename(cik, filings[0]["accession_nodash"])
        result["first_filing_ownership_xml_filename"] = fname
        result["conclusion"] = "OK" if fname else "Filing index fetched but no XML file matched any of the 3 detection strategies."
    except Exception as e:
        result["find_xml_error"] = str(e)

    return result


@router.get("/admin/finra-short-interest-snapshot")
def get_finra_short_interest_snapshot(token: str, request: Request, ticker: str):
    """
    2026-08-27 (Data Factory Step 9b): debug/visibility endpoint for
    services/finra_short_interest_service.py, same per-ticker-lookup
    pattern as /admin/sec-form4-snapshot.
    """
    verify_admin(token, f"get_finra_short_interest_snapshot:{ticker}", request)
    from services.finra_short_interest_service import get_short_interest_for_ticker
    return get_short_interest_for_ticker(ticker)


@router.get("/admin/finra-short-interest-debug")
def get_finra_short_interest_debug(token: str, request: Request):
    """
    Upfront diagnostic (same lesson learned this whole session): the one
    part of this collector that couldn't be verified from the sandbox is
    which candidate settlement-date file actually exists on FINRA's CDN
    right now, and whether its column headers match either of the two
    schema generations this parser tries. Returns the raw HTTP status
    for EVERY candidate date tried (not just the first success), plus
    the raw header row and first data row of whichever one worked.
    """
    verify_admin(token, "get_finra_short_interest_debug", request)
    from services.finra_short_interest_service import (
        _candidate_settlement_dates, FINRA_CSV_URL_TEMPLATE,
    )
    from services.outbound_http import get_with_backoff

    attempts = []
    first_success_text = None
    for d in _candidate_settlement_dates():
        url = FINRA_CSV_URL_TEMPLATE.format(date=d.strftime("%Y%m%d"))
        try:
            res = get_with_backoff(url, timeout=30)
            attempts.append({"date": d.isoformat(), "url": url, "status_code": res.status_code})
            if res.status_code == 200 and first_success_text is None:
                first_success_text = res.content.decode("utf-8", errors="replace")
        except Exception as e:
            attempts.append({"date": d.isoformat(), "url": url, "error": str(e)})

    result = {"attempts": attempts}
    if first_success_text:
        lines = first_success_text.splitlines()
        result["header_row"] = lines[0] if lines else None
        result["first_data_row"] = lines[1] if len(lines) > 1 else None
        result["total_lines"] = len(lines)
    else:
        result["conclusion"] = "No candidate settlement date returned HTTP 200 -- check the attempts list above; the URL pattern or settlement-date guessing window may need adjusting."
    return result


@router.get("/admin/email-debug")
def get_email_debug(token: str, request: Request, send_test: bool = False, test_to: str = None):
    """2026-08-27: diagnostic for the "Key was issued but the confirmation
    email failed to send" error AJ hit on the self-serve Intelligence API
    signup flow. services/email_service.py's EmailService.send() only ever
    logs `print(f"Email error: {e}")` to server stdout on failure -- not
    visible from the outside, and this whole SMTP path can't be exercised
    from the sandbox (no route to Railway's outbound network / real SMTP
    creds). This endpoint surfaces exactly which step fails and why,
    without needing Railway CLI log access.

    Step 1 (always runs, no side effect): reports whether each required
    env var is SET (never the actual secret value) plus the non-secret
    SMTP_HOST/SMTP_PORT values, then attempts connect -> starttls -> login
    against the real configured SMTP server, reporting the exact exception
    and which of those three steps it happened at. This alone diagnoses
    the overwhelming majority of "email failed" cases (missing env var,
    wrong host/port, revoked app password, provider blocking the IP) with
    zero risk of actually sending anything.

    Step 2 (opt-in via send_test=true&test_to=you@example.com): only after
    step 1's login succeeds, sends one real test email so AJ can confirm
    delivery end-to-end (not just that auth works) -- e.g. Namecheap/Gmail
    can accept a login but still silently drop mail past that point.

    2026-08-27: now branches on RESEND_API_KEY first (see services/
    email_service.py's module docstring -- Railway blocks outbound SMTP
    below its Pro plan, confirmed via this exact endpoint's `connect:
    "FAILED: timed out"` result against mail.privateemail.com:587). When
    RESEND_API_KEY is set, this checks Resend's HTTP API instead (GET
    /domains, which validates the key without sending anything) and skips
    the SMTP connect/starttls/login walk entirely, since EmailService.send
    itself would do the same -- this endpoint should diagnose whichever
    path send() actually takes, not a path it no longer uses."""
    verify_admin(token, "get_email_debug", request)
    import smtplib

    resend_key = os.getenv("RESEND_API_KEY")
    if resend_key:
        from services.email_service import RESEND_FROM_EMAIL

        result = {
            "active_path": "resend",
            "env": {"RESEND_API_KEY": True, "RESEND_FROM_EMAIL": RESEND_FROM_EMAIL},
        }
        try:
            resp = requests.get(
                "https://api.resend.com/domains",
                headers={"Authorization": f"Bearer {resend_key}"},
                timeout=15,
            )
            result["auth_check"] = {"status_code": resp.status_code}
            if resp.status_code == 200:
                domains = resp.json().get("data", [])
                result["auth_check"]["domains"] = [
                    {"name": d.get("name"), "status": d.get("status")} for d in domains
                ]
                from_domain = RESEND_FROM_EMAIL.split("@")[-1].rstrip(">").strip()
                verified_names = [d.get("name") for d in domains if d.get("status") == "verified"]
                result["conclusion"] = (
                    f"API key valid. RESEND_FROM_EMAIL's domain ({from_domain}) is "
                    + ("verified -- ready to send to any recipient." if from_domain in verified_names
                       else "NOT in the verified list above -- sends will fail or silently fall back to "
                            "onboarding@resend.dev (which can only deliver to your own Resend account email). "
                            "Verify this domain's DNS records in the Resend dashboard.")
                )
            elif resp.status_code == 401 and "restricted_api_key" in resp.text:
                # 2026-08-27: AJ's key returned exactly this -- Resend keys
                # can be scoped to "Sending access" only (vs. "Full
                # access"), and a sending-only key is correctly REJECTED
                # from GET /domains (a full-access-only endpoint) even
                # though it's completely valid for what EmailService.send()
                # actually needs. Don't misreport a scope restriction as an
                # invalid/inactive key -- the real send path can't be
                # verified via this call at all with a sending-only key;
                # send_test is the only way to confirm it end-to-end.
                result["auth_check"]["body"] = resp.text[:500]
                result["conclusion"] = (
                    "API key is valid but scoped to \"Sending access\" only, so Resend correctly "
                    "rejects it from this domain-verification check (a full-access-only endpoint) -- "
                    "this is NOT a broken key. sending emails itself may still work fine. Re-run with "
                    "send_test=true&test_to=you@example.com to confirm actual delivery -- if "
                    f"{RESEND_FROM_EMAIL}'s domain isn't verified in the Resend dashboard yet, that "
                    "test send will fail with a domain-verification error instead."
                )
            else:
                result["conclusion"] = f"Resend API key rejected (HTTP {resp.status_code}) -- check RESEND_API_KEY is correct and active."
                result["auth_check"]["body"] = resp.text[:500]
        except Exception as e:
            result["auth_check"] = {"error": str(e)}
            result["conclusion"] = "Could not reach api.resend.com -- see error above."

        _auth_status = result.get("auth_check", {}).get("status_code")
        _key_likely_sendable = _auth_status == 200 or (
            _auth_status == 401 and "restricted_api_key" in result.get("auth_check", {}).get("body", "")
        )
        if send_test and test_to and _key_likely_sendable:
            # 2026-08-27: calls Resend directly (not EmailService.send())
            # so the real status_code/body reaches this response -- send()
            # only ever print()s its failure reason to server stdout,
            # invisible without Railway CLI log access (the exact same gap
            # this whole endpoint exists to close for the SMTP path above).
            from services.email_service import RESEND_FROM_EMAIL as _from
            from_field = _from if "<" in _from else f"XFINLAB <{_from}>"
            try:
                send_resp = requests.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                    json={"from": from_field, "to": [test_to], "subject": "[XFINLAB] email-debug test (Resend)",
                          "html": "<p>This is a test send from /admin/email-debug.</p>"},
                    timeout=15,
                )
                result["send_test"] = {
                    "to": test_to,
                    "sent": send_resp.status_code in (200, 201),
                    "status_code": send_resp.status_code,
                    "body": send_resp.text[:500],
                }
            except Exception as e:
                result["send_test"] = {"to": test_to, "sent": False, "error": str(e)}

        return result

    env_status = {
        "EMAIL_ADDRESS": bool(os.getenv("EMAIL_ADDRESS")),
        "EMAIL_APP_PASSWORD": bool(os.getenv("EMAIL_APP_PASSWORD")),
        "SMTP_HOST": os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "SMTP_PORT": int(os.getenv("SMTP_PORT", "587")),
    }

    if not env_status["EMAIL_ADDRESS"] or not env_status["EMAIL_APP_PASSWORD"]:
        return {
            "env": env_status,
            "conclusion": "EMAIL_ADDRESS and/or EMAIL_APP_PASSWORD is not set on Railway -- "
                          "smtplib.login() would fail immediately with these missing. Set both "
                          "in Railway's environment variables and retry this endpoint.",
        }

    email_address = os.getenv("EMAIL_ADDRESS")
    email_password = os.getenv("EMAIL_APP_PASSWORD")
    smtp_host = env_status["SMTP_HOST"]
    smtp_port = env_status["SMTP_PORT"]

    steps = {"connect": None, "starttls": None, "login": None, "send": None}
    try:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
        steps["connect"] = "ok"
        try:
            server.starttls()
            steps["starttls"] = "ok"
            try:
                server.login(email_address, email_password)
                steps["login"] = "ok"

                if send_test and test_to:
                    from email.mime.text import MIMEText
                    from email.mime.multipart import MIMEMultipart
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = "[XFINLAB] email-debug test"
                    msg["From"] = f"XFINLAB <{email_address}>"
                    msg["To"] = test_to
                    msg.attach(MIMEText("<p>This is a test send from /admin/email-debug.</p>", "html"))
                    server.send_message(msg)
                    steps["send"] = "ok"
            except Exception as e:
                steps["login"] = f"FAILED: {e}"
        except Exception as e:
            steps["starttls"] = f"FAILED: {e}"
        server.quit()
    except Exception as e:
        steps["connect"] = f"FAILED: {e}"

    if all(v in (None, "ok") for v in steps.values()) and steps["login"] == "ok":
        conclusion = (
            "Login succeeded -- SMTP credentials and host/port are correct. "
            "Re-run with send_test=true&test_to=you@example.com to confirm actual delivery, "
            "or if that already worked, the original failure may have been transient "
            "(e.g. provider rate-limited a burst of signups)."
        ) if not (send_test and test_to) else (
            f"Login and test send both succeeded -- check {test_to}'s inbox (and spam folder)."
        )
    else:
        conclusion = "See the first non-'ok' step above for the exact failure point and exception."

    return {"env": env_status, "steps": steps, "conclusion": conclusion}


@router.get("/admin/security-scan")
def get_security_scan(token: str, request: Request):
    """
    Task #326: surfaces the security watch (scripts/security_scan.py /
    services/security_scan_service.py) in the admin panel, instead of it
    only ever existing as terminal output from the external 6-hourly
    scheduled task. Returns the most recent scan result already
    persisted into xfinlab.db by the in-process APScheduler job
    (backend/main.py) or a prior manual run -- this is intentionally
    fast/read-only, it does NOT run a fresh scan on every page load.
    """
    verify_admin(token, "get_security_scan", request)
    from services.security_scan_service import get_latest_scan_result, get_scan_history
    latest = get_latest_scan_result()
    if latest is None:
        return {"status": "no_data", "result": None, "history": []}
    return {"status": "ok", "result": latest, "history": get_scan_history(limit=10)}


@router.post("/admin/security-scan/run")
def run_security_scan_now(token: str, request: Request):
    """
    Manual "Run Scan Now" trigger for the admin panel. Runs synchronously
    (skips the slow pip-audit dependency scan so this stays fast enough
    for a single HTTP request -- the in-process 6-hourly job still runs
    the full scan including dependency CVEs) and persists the result
    like every other run, so it immediately shows up in get_security_scan
    and in the history list too.
    """
    verify_admin(token, "run_security_scan_now", request)
    from services.security_scan_service import run_and_save
    result = run_and_save(skip_dependency_scan=True)
    return {"status": "ok", "result": result}
