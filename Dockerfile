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
# The TypeScript demo app's dependencies (the OpenAI SDK and tsx), so its calls can run in evals.
WORKDIR /app/examples/supportdesk-ts
COPY examples/supportdesk-ts/package.json examples/supportdesk-ts/package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM python:3.12.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /bin/uv
# git: the dashboard and CLI shallow-clone public GitHub repos to scan them. Node runs TypeScript call sites in evals.
RUN apt-get update && apt-get install -y --no-install-recommends git libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY --from=dashboard /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
COPY --from=dashboard /app/src/openport/server/static/ src/openport/server/static/
RUN uv sync --frozen --no-dev --no-editable
COPY examples/ examples/
COPY --from=dashboard /app/examples/supportdesk-ts/node_modules/ examples/supportdesk-ts/node_modules/
ENV PATH="/app/.venv/bin:$PATH" OPENPORT_RUNS_DIR=/app/runs OPENPORT_EXAMPLES=/app/examples
# A public dashboard runs repo code only for its demo apps and pinned eval packs: testing a call imports the
# repo into this process, next to the API keys. Set it to "any" for a private deployment you trust with any repo.
ENV OPENPORT_RUN_REPO_CODE=vetted
EXPOSE 8000
CMD ["openport", "serve", "--host", "0.0.0.0", "--port", "8000"]
