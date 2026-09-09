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
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

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
