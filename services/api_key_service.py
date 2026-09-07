"""2026-07-30: issuance/verification for Intelligence API keys.

IMPORTANT correction made while building this: database/db.py (a
SQLAlchemy `User`/declarative-Base layer) is NOT what the live app
actually uses -- sqlalchemy isn't even in requirements.txt, and the real
`users` table is created/managed via raw sqlite3 in
backend/auth/auth.py (schema: id, email, password, name, plan,
created_at, risk_flagged, plan_expires_at). database/db.py is dead
scaffolding nothing imports (confirmed via grep before writing this --
only tests/test_level1.py references it). This module follows the real,
live convention instead: raw sqlite3 against the same xfinlab.db file
services/quota_service.py and backend/auth/auth.py already use.

V1 is admin-issued only for paid tiers (see api/intelligence.py's
/intelligence/admin/issue-key, gated by api.admin.verify_admin) -- no
Stripe/Paddle billing wired up yet, so Pro/Enterprise stay manual.

2026-07-31: Free tier now has automated self-serve issuance (see
issue_self_serve_free_key below), added to close that specific gap while
paid-tier billing is still unbuilt. It deliberately lives in its own
self_serve_api_keys table rather than reusing api_keys/users -- a public,
unauthenticated signup shouldn't require (or silently create) a full
XFINLAB consumer account, and keeping it in a separate table means this
change is purely additive: zero schema changes to the tables the
admin-issued flow already depends on in production.
"""
import hashlib
import sqlite3
import os
import secrets
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "xfinlab.db")


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _hash_key(raw_key: str) -> str:
    """2026-09-07 (security review: "API keys stored in plaintext at
    rest, fix: hash keys, use hmac.compare_digest if ever compared
    in-process"): SHA-256 hex digest of a raw API key. One-way -- once
    a row is migrated (see verify_key()'s docstring below), the
    plaintext bearer secret is no longer recoverable from the database
    at all, even with full read access to xfinlab.db.

    Plain SHA-256 (not a slow KDF like bcrypt/argon2, which IS the
    right call for passwords -- see backend/auth/password.py) is the
    right choice here specifically because these keys are already
    high-entropy, randomly generated secrets (generate_key() below:
    secrets.token_urlsafe(32), 256 bits of real randomness), not
    low-entropy user-chosen passwords. There's no realistic offline
    dictionary/brute-force attack surface against a value like that for
    a slow KDF to meaningfully defend against beyond what a fast hash
    already closes, and a fast hash keeps every single Intelligence API
    request's auth lookup (this runs on the hot path of every paid API
    call) cheap."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _key_preview(raw_key: str) -> str:
    """First-8/last-4 preview shown in the dashboard/admin key list --
    factored out so both the credential tables' migration path and the
    original issuance path build it identically."""
    return raw_key[:8] + "..." + raw_key[-4:]


def _init_table():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            key TEXT UNIQUE NOT NULL,
            tier TEXT DEFAULT 'free',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            last_used_at TEXT
        )
    """)
    # 2026-09-07 (security review: hash keys at rest) -- guarded ALTERs,
    # same idempotent-no-op-if-column-exists convention every other
    # incremental column addition in this codebase uses (see e.g.
    # self_serve_api_keys' expires_at below). See verify_key()'s
    # docstring for the full migration story: `key` still exists and
    # stays UNIQUE NOT NULL (SQLite can't cheaply relax that without a
    # full table rebuild), but for every row migrated after this ships,
    # it holds the SAME value as key_hash -- never the plaintext secret.
    for ddl in (
        "ALTER TABLE api_keys ADD COLUMN key_hash TEXT",
        "ALTER TABLE api_keys ADD COLUMN key_preview TEXT",
    ):
        try:
            conn.execute(ddl)
        except Exception:
            pass
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_key_hash ON api_keys(key_hash)")
    conn.commit()
    conn.close()


_init_table()


def generate_key() -> str:
    # "xfl_" prefix makes leaked keys greppable/identifiable in logs, same
    # idea as Stripe's "sk_live_"/GitHub's "ghp_" prefixes.
    return "xfl_" + secrets.token_urlsafe(32)


