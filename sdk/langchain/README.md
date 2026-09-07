# xfinlab-langchain

LangChain tools for the [XFINLAB Intelligence API](https://www.xfinlab.com/intelligence-api.html) -- lets a LangChain agent call real, computed-from-actual-market-data endpoints (technical analysis, Monte Carlo stress testing, macro opportunity radar, news sentiment) instead of an LLM guessing those numbers from its own training data.

Built on top of [`xfinlab-intelligence`](../python), this repo's own official Python SDK -- not a reimplementation of the HTTP layer.

Not yet on PyPI (same reasoning as the base SDK -- no paying developers on the API yet to justify maintaining a public package release). Install directly from this repo:

```bash
pip install "git+https://github.com/lnanology/Xfinlab.git#subdirectory=sdk/langchain"
```

## Get a key

Free tier keys are issued instantly, no waiting: https://www.xfinlab.com/intelligence-api.html

## Quickstart

```python
from xfinlab_langchain import get_xfinlab_tools

tools = get_xfinlab_tools(api_key="xfl_...")
# tools is a plain list[StructuredTool] -- pass it to any LangChain
# agent constructor that accepts tools=[...]:

from langchain.agents import create_react_agent
from langchain import hub

prompt = hub.pull("hwchase17/react")
agent = create_react_agent(llm, tools, prompt)
```

## What's included

| Tool name | What it does | Backed by |
|---|---|---|
| `xfinlab_technical_analysis` | Trend, confluence direction/confidence, support/resistance, market structure | `GET /intelligence/v1/technical/{ticker}` |
| `xfinlab_stress_test` | Bootstrap Monte Carlo stress test seeded from real historical returns | `POST /intelligence/v1/stress-test` |
| `xfinlab_opportunity_radar` | Real macro snapshot: real estate, supply chain, consumer demand | `GET /intelligence/v1/opportunity-radar` |
| `xfinlab_sentiment` | Real news-sentiment read from recent headlines | `GET /intelligence/v1/sentiment` |

These four are a starting set, not the full API surface -- `XfinlabClient` (the underlying SDK) wraps ~20 more endpoints (insider trades, short interest, sector-specific macro context, forecasts, webhooks, and more). If your agent needs one of those as a tool, add a `StructuredTool.from_function(...)` following the same pattern in `xfinlab_langchain/__init__.py`'s `_make_tools()` -- it's about 10 lines per additional tool.

## Error handling

Every tool call is wrapped so a failed request comes back as a JSON string like `{"error": "...", "status_code": 429}` instead of raising and aborting the agent's run -- the agent sees a normal tool result either way and can decide how to react (retry, tell the user, try a different tool).

## License

MIT -- see `../LICENSE`.
