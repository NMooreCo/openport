# OpenPort in a container: the dashboard (a Nebius Serverless Endpoint) and tuning runs (Nebius Serverless Jobs).
#
#   docker build -t openport .
#   docker run -p 8000:8000 -e NEBIUS_API_KEY -e TAVILY_API_KEY openport                    # dashboard
#   docker run -e NEBIUS_API_KEY openport openport tune examples/supportdesk --sites triage  # one tuning job
#
# Keys are passed at run time and never baked into the image (.env is excluded by .dockerignore).

FROM node:22.23.4-slim AS dashboard
WORKDIR /app/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/ ./
RUN npm run build

FROM python:3.12.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /bin/uv
# git: the dashboard and CLI shallow-clone public GitHub repos to scan them.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
COPY --from=dashboard /app/src/openport/server/static/ src/openport/server/static/
RUN uv sync --frozen --no-dev --no-editable
COPY examples/ examples/
ENV PATH="/app/.venv/bin:$PATH" OPENPORT_RUNS_DIR=/app/runs OPENPORT_EXAMPLES=/app/examples
# A public dashboard runs repo code only for its demo apps and pinned eval packs: testing a call imports the
# repo into this process, next to the API keys. Set it to "any" for a private deployment you trust with any repo.
ENV OPENPORT_RUN_REPO_CODE=vetted
EXPOSE 8000
CMD ["openport", "serve", "--host", "0.0.0.0", "--port", "8000"]
