FROM python:3.10-slim

WORKDIR /app

# 2026-09-09 (Glama.ai MCP server listing requirement, per the
# awesome-mcp-servers PR feedback): Glama builds and runs this Dockerfile
# directly to verify the MCP server actually starts and responds to
# introspection requests, rather than only checking the live production
# URL. Kept intentionally minimal/production-only -- no ffmpeg/fonts (the
# Video Engine's own is_available() check already degrades gracefully
# without them, same as on Railway if that apt layer were ever missing)
# and no requirements-dev.txt (test-only deps, irrelevant to booting the
# app).
#
# build-essential is included defensively in case QuantLib (a compiled
# C++ library with Python bindings) doesn't ship a prebuilt wheel for
# whatever platform Glama builds on -- without it, that one line in
# requirements.txt could fail to install and take the whole build down.
#
# curl is needed for the litestream install step below.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 2026-09-09 fix (real Railway outage): railway.json's own "deploy.startCommand"
# always runs `/app/bin/litestream ...` to wrap uvicorn (continuous SQLite
# replication), regardless of which builder actually produced the image.
# Adding this Dockerfile in PR #51 caused a real production deploy failure --
# "The executable /app/bin/litestream could not be found" -- because Railway
# built from THIS file instead of nixpacks.toml (which is where litestream
# was previously installed, in its own [phases.litestream] block) even
# though railway.json pins "builder": "NIXPACKS". Installing the identical
# binary here (same version/URL as nixpacks.toml) makes this Dockerfile
# correct no matter which builder Railway or any other platform picks.
RUN mkdir -p /app/bin \
    && curl -L https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-amd64.tar.gz -o /tmp/litestream.tar.gz \
    && tar -xzf /tmp/litestream.tar.gz -C /app/bin \
    && chmod +x /app/bin/litestream \
    && rm -f /tmp/litestream.tar.gz

# Same placeholder pattern already proven in .github/workflows/ci.yml:
# services/news_service.py's NewsService.__init__ only checks that
# NEWS_API_KEY is *present* at import time -- it never calls the real
# NewsAPI just to let backend.main import successfully. Real deployments
# (Railway) set a genuine key via their own env vars; this container only
# needs to boot and answer MCP introspection, not serve real news data.
ENV NEWS_API_KEY=docker-build-placeholder-not-a-real-key
ENV PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "PYTHONPATH=/app uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