def issue_key(email: str, tier: str = "free") -> dict:
    """Returns {"error": "..."} on failure, else {"key": "...", "tier": ...,
    "user_id": ...}. The raw key is only ever returned here, at issuance
    time -- show it to the admin/developer immediately, it isn't
    retrievable later (only a preview is, via list_keys_for_email).

    2026-09-07 (security review: hash keys at rest): the raw key is
    never written to the database at all for a freshly-issued key --
    `key_hash`/`key_preview` are computed here and are the only things
    persisted (plus `key` = the same hash value, purely to satisfy the
    pre-existing UNIQUE NOT NULL constraint -- see verify_key()'s
    docstring). The function's own return value / caller contract is
    completely unchanged: the raw key still comes back here, once,
    exactly as before."""
    conn = _get_db()
    try:
        user = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            return {"error": f"No user found with email {email}"}

        key = generate_key()
        key_hash = _hash_key(key)
        conn.execute(
            "INSERT INTO api_keys (user_id, key, key_hash, key_preview, tier, active) VALUES (?, ?, ?, ?, ?, 1)",
            (user["id"], key_hash, key_hash, _key_preview(key), tier),
        )
        conn.commit()
        return {"key": key, "tier": tier, "user_id": user["id"]}
    finally:
        conn.close()


def verify_key(key: str) -> dict:
    """Returns {"valid": False} if missing/inactive/unknown, else
    {"valid": True, "user_id":..., "tier":...}. Never raises -- callers
    (api/intelligence.py) turn a False result into a 401 themselves.

    2026-09-07 (security review: hash keys at rest, zero customer
    impact): looks up by key_hash first -- this covers every key issued
    after this migration shipped (issue_key()/issue_self_serve_*_key()
    now write key_hash immediately) AND every legacy key that's already
    been auto-migrated by a prior call to this same function. A row
    with key_hash IS NULL is a legacy key issued before this shipped --
    its `key` column still holds the real plaintext, checked here as a
    one-time fallback. On a legacy match, this call auto-migrates that
    row on the spot: fills in key_hash/key_preview and overwrites `key`
    with the hash, so the plaintext is gone from the database and this
    fallback path is never needed for that row again. Net effect: every
    key that gets used at least once after this ships is transparently
    migrated -- no re-issuance, no downtime, no "please get a new key"
    email to any of the live paying customers on this table."""
    if not key:
        return {"valid": False}

    conn = _get_db()
    try:
        key_hash = _hash_key(key)
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_hash=? AND active=1", (key_hash,)
        ).fetchone()
        if not row:
            legacy = conn.execute(
                "SELECT * FROM api_keys WHERE key=? AND key_hash IS NULL AND active=1", (key,)
            ).fetchone()
            if legacy:
                conn.execute(
                    "UPDATE api_keys SET key_hash=?, key_preview=?, key=? WHERE id=?",
                    (key_hash, _key_preview(key), key_hash, legacy["id"]),
                )
                conn.commit()
                row = legacy
        if row:
            conn.execute(
                "UPDATE api_keys SET last_used_at=? WHERE id=?",
                (datetime.utcnow().isoformat(), row["id"]),
            )
            conn.commit()
            return {"valid": True, "user_id": row["user_id"], "tier": row["tier"]}

        # 2026-07-31: fallback to the self-serve free-tier table (see
        # issue_self_serve_free_key below) -- kept as a second lookup here
        # rather than merging tables, so this stays the single
        # verification entrypoint api/intelligence.py calls regardless of
        # which flow issued the key.
        #
        # 2026-09-07: same hash-first/legacy-plaintext-fallback/auto-
        # migrate pattern as api_keys above.
        row2 = conn.execute(
            "SELECT * FROM self_serve_api_keys WHERE key_hash=? AND active=1", (key_hash,)
        ).fetchone()
        if not row2:
            legacy2 = conn.execute(
                "SELECT * FROM self_serve_api_keys WHERE key=? AND key_hash IS NULL AND active=1", (key,)
            ).fetchone()
            if legacy2:
                conn.execute(
                    "UPDATE self_serve_api_keys SET key_hash=?, key_preview=?, key=? WHERE id=?",
                    (key_hash, _key_preview(key), key_hash, legacy2["id"]),
                )
                conn.commit()
                row2 = legacy2
        if row2:
            # 2026-08-24 (self-serve Pro API billing): paid self-serve keys
            # carry an expires_at (subscription period end, refreshed by
            # the Stripe webhook on renewal -- see issue_self_serve_paid_key
            # below). Free-tier keys keep expires_at NULL and never hit
            # this branch. An expired row is treated like no row at all --
            # we don't flip active=0 here (a renewal can still land later
            # and should just extend expires_at), just refuse to verify it.
            if row2["expires_at"]:
                try:
                    expired = datetime.fromisoformat(row2["expires_at"]) < datetime.utcnow()
                except Exception:
                    expired = False
                if expired:
                    return {"valid": False}
            conn.execute(
                "UPDATE self_serve_api_keys SET last_used_at=? WHERE id=?",
                (datetime.utcnow().isoformat(), row2["id"]),
            )
            conn.commit()
            return {"valid": True, "user_id": None, "tier": row2["tier"]}

        return {"valid": False}
    finally:
        conn.close()


