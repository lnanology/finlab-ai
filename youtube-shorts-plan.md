# YouTube Shorts Content Plan — XFINLAB

Prepared 2026-08-31, off the back of AJ's market-pain-point research batch
(zero-fabrication trust angle, competitor pain points) and the
already-shipped `trust.html` + Data Factory sources. Every prompt below is
ready to paste into **admin.html's Growth OS → Video Engine → chat-to-video
box** (`POST /admin/video/generate-custom`, `services/video_engine_service.
generate_custom_video()`), which already exists and already supports 9:16
vertical output, 7 narration languages, and one-click Telegram + YouTube
auto-upload.

## Why this is worth the posting cadence

Per current research on B2B/SaaS short-form video: 96% of B2B marketers
report positive ROI from short-form video, brands posting 12+ Shorts/month
see a 35% reduction in cost-per-lead vs. static ads alone, and **native**
short-form content (written for the vertical format, not a repurposed clip)
gets a 3x higher completion rate than repurposed horizontal content. That
last point matters most here: every prompt below is written as its own
short, not a chopped-up version of a longer video.

## Before you start

1. Confirm in admin.html → Growth OS that the `video_engine` feature flag
   is ON, and the status widget shows `GOOGLE_TTS_API_KEY` configured +
   ffmpeg present (`GET /admin/video-engine-status`). If either is
   missing, generation will return `{"available": false, ...}` instead
   of failing silently — the message tells you which one.
2. For direct YouTube upload, `GOOGLE_YT_REFRESH_TOKEN` and related
   `GOOGLE_YT_*` env vars need to be set (one-time setup via the repo's
   `get_youtube_refresh_token.py` script) — otherwise generate the video
   here and upload manually the first few times.
3. Tick "Upload to YouTube" (and/or "Post to Telegram") per-generation —
   neither happens automatically, by design, so testing never spams a
   live channel.

## 10 ready-to-paste prompts

Each is written to hit a specific, real pain point from the research —
not generic brand messaging (role-specific pain points reportedly
outperform generic ones for this audience).

1. **Zero-fabrication vs. AI hallucination crisis**
   `Make a vertical Shorts video: "AI-generated stock data cost traders $2.3 billion in Q1 2026. Here's how XFINLAB's API guarantees every number is real or explicitly marked unavailable — never guessed." Reference trust.html's live data source status page. English.`

2. **IEX Cloud shutdown fear**
   `Make a vertical Shorts video about developers who got stranded when IEX Cloud shut down in August 2024 with only 3 months' notice, and how XFINLAB publishes an open, live data-source status page so nothing like that is a surprise. English.`

3. **openFDA recall lookup demo (consumer hook)**
   `Make a vertical Shorts video: "Did you know you can check if a company you're invested in has an active FDA recall, for free, in one API call?" Explain XFINLAB's openFDA consumer-safety endpoint. English.`

4. **CPSC recall + "we tell you when a data source is down" honesty angle**
   `Make a vertical Shorts video about why XFINLAB shows "temporarily unavailable" instead of a fake number when a government data source (like CPSC) is having an outage — most APIs would just show stale or made-up data instead. English.`

5. **Opportunity Radar — free, no signup**
   `Make a vertical Shorts video showing off XFINLAB's free Opportunity Radar tool — real, live macro data across real estate, supply chain, consumer demand, energy and agriculture, no fabricated composite score, no signup required. English.`

6. **Insider trading transparency**
   `Make a vertical Shorts video: "Every SEC Form 4 insider trade, indexed and queryable by ticker, in one free API call." Explain why this matters for retail investors doing their own research. English.`

7. **MCP server for AI agents (dev-audience hook)**
   `Make a vertical Shorts video for developers: "Give your AI agent real financial data with 3 lines of code" — explain XFINLAB's MCP server integration with Claude and other agent frameworks. English.`

8. **Free tier, no card, instant key (developer pain point)**
   `Make a vertical Shorts video about how XFINLAB issues a free API key instantly, no credit card, no sales call — contrasted with financial data APIs that gate pricing behind a sales conversation. English.`

9. **Cantonese/Hong Kong audience — zero-fabrication in Cantonese**
   `製作一條vertical Shorts影片，用廣東話講解點解XFINLAB嘅API唔會用AI亂估數據 —— 每個數字都嚟自真實政府或交易所來源，如果冇資料就會誠實話畀你知，唔會靠估。zh-HK.`

