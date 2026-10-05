FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY incident_response ./incident_response
ENV INCIDENT_DATA_DIR=/data/incidents
EXPOSE 8001
CMD ["uv", "run", "--no-sync", "uvicorn", "incident_response.main:app", "--host", "0.0.0.0", "--port", "8001"]
