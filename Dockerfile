FROM python:3.12-slim
WORKDIR /app
RUN pip install uv

# Install third-party dependencies first so this layer stays cached across
# source-only changes (uv can't install the local package yet without src/).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Only the router's own package — src/hermes/ (MCP/plugin templates seeded
# into rooms at runtime, see docker-compose.yml's hermes-templates mount) is
# irrelevant to the router image and would only bloat it / bust this layer's
# cache on every unrelated template edit.
COPY src/alice_office_router/ ./src/alice_office_router/
RUN uv sync --frozen --no-dev

# --no-sync: the venv is already fully synced at build time: starting the
# app must never depend on network access to PyPI.
CMD ["uv", "run", "--no-sync", "uvicorn", "alice_office_router.main:app", "--host", "0.0.0.0", "--port", "8000"]