def get_email_for_key(key: str) -> "str | None":
    """2026-08-18 (quota-exceeded upgrade nudge): the one lookup neither
    verify_key() nor anything else in this file exposes -- given a raw key,
    return the email to notify, or None if the key is unknown/inactive.
    Checks self_serve_api_keys first (email is stored directly there),
    then falls back to api_keys -> users (admin-issued keys only carry a
    user_id, so this needs the join). Never raises; a lookup failure just
    means no nudge gets sent, not a broken request.

    2026-09-07 (security review: hash keys at rest): looks up by
    key_hash first (matches every migrated/freshly-issued row -- in
    practice this is always the branch that fires, since both real
    callers of this function run it right after verify_key() already
    ran for the same key in the same request, which auto-migrates a row
    on first use). Falls back to the plaintext `key` column purely for
    a not-yet-migrated legacy row, same as verify_key() -- read-only
    here (no migration side effect); verify_key() already owns that."""
    if not key:
        return None
    conn = _get_db()
    try:
        key_hash = _hash_key(key)
        row = conn.execute(
            "SELECT email FROM self_serve_api_keys WHERE (key_hash=? OR key=?) AND active=1",
            (key_hash, key),
        ).fetchone()
        if row:
            return row["email"]

        row2 = conn.execute(
            """
            SELECT u.email AS email
            FROM api_keys k JOIN users u ON u.id = k.user_id
            WHERE (k.key_hash=? OR k.key=?) AND k.active=1
            """,
            (key_hash, key),
        ).fetchone()
        if row2:
            return row2["email"]

        return None
    finally:
        conn.close()


def list_keys_for_email(email: str) -> list:
    conn = _get_db()
    try:
        user = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            return []
        rows = conn.execute(
            "SELECT * FROM api_keys WHERE user_id=?", (user["id"],)
        ).fetchall()
        return [
            {
                "id": r["id"],
                "tier": r["tier"],
                "active": bool(r["active"]),
                "created_at": r["created_at"],
                "last_used_at": r["last_used_at"],
                # 2026-09-07: key_preview column is set for every
                # migrated/freshly-issued row; the raw `key` fallback
                # below only ever fires for a legacy row that's never
                # been verified even once since the hash migration
                # shipped (key column still holds real plaintext then).
                "key_preview": r["key_preview"] or (r["key"][:8] + "..." + r["key"][-4:]),
            }
            for r in rows
        ]
    finally:
        conn.close()


