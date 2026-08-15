# Stage 1 — compile the React dashboard to static assets. The FastAPI
# service serves the same bundle at /app (live) and /demo (seeded judge
# mode), so one Cloud Run service carries the whole product (plan §7.3).
FROM node:22-slim AS webbuild
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2 — the Python service.
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock README.md ./
COPY backend ./backend
RUN uv sync --frozen --no-dev

COPY --from=webbuild /web/dist ./frontend/dist

ENV PATH="/app/.venv/bin:$PATH"

CMD ["sh", "-c", "uvicorn commitmentos.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8080}"]
