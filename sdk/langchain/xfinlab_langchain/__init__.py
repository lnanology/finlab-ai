"""LangChain tools for the XFINLAB Intelligence API.

Thin wrappers around ``xfinlab_intelligence.XfinlabClient`` (this repo's
own official Python SDK -- see sdk/python/xfinlab_intelligence/__init__.py)
so a LangChain agent can call XFINLAB's real, computed-from-actual-market-
data endpoints as tools, instead of an LLM guessing at technicals or a
stress-test outcome from its own training data.

Covers the 4 endpoints most useful to a general-purpose finance agent:
technical analysis, stress testing, the macro opportunity radar, and news
sentiment. The first three are the ones XFINLAB's own docs recommend
building a real integration on (see api-cookbook-9-features.md's "Which
one should you actually build on?" section) -- versioned, documented, not
going to change shape without notice. The underlying XfinlabClient wraps
~20 more endpoints (insider trades, short interest, sector-specific macro
context, forecasts, webhooks, etc.); add a matching StructuredTool here
following the same pattern (see _make_tools() below) if your agent needs
one that isn't wrapped yet -- this module is intentionally a starting
set, not an attempt to mirror the entire API surface up front.

Every tool's underlying function returns a JSON string (LangChain tool
outputs are conventionally strings an LLM can read directly), and every
tool catches its own errors -- a failed API call comes back to the agent
as ``{"error": "..."}`` instead of an unhandled exception aborting the
whole agent run, so the agent can react to a real failure message
("stress test failed: rate limited") the same way it would to any other
tool result.

Requires: langchain-core>=0.2, xfinlab-intelligence (this repo's Python
SDK -- see its own README for why it isn't on PyPI yet).

    pip install "git+https://github.com/lnanology/Xfinlab.git#subdirectory=sdk/langchain"

Example:
    from xfinlab_langchain import get_xfinlab_tools

    tools = get_xfinlab_tools(api_key="xfl_...")
    # tools is a plain list[BaseTool] -- pass it to any LangChain agent
    # constructor that accepts `tools=[...]`, e.g.:
    #   from langchain.agents import create_react_agent
    #   agent = create_react_agent(llm, tools, prompt)
"""
from __future__ import annotations

import json
from typing import List, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from xfinlab_intelligence import XfinlabClient, XfinlabError

__version__ = "0.1.0"
__all__ = ["get_xfinlab_tools", "XfinlabError"]


def _safe_call(fn, *args, **kwargs) -> str:
    """Every tool function below routes its real call through here.
    Returns a JSON string on both success and failure -- never raises --
    so a network error or a 4xx from the API surfaces to the calling
    agent as readable tool output, not a crashed agent run."""
    try:
        result = fn(*args, **kwargs)
        return json.dumps(result, default=str)
    except XfinlabError as e:
        return json.dumps({"error": str(e), "status_code": e.status_code})
    except Exception as e:  # network errors, timeouts, etc. -- requests' own exceptions
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


class _TechnicalInput(BaseModel):
    ticker: str = Field(description="Stock or crypto ticker symbol, e.g. 'AAPL', 'NVDA', 'BTC-USD'")
    period: str = Field(default="6mo", description="History window, e.g. '1mo', '3mo', '6mo', '1y'")
    interval: str = Field(default="1d", description="Bar interval, e.g. '1d', '1wk'")


class _StressTestInput(BaseModel):
    symbol: str = Field(description="Ticker or named strategy, e.g. 'AAPL' or 'Stocks/Bonds 60/40'")
    amount: float = Field(description="Starting portfolio amount in USD, e.g. 100000")
    horizon_days: int = Field(default=252, description="Simulation horizon in trading days (252 is roughly 1 year)")


class _SentimentInput(BaseModel):
    ticker: str = Field(description="Stock or crypto ticker symbol, e.g. 'TSLA'")
    limit: int = Field(default=10, description="Max number of recent news items to factor into the read")


class _NoInput(BaseModel):
    """Opportunity Radar takes no arguments -- an empty schema still lets
    every LangChain tool-calling code path (which expects an
    args_schema) treat it the same as the other tools here."""


def _make_tools(client: XfinlabClient) -> List[StructuredTool]:
    def technical_analysis(ticker: str, period: str = "6mo", interval: str = "1d") -> str:
        return _safe_call(client.technical, ticker, period=period, interval=interval)

    def stress_test(symbol: str, amount: float, horizon_days: int = 252) -> str:
        return _safe_call(client.stress_test, symbol, amount, horizon_days=horizon_days)

    def opportunity_radar() -> str:
        return _safe_call(client.opportunity_radar)

    def sentiment(ticker: str, limit: int = 10) -> str:
        return _safe_call(client.sentiment, ticker, limit=limit)

    return [
        StructuredTool.from_function(
            func=technical_analysis,
            name="xfinlab_technical_analysis",
            description=(
                "Get real technical analysis for a stock or crypto ticker: trend direction, "
                "a confidence-scored bullish/bearish confluence signal, support/resistance "
                "levels, and market structure -- all computed from real price history, not "
                "an AI guess."
            ),
            args_schema=_TechnicalInput,
        ),
        StructuredTool.from_function(
            func=stress_test,
            name="xfinlab_stress_test",
            description=(
                "Run a real bootstrap Monte Carlo stress test on a stock or portfolio strategy, "
                "seeded from actual historical returns (not a fabricated volatility assumption). "
                "Returns median and 5th-percentile ending value plus median max drawdown over "
                "the given horizon."
            ),
            args_schema=_StressTestInput,
        ),
        StructuredTool.from_function(
            func=opportunity_radar,
            name="xfinlab_opportunity_radar",
            description=(
                "Get a real, current macro snapshot across US real estate, supply chain, and "
                "consumer demand -- each indicator's own real percent change and improving/"
                "worsening label, no fabricated cross-industry composite score. Takes no input."
            ),
            args_schema=_NoInput,
        ),
        StructuredTool.from_function(
            func=sentiment,
            name="xfinlab_sentiment",
            description=(
                "Get a real news-sentiment read for a stock or crypto ticker, based on its most "
                "recent headlines."
            ),
            args_schema=_SentimentInput,
        ),
    ]


def get_xfinlab_tools(
    api_key: str, base_url: Optional[str] = None, timeout: int = 30
) -> List[StructuredTool]:
    """Convenience factory: builds one XfinlabClient (one HTTP session,
    one API key) shared across all 4 tools, returned as a plain list
    ready to hand to any LangChain agent constructor.

    Args:
        api_key: Your XFINLAB API key (free tier available -- see
            https://www.xfinlab.com/intelligence-api.html).
        base_url: Override the API base URL. Defaults to
            XfinlabClient's own default (https://api.xfinlab.com/api) --
            only pass this if you're pointing at a self-hosted or staging
            instance.
        timeout: Per-request timeout in seconds.
    """
    kwargs = {"api_key": api_key, "timeout": timeout}
    if base_url is not None:
        kwargs["base_url"] = base_url
    client = XfinlabClient(**kwargs)
    return _make_tools(client)
