"""LlamaIndex tool spec for the XFINLAB Intelligence API.

Thin wrapper around ``xfinlab_intelligence.XfinlabClient`` (this repo's
own official Python SDK -- see sdk/python/xfinlab_intelligence/__init__.py)
exposed as a standard LlamaIndex ``BaseToolSpec``, the same pattern
LlamaIndex's own community tool specs (Yahoo Finance, Wikipedia, etc.)
use -- every public method listed in ``spec_functions`` becomes a real
tool an agent can call, with its docstring as the tool description and
its type-hinted signature as the tool's argument schema.

Covers the same 4 endpoints as this repo's LangChain integration
(sdk/langchain): technical analysis, stress testing, the macro
opportunity radar, and news sentiment. The first three are the ones
XFINLAB's own docs recommend building a real integration on (see
api-cookbook-9-features.md's "Which one should you actually build on?"
section); the underlying XfinlabClient wraps ~20 more endpoints --
add a matching method + name in ``spec_functions`` here if your agent
needs one that isn't wrapped yet.

Every method returns a JSON string and never raises -- a failed API call
comes back as ``{"error": "..."}`` so the agent sees a normal tool
result it can react to, not a crashed run.

Requires: llama-index-core>=0.10, xfinlab-intelligence (this repo's
Python SDK -- see its own README for why it isn't on PyPI yet).

    pip install "git+https://github.com/lnanology/Xfinlab.git#subdirectory=sdk/llamaindex"

Example:
    from xfinlab_llamaindex import XfinlabToolSpec
    from llama_index.core.agent.workflow import ReActAgent

    tool_spec = XfinlabToolSpec(api_key="xfl_...")
    agent = ReActAgent(tools=tool_spec.to_tool_list(), llm=llm)
"""
from __future__ import annotations

import json
from typing import Optional

from llama_index.core.tools.tool_spec.base import BaseToolSpec

from xfinlab_intelligence import XfinlabClient, XfinlabError

__version__ = "0.1.0"
__all__ = ["XfinlabToolSpec"]


def _safe_call(fn, *args, **kwargs) -> str:
    """Every tool method below routes its real call through here. Returns
    a JSON string on both success and failure -- never raises -- so a
    network error or a 4xx from the API surfaces to the calling agent as
    readable tool output, not a crashed agent run. Same helper/contract
    as sdk/langchain's _safe_call(), kept as a private duplicate rather
    than a shared import so each integration package has zero
    dependencies beyond its own framework + the base SDK."""
    try:
        result = fn(*args, **kwargs)
        return json.dumps(result, default=str)
    except XfinlabError as e:
        return json.dumps({"error": str(e), "status_code": e.status_code})
    except Exception as e:  # network errors, timeouts, etc. -- requests' own exceptions
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


class XfinlabToolSpec(BaseToolSpec):
    """XFINLAB Intelligence API tools for a LlamaIndex agent.

    Args:
        api_key: Your XFINLAB API key (free tier available -- see
            https://www.xfinlab.com/intelligence-api.html).
        base_url: Override the API base URL. Defaults to XfinlabClient's
            own default (https://api.xfinlab.com/api) -- only pass this
            if you're pointing at a self-hosted or staging instance.
        timeout: Per-request timeout in seconds.
    """

    spec_functions = ["technical_analysis", "stress_test", "opportunity_radar", "sentiment"]

    def __init__(self, api_key: str, base_url: Optional[str] = None, timeout: int = 30):
        kwargs = {"api_key": api_key, "timeout": timeout}
        if base_url is not None:
            kwargs["base_url"] = base_url
        self.client = XfinlabClient(**kwargs)

    def technical_analysis(self, ticker: str, period: str = "6mo", interval: str = "1d") -> str:
        """Get real technical analysis for a stock or crypto ticker: trend direction, a
        confidence-scored bullish/bearish confluence signal, support/resistance levels, and
        market structure -- all computed from real price history, not an AI guess.

        Args:
            ticker: Stock or crypto ticker symbol, e.g. 'AAPL', 'NVDA', 'BTC-USD'.
            period: History window, e.g. '1mo', '3mo', '6mo', '1y'.
            interval: Bar interval, e.g. '1d', '1wk'.
        """
        return _safe_call(self.client.technical, ticker, period=period, interval=interval)

    def stress_test(self, symbol: str, amount: float, horizon_days: int = 252) -> str:
        """Run a real bootstrap Monte Carlo stress test on a stock or portfolio strategy,
        seeded from actual historical returns (not a fabricated volatility assumption).
        Returns median and 5th-percentile ending value plus median max drawdown over the
        given horizon.

        Args:
            symbol: Ticker or named strategy, e.g. 'AAPL' or 'Stocks/Bonds 60/40'.
            amount: Starting portfolio amount in USD, e.g. 100000.
            horizon_days: Simulation horizon in trading days (252 is roughly 1 year).
        """
        return _safe_call(self.client.stress_test, symbol, amount, horizon_days=horizon_days)

    def opportunity_radar(self) -> str:
        """Get a real, current macro snapshot across US real estate, supply chain, and
        consumer demand -- each indicator's own real percent change and improving/worsening
        label, no fabricated cross-industry composite score. Takes no input."""
        return _safe_call(self.client.opportunity_radar)

    def sentiment(self, ticker: str, limit: int = 10) -> str:
        """Get a real news-sentiment read for a stock or crypto ticker, based on its most
        recent headlines.

        Args:
            ticker: Stock or crypto ticker symbol, e.g. 'TSLA'.
            limit: Max number of recent news items to factor into the read.
        """
        return _safe_call(self.client.sentiment, ticker, limit=limit)
