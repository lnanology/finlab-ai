# XFINLAB Intelligence API SDKs

Official client libraries for the [XFINLAB Intelligence API](https://www.xfinlab.com/intelligence-api.html) -- structured market events, FinBERT sentiment, multi-agent AI debate, an AI-clustered intelligence feed, technical/market-structure analysis, and Monte Carlo stress testing. Every endpoint returns real, traceable data -- no fabricated numbers, same anti-fabrication principle behind the rest of xfinlab.com.

- [`python/`](python/) -- `xfinlab-intelligence` Python client (`requests`-based, one file, zero internal imports)
- [`js/`](js/) -- `xfinlab-intelligence` JavaScript/Node client (native `fetch`, zero dependencies, UMD)
- [`langchain/`](langchain/) -- `xfinlab-langchain` LangChain tools, built on the Python client
- [`llamaindex/`](llamaindex/) -- `xfinlab-llamaindex` LlamaIndex tool spec, built on the Python client
- [`examples/`](examples/) -- runnable quickstart scripts for both base clients

The Python/JS clients are thin, honest wrappers: one method per endpoint, no client-side caching or retry magic that could mask a real API error, and the same error surfaced to you that the API itself returned. The LangChain/LlamaIndex packages wrap the Python client's methods as agent tools -- same real data, same error transparency, just packaged so an AI agent can call them directly instead of guessing technicals or a stress-test outcome from its own training data.

## Get an API key

Free tier: instant, automated, no waiting. Pro/Enterprise: request access and we'll follow up personally with pricing.

https://www.xfinlab.com/intelligence-api.html

## Status: not yet on PyPI / npm

All four packages are fully functional but not yet published to a public package registry -- there are no paying developers on this API yet to justify the ongoing maintenance overhead of a public release (an honest scope decision, not an oversight; see each package's own README for the current install path from this repo). If that changes, this README will be the first thing updated with real `pip install` / `npm install` instructions.

## Terms

Use of the API (and therefore these clients) is governed by the [API Terms of Service](https://www.xfinlab.com/api-terms.html).

## License

MIT -- see [`LICENSE`](LICENSE). Applies to the SDK code in this directory only, not to the XFINLAB API service itself or the data it returns.