def revoke_key(key_id: int) -> bool:
    conn = _get_db()
    try:
        cur = conn.execute("UPDATE api_keys SET active=0 WHERE id=?", (key_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2026-08-09 (task #724, AJ "全做" batch): logged-in-user self-service key
# view/regenerate for dashboard.html's account area. Reuses the same
# api_keys table + "raw key shown once" posture as issue_key()/revoke_key()
# above -- this is NOT a new issuance flow, just a user-facing wrapper: a
# regenerate is "revoke my own active key(s), then issue_key() a fresh one",
# scoped strictly to the caller's own user_id (never touches other users'
# rows, unlike the admin endpoints which take an arbitrary email).
# ---------------------------------------------------------------------------

# 2026-08-25 (AJ: "referral雙方加quota"): resolves a user_id straight to
# their active key -- referral_service.py's use_code() only has user_id
# for both the referrer and the new user (not their email), and needs a
# stable per-key identifier to call intelligence_quota_service.
# add_quota_bonus(). Returns the most-recently-issued active key's
# identifier, or None if this user has no key yet (a pre-existing user
# who registered before the auto-issuance batch shipped) -- callers
# treat None as "nothing to bonus, skip silently", never an error.
#
# 2026-09-07 (security review: hash keys at rest): returns key_hash, not
# the raw key -- after the credential-table migration above, the raw
# key isn't reliably retrievable here any more (a row that's been
# verified even once no longer holds it). intelligence_quota_service's
# add_quota_bonus()/get_quota_bonus() were updated in the same change to
# be keyed by this same hash value throughout (see that module's own
# 2026-09-07 note), so this return value's new meaning is consistent
# everywhere it flows. For a legacy row that hasn't been auto-migrated
# yet (key_hash still NULL), computes the hash on the fly from the
# still-present plaintext `key` column -- read-only, doesn't persist it
# (verify_key() owns that side effect) -- so this always returns the
# same stable hash regardless of a given row's migration state.
def get_active_key_for_user(user_id: int) -> "str | None":
    conn = _get_db()
    try:
        # id DESC as a tiebreaker: created_at has only second resolution,
        # so two keys issued within the same second would otherwise tie
        # and SQLite's tie-break order isn't guaranteed -- id is
        # autoincrement and always reflects true insertion order.
        row = conn.execute(
            "SELECT key, key_hash FROM api_keys WHERE user_id=? AND active=1 ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return row["key_hash"] or _hash_key(row["key"])
    finally:
        conn.close()


def get_my_key_status(email: str) -> dict:
    """For the dashboard account panel: never returns a raw key (none of
    the existing rows have it retained -- see issue_key()'s docstring), just
    whether the user has one and its masked preview/tier/dates."""
    keys = list_keys_for_email(email)
    active = [k for k in keys if k["active"]]
    if not active:
        return {"has_key": False}
    k = active[0]
    # 2026-08-25 (AJ: "referral雙方加quota"): surfaces the referral-earned
    # bonus (services/intelligence_quota_service.add_quota_bonus) so the
    # dashboard panel can show "+50 from referrals" rather than a user
    # wondering why their limit is higher than the advertised 300. Looks
    # the key identifier up separately (list_keys_for_email intentionally
    # never returns anything key-derived beyond the masked preview)
    # purely to read the bonus table -- never included in this
    # function's own return value.
    quota_bonus = 0
    try:
        conn = _get_db()
        user = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        conn.close()
        if user:
            key_ref = get_active_key_for_user(user["id"])  # key_hash -- see that function's 2026-09-07 note
            if key_ref:
                from services.intelligence_quota_service import get_quota_bonus
                quota_bonus = get_quota_bonus(key_ref)
    except Exception:
        quota_bonus = 0
    return {
        "has_key": True,
        "key_preview": k["key_preview"],
        "tier": k["tier"],
        "created_at": k["created_at"],
        "last_used_at": k["last_used_at"],
        "quota_bonus": quota_bonus,
    }


def regenerate_key_for_user(email: str, tier: str = "free") -> dict:
    """Revokes every active api_keys row owned by `email`'s user_id, then
    issues a brand new one. Returns the same shape as issue_key() (raw key
    included -- shown once, exactly like every other issuance path in this
    file)."""
    conn = _get_db()
    try:
        user = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            return {"error": f"No user found with email {email}"}
        conn.execute(
            "UPDATE api_keys SET active=0 WHERE user_id=? AND active=1", (user["id"],)
        )
        conn.commit()
    finally:
        conn.close()
    return issue_key(email, tier)


# ---------------------------------------------------------------------------
# 2026-07-31: Self-serve Free-tier automation (Task #575).
#
# Separate table on purpose -- see module docstring. Nothing above this
# line is touched or altered; verify_key() below gets one additive
# fallback check so api/intelligence.py keeps calling a single
# verify_key() regardless of which table actually issued the key.
# ---------------------------------------------------------------------------

SELF_SERVE_SIGNUP_DAILY_LIMIT_PER_IP = 5


def _init_self_serve_tables():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS self_serve_api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            key TEXT UNIQUE NOT NULL,
            tier TEXT DEFAULT 'free',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            last_used_at TEXT
        )
    """)
    # 2026-08-24 (self-serve Pro API billing): NULL for the Free tier (no
    # expiry, same as before), set to a real timestamp for a paid tier --
    # see issue_self_serve_paid_key() below. Guarded ALTER so this is a
    # no-op on a DB that already has the column, same convention as every
    # other incremental-column addition in this codebase.
    #
    # 2026-09-07 (security review: hash keys at rest) -- same key_hash/
    # key_preview addition + unique index as api_keys above. See
    # verify_key()'s docstring for the full migration story.
    for ddl in (
        "ALTER TABLE self_serve_api_keys ADD COLUMN expires_at TEXT",
        "ALTER TABLE self_serve_api_keys ADD COLUMN key_hash TEXT",
        "ALTER TABLE self_serve_api_keys ADD COLUMN key_preview TEXT",
    ):
        try:
            conn.execute(ddl)
        except Exception:
            pass
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_self_serve_api_keys_key_hash ON self_serve_api_keys(key_hash)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS self_serve_signup_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            date TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            UNIQUE(ip, date)
        )
    """)
    conn.commit()
    conn.close()


_init_self_serve_tables()


def check_self_serve_signup_rate(ip: str) -> bool:
    """Read-only -- True if `ip` still has budget to request a free
    self-serve key today. Call record_self_serve_signup_attempt()
    separately after an actual attempt (check-then-increment split, same
    convention as services/intelligence_quota_service.py), so this doesn't
    burn budget on a request that fails validation before ever reaching
    here."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT count FROM self_serve_signup_attempts WHERE ip=? AND date=?",
            (ip, today),
        ).fetchone()
        used = row["count"] if row else 0
        return used < SELF_SERVE_SIGNUP_DAILY_LIMIT_PER_IP
    finally:
        conn.close()


def record_self_serve_signup_attempt(ip: str):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn = _get_db()
    conn.execute(
        """
        INSERT INTO self_serve_signup_attempts (ip, date, count)
        VALUES (?, ?, 1)
        ON CONFLICT(ip, date) DO UPDATE SET count = count + 1
        """,
        (ip, today),
    )
    conn.commit()
    conn.close()


# 2026-08-25: shared email template for "here is your raw API key" --
# factored out of api/intelligence.py's /intelligence/v1/signup endpoint
# (which had this HTML inline) so backend/auth/auth.py's new
# "every free signup gets a key" registration hook (see
# _on_user_registered_issue_free_api_key in backend/auth/auth.py) can send
# the exact same email instead of a second, driftable copy. Also fixes a
# pre-existing mismatch this extraction surfaced: the old inline template
# said "200 weighted calls/day" but services/intelligence_quota_service.py's
# TIER_LIMITS["free"] is actually 100 -- corrected to read the real number
# from that module instead of a hardcoded (and wrong) string.
def send_api_key_email(email: str, key: str) -> bool:
    from services.email_service import EmailService
    from services.intelligence_quota_service import TIER_LIMITS

    limit = TIER_LIMITS.get("free", 100)
    html = f"""
    <div style="font-family:Arial,sans-serif;padding:20px;background:#080c14;color:#e2e8f0">
        <h2 style="color:#00d4ff">Your XFINLAB Intelligence API key</h2>
        <p>Free tier -- {limit} weighted calls/day. Keep this key secret; it will not be shown again (regenerate from your dashboard, or re-run self-serve signup with the same email, if lost).</p>
        <p style="font-family:monospace;background:#111827;padding:12px;border-radius:8px;word-break:break-all">{key}</p>
        <p>Docs: <a href="https://www.xfinlab.com/intelligence-api.html" style="color:#00d4ff">xfinlab.com/intelligence-api.html</a> &middot; Terms: <a href="https://www.xfinlab.com/api-terms.html" style="color:#00d4ff">api-terms.html</a></p>
    </div>
    """
    try:
        return EmailService.send(email, "[XFINLAB] Your Intelligence API key", html)
    except Exception:
        return False


def issue_self_serve_free_key(email: str) -> dict:
    """Public, unauthenticated free-tier issuance -- does NOT require a
    pre-existing `users` row (unlike issue_key() above, which is for
    admin-issued keys tied to a full XFINLAB consumer account). Tracked in
    self_serve_api_keys, keyed directly by email.

    Exactly one active self-serve key per email: a repeat signup silently
    deactivates any prior self-serve key(s) for that email before issuing a
    fresh one. The raw key is only ever shown once (emailed at issuance,
    same "never retrievable later" posture as issue_key()) -- re-signup is
    the recovery path for a lost key, there's nothing to "re-show"."""
    email = (email or "").strip().lower()
    conn = _get_db()
    try:
        conn.execute(
            "UPDATE self_serve_api_keys SET active=0 WHERE email=? AND active=1",
            (email,),
        )
        key = generate_key()
        key_hash = _hash_key(key)  # 2026-09-07: raw key never stored -- see issue_key()'s note
        conn.execute(
            "INSERT INTO self_serve_api_keys (email, key, key_hash, key_preview, tier, active) VALUES (?, ?, ?, ?, 'free', 1)",
            (email, key_hash, key_hash, _key_preview(key)),
        )
        conn.commit()
        return {"key": key, "tier": "free", "email": email}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2026-08-24: Self-serve PAID-tier issuance (Intelligence API Pro checkout).
#
# Mirrors issue_self_serve_free_key() above -- same table, same "one active
# self-serve key per email" posture -- but carries an expires_at so the key
# stops working if the Stripe subscription lapses. Called from the
# checkout.session.completed / invoice.paid webhook branches in
# api/webhooks_stripe.py, same as every other paid-grant path in that file.
# On renewal (invoice.paid on an existing subscription), the webhook calls
# this again with the same email/tier -- it re-issues a fresh key each time,
# same as a Free re-signup would. That's a deliberate simplification (no key
# rotation nagging for a renewal), not an oversight.
# ---------------------------------------------------------------------------

def issue_self_serve_paid_key(email: str, tier: str, days: int) -> dict:
    """Public issuance for a Stripe-paid Intelligence API tier (currently
    just 'pro'). Deactivates any prior self-serve key(s) for that email
    first, exactly like issue_self_serve_free_key(), then issues a fresh
    key with expires_at = now + days."""
    email = (email or "").strip().lower()
    conn = _get_db()
    try:
        conn.execute(
            "UPDATE self_serve_api_keys SET active=0 WHERE email=? AND active=1",
            (email,),
        )
        key = generate_key()
        key_hash = _hash_key(key)  # 2026-09-07: raw key never stored -- see issue_key()'s note
        from datetime import timedelta
        expires_at = (datetime.utcnow() + timedelta(days=days)).replace(microsecond=0).isoformat()
        conn.execute(
            "INSERT INTO self_serve_api_keys (email, key, key_hash, key_preview, tier, active, expires_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (email, key_hash, key_hash, _key_preview(key), tier, expires_at),
        )
        conn.commit()
        return {"key": key, "tier": tier, "email": email, "expires_at": expires_at}
    finally:
        conn.close()
