# XFINLAB API Cookbook — 9 Core Features

Prepared 2026-09-06. Every endpoint below was verified against the actual route
handlers in `api/*.py` and `services/*.py` — nothing here is guessed or
extrapolated. Each section is labeled so you know exactly what you're
building against:

- 🔑 **Official Intelligence API** — versioned, `X-API-Key` auth, documented
  on intelligence-api.html, part of the paid product. Safe to build a
  production integration on.
- 🌐 **Free website tool, no key** — powers a public page on xfinlab.com
  directly. No auth, no versioning guarantee, no SLA. Fine for personal
  scripts, quick lookups, or prototyping — **not recommended for anything
  you depend on staying stable**, since it can change without notice the
  same way any other part of a website's frontend can.

---

## 1. AI Chart Analysis 🔑 (also has a free web version)

**Free web tool:**
```bash
curl "https://api.xfinlab.com/api/chart-search/AAPL?period=6mo&interval=1d"
```

**Official API (X-API-Key, recommended for real use):**
```bash
curl "https://api.xfinlab.com/api/intelligence/v1/technical/AAPL" \
  -H "X-API-Key: YOUR_KEY"
```

```python
import requests

r = requests.get(
    "https://api.xfinlab.com/api/intelligence/v1/technical/AAPL",
    headers={"X-API-Key": "YOUR_KEY"},
)
data = r.json()["data"]
print(data["confluence"]["direction"], data["confluence"]["confidence_pct"])
print(data["support"], data["resistance"])
```

Response includes `confluence` (direction/confidence/bullish & bearish
signal list), `trend`, `support`/`resistance`, `decision_levels`, and
`market_structure` — all computed from real OHLC history, never
AI-guessed. The official API version omits raw OHLC bars (licensing);
the free web version includes them.

---

## 2. Stress Lab 🔑 (also has a free web version)

**Free web tool:**
```bash
curl -X POST "https://api.xfinlab.com/api/stress-lab" \
  -H "Content-Type: application/json" \
  -d '{"symbol": "Stocks/Bonds 60/40", "amount": 100000, "horizon_days": 252}'
```

**Official API:**
```python
import requests

r = requests.post(
    "https://api.xfinlab.com/api/intelligence/v1/stress-test",
    headers={"X-API-Key": "YOUR_KEY"},
    json={"symbol": "AAPL", "amount": 100000, "horizon_days": 252},
)
d = r.json()["data"]
print(f"Median outcome: ${d['ending_value_p50']:,.0f}")
print(f"5th percentile (bad case): ${d['ending_value_p5']:,.0f}")
print(f"Median max drawdown: {d['max_drawdown_p50_pct']}%")
```

This runs a real bootstrap Monte Carlo over actual historical returns
(`n_real_observations` tells you exactly how much real history backed the
simulation) — not a fabricated volatility assumption.

---

## 3. Free Signals 🌐 (no key, no official API equivalent)

```bash
curl "https://api.xfinlab.com/api/free-signals"
```

Returns today's top confluence-ranked signals across stocks/futures/crypto:
`{date, signals:[{ticker, label, price, confluence_direction,
confluence_confidence_pct, ...}], locked_count, plan}`. `locked_count`
tells you how many additional rows exist behind a login — this endpoint
intentionally rations rows for non-logged-in callers, so don't build
anything that assumes a fixed row count.

---

## 4. Opportunity Radar 🔑 (also has a free web version)

**Free web tool:**
```bash
curl "https://api.xfinlab.com/api/free-tools-demo/opportunity-radar"
```

**Official API:**
```bash
curl "https://api.xfinlab.com/api/intelligence/v1/opportunity-radar" \
  -H "X-API-Key: YOUR_KEY"
```

Both return the same shape: real % change per indicator across
`real_estate`, `supply_chain`, `consumer_demand`, `energy`, `agriculture`
— each indicator reports its own trailing change against itself, never a
fabricated cross-industry composite score (see `methodology_note` in the
response for the exact math).

---

## 5. Stock Screener 🌐 (no key, no official API equivalent)

```bash
curl -X POST "https://api.xfinlab.com/api/ai-analysis" \
  -H "Content-Type: application/json" \
  -d '{"filters": {"sector": "Technology", "growth": "high"}}'
```

Returns AI-written commentary (`conclusion`, `analysis`) grounded in the
filter criteria you pass — this is a free-text research aid, not a
structured list-of-tickers-with-scores endpoint. If you need a
structured, code-friendly screen, this isn't it yet (flagging as a real
gap, not glossing over it).

---

## 6. Pairs Statistical Arbitrage Scanner 🌐 (no key, no official API equivalent)

```bash
curl -X POST "https://api.xfinlab.com/api/pairs-scan" \
  -H "Content-Type: application/json" \
  -d '{"symbol_a": "KO", "symbol_b": "PEP", "period": "6mo"}'
```

Returns `z_score`, `correlation`, `divergence`, and which side of the
pair is `richer_symbol`/`cheaper_symbol` — a correlation + z-score
divergence read on the real historical spread, explicitly not a formal
cointegration test (the page says so, and so does the API).

---

## 7. Probability Scan 🌐 (no key, no official API equivalent)

```bash
curl "https://api.xfinlab.com/api/pipeline/AAPL"
```

Combines real market data, technicals, and news sentiment into
`bullish_probability`/`bearish_probability`. The page itself carries a
disclaimer that the underlying scoring formulas aren't yet backtested —
treat this as a reference number, not a calibrated probability, same as
the site does.

---

## 8. News Denoise Analysis 🌐 (no key, no official API equivalent)

```bash
curl -X POST "https://api.xfinlab.com/api/news-denoise" \
  -H "Content-Type: application/json" \
  -d '{"topic": "AAPL"}'
```

Returns an AI-generated summary (`analysis`, `conclusion`) that filters
sensationalized language out of recent headlines for a ticker or topic —
this is generated live per-request, not pulled from a structured news
database, so wording will vary call to call even for the same topic.

---

## 9. Portfolio Allocation Analysis 🌐 (no key, no official API equivalent)

```bash
curl "https://api.xfinlab.com/api/portfolio"
```

Without a login token this returns a default basket; with a site-login
`token` query param it personalizes to that user's actual watchlist.
Returns suggested `allocation` weights per ticker based on each ticker's
real market score — not equal-weighted, not user-set.

---

## Which one should you actually build on?

If you're evaluating XFINLAB as a data provider for something you'll
maintain long-term, start with the 3 🔑 endpoints — those are the ones
with a documented contract and a reason to expect they won't move under
you. The 6 🌐 endpoints are genuinely useful for quick scripts and
one-off research, and we're not hiding them, but they're free-tier site
plumbing, not a product commitment.
