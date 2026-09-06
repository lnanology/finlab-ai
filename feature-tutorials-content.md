# XFINLAB Feature Tutorials — content draft for Help/Tutorials page

Prepared 2026-09-06. Each section below is the written walkthrough for one
feature. `[MOBILE SCREENSHOT: page-name.html]` marks where AJ's phone
screenshot slots in once captured (all 10 pages already verified live and
working on mobile via desktop-tool testing 2026-09-05/06 — screenshots
themselves need to come from AJ's own phone since the tool used to verify
can't export image files).

---

## 1. AI Chart Analysis

**What it does:** Type any global ticker (US, Hong Kong, China A-shares,
crypto) and get an instant technical read — trend direction, support/
resistance, and a confidence-scored confluence signal — computed from real
historical price data, not an AI guess.

**How to use it:**
1. Go to xfinlab.com → Chart Engine (or `/chart-analysis.html` directly)
2. Type a ticker — e.g. `AAPL`, `0700` or `0700.HK` for Hong Kong,
   `600519` for China A-shares, `BTC` for crypto
3. Tap **Analyze**
4. Read the confluence score and direction, then scroll down for the
   full support/resistance levels and trend breakdown

[MOBILE SCREENSHOT: chart-analysis.html]

**Worth knowing:** the confidence percentage reflects how many of the
underlying technical signals agree with each other — it is not a
prediction of future price movement.

---

## 2. Black Swan Stress Test Lab

**What it does:** Shows how a given strategy (or single ticker) would
have behaved during real historical crisis periods — 2008, 2020 COVID
crash, etc. — using an actual Monte Carlo simulation seeded from real
historical returns, not a made-up volatility number.

**How to use it:**
1. Go to `/stress-lab.html`
2. Pick a strategy preset (e.g. "Stocks/Bonds 60/40") or type a ticker
3. Enter your investment amount
4. Tap **Run Simulation**
5. Scroll down to see outcome ranges (5th/25th/50th/75th/95th percentile)
   and the median max drawdown

[MOBILE SCREENSHOT: stress-lab.html]

**Worth knowing:** the page states plainly this is "estimates based on
historical event declines, not a full historical price backtest" — read
that line, it's there for a reason.

---

## 3. Free Daily Market Signals

**What it does:** A free, no-login daily pick of the strongest technical
signals across stocks, futures, and crypto, ranked by confidence.

**How to use it:**
1. Go to `/free-signals.html`
2. Signals refresh daily — the "Updated" date at the top shows freshness
3. Optionally subscribe by email or enable notifications to get pushed
   the day's picks automatically

[MOBILE SCREENSHOT: free-signals.html]

**Worth knowing:** some rows are locked for non-logged-in visitors
(`locked_count` in the API) — creating a free account unlocks the rest.

---

## 4. Opportunity Radar

**What it does:** Real macro data across US real estate, supply chain/
manufacturing, consumer demand, energy, and agriculture — shows which
indicators are actually improving vs. worsening, with real numbers and
dates. No signup required.

**How to use it:**
1. Go to `/opportunity-radar.html`
2. Scroll through the macro backdrop (Fed Funds Rate, unemployment,
   yield curve) and then each industry section

[MOBILE SCREENSHOT: opportunity-radar.html]

**Worth knowing:** there is deliberately no single combined "opportunity
score" — see the Methodology section on the page for why a fabricated
cross-industry composite would be misleading.

---

## 5. Stock Screener

**What it does:** Set criteria (or use a quick preset like "High Growth"
or "Value Investing") and get an AI-written screening report explaining
which stocks fit and why.

**How to use it:**
1. Go to `/screener.html`
2. Tap a quick preset, or set Market / Sector / Market Cap manually
3. Review the AI's written analysis

[MOBILE SCREENSHOT: screener.html]

**Worth knowing:** this returns written analysis, not a sortable table of
tickers with numeric scores — treat it as a research starting point, not
a ranked list to trade off directly.

---

## 6. Pairs Statistical Arbitrage Scanner

**What it does:** Enter two tickers (e.g. KO and PEP) and see whether
today's price spread between them has moved unusually far from its own
historical norm — a real correlation + z-score divergence read.

**How to use it:**
1. Go to `/pairs-scan.html`
2. Enter Symbol A and Symbol B
3. Pick a lookback period (default 6 months)
4. Review the z-score, correlation, and which side is currently richer/
   cheaper relative to the pair's own history

[MOBILE SCREENSHOT: pairs-scan.html]

**Worth knowing:** the page is explicit that this is "not a formal
cointegration test" — it's a simpler, real-data divergence signal.

---

## 7. Probability Scan

**What it does:** Combines real market data, technical indicators, and
news sentiment into a bullish/bearish probability read for a ticker.

**How to use it:**
1. Go to `/probability-scan.html`
2. Enter a ticker
3. Tap **Scan**

[MOBILE SCREENSHOT: probability-scan.html]

**Worth knowing:** the page carries its own disclaimer — the internal
scoring formulas haven't been backtested yet, so this is a reference
probability, not investment advice, and shouldn't be treated as
calibrated.

---

## 8. News Denoise Analysis

**What it does:** Enter a ticker or market topic and get an AI summary
that filters out sensationalized headline language, leaving the
substance.

**How to use it:**
1. Go to `/news-denoise.html`
2. Type a ticker, topic, or question (e.g. "AAPL", "Fed rate decision")
3. Tap **Start Analysis**

[MOBILE SCREENSHOT: news-denoise.html]

**Worth knowing:** this is generated live by AI per-request, not pulled
from a structured news archive — wording will vary between calls even
for the same topic, by design (it's a fresh read each time, not a cached
canned answer).

---

## 9. Portfolio Allocation Analysis

**What it does:** Uses each of your watchlist stocks' real market scores
to suggest allocation weights, so you can see how diversified your
portfolio actually is.

**How to use it:**
1. Go to `/portfolio.html`
2. If logged in with a watchlist, tap **Calculate Allocation** to use
   your real holdings; otherwise it uses a default basket
3. Review the suggested weights per ticker

[MOBILE SCREENSHOT: portfolio.html]

**Worth knowing:** weights are derived from each ticker's real market
score, not equal-weighted and not something you set manually — it's a
suggestion to compare against your actual allocation, not an
auto-rebalancing tool.

---

## Also worth mentioning: the Stock Analysis Dashboard

The main dashboard (`/dashboard.html`) is the fastest single entry point
if a visitor only tries one thing: type a ticker, tap Analyze, and see
Final Score / Price / Risk Level / AI Rating together with a score
breakdown and AI research report — effectively a one-screen summary that
pulls from several of the engines above.

[MOBILE SCREENSHOT: dashboard.html]
