FROM python:3.12-slim
WORKDIR /app
RUN pip install uv
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev
COPY src/ ./src/
CMD ["uv", "run", "uvicorn", "alice_office_router.main:app", "--host", "0.0.0.0", "--port", "8000"]
