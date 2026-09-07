# xfinlab-llamaindex

LlamaIndex tool spec for the [XFINLAB Intelligence API](https://www.xfinlab.com/intelligence-api.html) -- lets a LlamaIndex agent call real, computed-from-actual-market-data endpoints (technical analysis, Monte Carlo stress testing, macro opportunity radar, news sentiment) instead of an LLM guessing those numbers from its own training data.

Built on top of [`xfinlab-intelligence`](../python), this repo's own official Python SDK -- not a reimplementation of the HTTP layer. Follows the same `BaseToolSpec` pattern as LlamaIndex's own community tool specs (Yahoo Finance, Wikipedia, etc.).

Not yet on PyPI (same reasoning as the base SDK -- no paying developers on the API yet to justify maintaining a public package release). Install directly from this repo:

```bash
pip install "git+https://github.com/lnanology/Xfinlab.git#subdirectory=sdk/llamaindex"
```

## Get a key

Free tier keys are issued instantly, no waiting: https://www.xfinlab.com/intelligence-api.html

## Quickstart

```python
from xfinlab_llamaindex import XfinlabToolSpec
from llama_index.core.agent.workflow import ReActAgent

tool_spec = XfinlabToolSpec(api_key="xfl_...")
agent = ReActAgent(tools=tool_spec.to_tool_list(), llm=llm)

response = await agent.run("What's the current technical setup for NVDA?")
```

You can also grab individual tools instead of the full set:

```python
tools = tool_spec.to_tool_list(spec_functions=["technical_analysis", "stress_test"])
```

## What's included

| Method | What it does | Backed by |
|---|---|---|
| `technical_analysis(ticker, period="6mo", interval="1d")` | Trend, confluence direction/confidence, support/resistance, market structure | `GET /intelligence/v1/technical/{ticker}` |
| `stress_test(symbol, amount, horizon_days=252)` | Bootstrap Monte Carlo stress test seeded from real historical returns | `POST /intelligence/v1/stress-test` |
| `opportunity_radar()` | Real macro snapshot: real estate, supply chain, consumer demand | `GET /intelligence/v1/opportunity-radar` |
| `sentiment(ticker, limit=10)` | Real news-sentiment read from recent headlines | `GET /intelligence/v1/sentiment` |

These four are a starting set, not the full API surface -- `XfinlabClient` (the underlying SDK) wraps ~20 more endpoints (insider trades, short interest, sector-specific macro context, forecasts, webhooks, and more). If your agent needs one of those, add a method to `XfinlabToolSpec` and its name to `spec_functions` in `xfinlab_llamaindex/__init__.py` -- LlamaIndex turns any listed method into a tool automatically, using its docstring as the description and its type hints as the argument schema.

## Error handling

Every tool call is wrapped so a failed request comes back as a JSON string like `{"error": "...", "status_code": 429}` instead of raising and aborting the agent's run -- the agent sees a normal tool result either way and can decide how to react (retry, tell the user, try a different tool).

## License

MIT -- see `../LICENSE`.