10. **Webhook alerts (sticky-feature hook)**
    `Make a vertical Shorts video: "Stop refreshing your dashboard." Explain XFINLAB's Pro-tier webhooks — get pushed a notification the moment VIX regime shifts, a new 13D filing appears, or Opportunity Radar's industry lean flips. English.`

## 9 more prompts — one per core feature (2026-09-06 addition)

Educational/how-to-use content, not pain-point marketing like the 10
above — these are meant to teach people what each tool does, so they
double as onboarding content and can be reused as the narration base for
the Feature Tutorials help page. Each names the real page so viewers can
go try it immediately.

11. **AI Chart Analysis**
    `Make a vertical Shorts video explaining XFINLAB's free AI Chart Analysis tool at xfinlab.com/chart-analysis.html — type any global ticker (US, Hong Kong, China A-shares, or crypto) and get real technical analysis: trend, support/resistance, and a confidence-scored signal, computed from real price history, not an AI guess. English.`

12. **Black Swan Stress Test Lab**
    `Make a vertical Shorts video explaining XFINLAB's free Stress Test Lab at xfinlab.com/stress-lab.html — see how your portfolio strategy would have survived 2008 or the 2020 crash, using a real Monte Carlo simulation seeded from actual historical returns, not a made-up volatility number. English.`

13. **Free Daily Signals**
    `Make a vertical Shorts video about XFINLAB's Free Daily Market Signals at xfinlab.com/free-signals.html — a free, no-login daily pick of the strongest technical signals across stocks, futures and crypto. English.`

14. **Opportunity Radar deep dive**
    `Make a vertical Shorts video walking through XFINLAB's Opportunity Radar at xfinlab.com/opportunity-radar.html — real macro data across US real estate, supply chain, consumer demand, energy and agriculture, no fabricated cross-industry score. English.`

15. **Stock Screener**
    `Make a vertical Shorts video explaining XFINLAB's free AI Stock Screener at xfinlab.com/screener.html — pick a preset like High Growth or Value Investing, or set your own criteria, and get an AI-written screening report. English.`

16. **Pairs Arbitrage Scanner**
    `Make a vertical Shorts video about XFINLAB's Pairs Statistical Arbitrage Scanner at xfinlab.com/pairs-scan.html — enter two tickers like KO and PEP and see a real correlation + z-score divergence read on their price spread. English.`

17. **Probability Scan**
    `Make a vertical Shorts video explaining XFINLAB's Probability Scan at xfinlab.com/probability-scan.html — combines real market data, technicals and news sentiment into a bullish/bearish probability read, clearly labeled as a reference number, not investment advice. English.`

18. **News Denoise**
    `Make a vertical Shorts video about XFINLAB's News Denoise Analysis at xfinlab.com/news-denoise.html — type a ticker or topic and get an AI summary that strips out sensationalized headline hype, leaving the substance. English.`

19. **Portfolio Allocation Analysis**
    `Make a vertical Shorts video explaining XFINLAB's Portfolio Allocation Analysis at xfinlab.com/portfolio.html — see suggested allocation weights across your watchlist based on each ticker's real market score, so you can check how diversified you actually are. English.`

Cantonese versions: reuse the same 9 prompts with `用廣東話講解` swapped
in and `zh-HK.` as the trailing language tag, same pattern as prompt #9
in the original 10 above — worth doing for at least a few of these since
that audience segment already converts well per the original batch.

## Suggested cadence

Research cited above ties the CPL improvement specifically to **12+
Shorts/month** — roughly 3/week. A realistic ramp:

- Week 1-2: post prompts 1, 2, 5, 7 (strongest hooks: trust crisis,
  IEX Cloud fear, free tool, dev audience) — 2/week.
- Week 3+: fill in with 3, 4, 6, 8, 9, 10 and start reusing #1/#5's
  format with fresh numbers (Opportunity Radar's live data changes
  week to week, so it's a natural repeat topic without repeating
  content).

## What this plan does NOT do

It doesn't post anything automatically — every generation is a manual
"Generate Now" / chat-to-video trigger in admin.html, by design (per
`services/video_engine_service.py`'s docstring: this is the heaviest
single Growth OS operation, so it stays a deliberate admin action, not
an unattended cron, unless/until you decide to wire a schedule).
