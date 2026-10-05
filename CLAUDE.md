# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Context

A deliberately small FastAPI + SQLite order tracking app for the AI Dev Tools Zoomcamp observability homework (Homework 4: add telemetry, alerts, and an incident responder). The exercise is about detecting and handling incidents, not scaling: run only one app container at a time.

## Commands

```bash
uv run --frozen pytest -q                                   # all tests
uv run --frozen pytest -q tests/test_api.py::test_missing_order  # one test
uv run uvicorn app.main:app --reload                        # local server, DB at data/orders.db
docker compose up --build -d --wait                         # run in Docker on 127.0.0.1:8000
docker compose logs app                                     # app and uvicorn output (telemetry goes to Grafana)
(cd incident-response && uv run --frozen pytest -q)        # incident-response tests (separate uv project)
docker compose down                                         # stop; add -v to also delete order data
```

There is no linter or formatter configured. Add dependencies with `uv add`, because the Docker image installs with `uv sync --frozen` and fails if `uv.lock` is out of date. If port 8000 is taken, set `ORDER_TRACKER_PORT`.

## Architecture

- **`app/main.py`** holds the whole API: models, routes, and SQLite access. Each request opens a fresh connection with `connect()`, which reads the module-level `DB_PATH` at call time. Tests rely on this by monkeypatching `main.DB_PATH` to a temp file.
- **Seed data:** `init_db()` runs in the FastAPI lifespan and inserts three orders when the table is empty. `express-1002` is dated the last day of the previous month on purpose, so seed data exercises month-boundary date logic.
- **Express delivery estimates** are computed on read in `order_detail()`, not stored. `create_order` and `update_status` return their result by calling `get_order()`, so they go through the same lookup code (and produce `order.lookup` spans and logs).
- **`static/index.html`** is a single-file frontend with inline JS, served at `/`, and it calls the `/api/orders` endpoints.

## Telemetry

Signals flow: app → OTLP/HTTP → `otel-collector` → Prometheus (metrics, via its OTLP receiver), Loki (logs, via `/otlp`), and Tempo (traces). Grafana is provisioned with all three datasources (fixed UIDs `prometheus`, `loki`, `tempo`) and the dashboard in `observability/grafana/dashboards/order-tracker.json`. The dashboard is file-provisioned and read-only in the UI, so edit the JSON and restart Grafana.

- **Exporters:** `app/telemetry.py` uses OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (Compose sets it) and the console otherwise. Tests pass in-memory exporters to `telemetry.configure()` in `tests/conftest.py`, *before* `app.main` is imported. `main.py` calls `configure_once()`, which does nothing if a provider is already installed. Keep that import order, because OTel global providers can only be set once.
- **HTTP spans and the request metric come from FastAPI's built-in telemetry** (FastAPI ≥ 0.142), not app code. It must stay `FastAPI(telemetry={"auto_configure": False})`: with auto-configure on, FastAPI attaches its own OTLP exporters to our providers and every signal is exported twice. Don't add a second recorder of `http.server.request.duration` either; two attribute sets for one metric double-count in `sum()`.
- In Prometheus the metric is `http_server_request_duration_seconds_count`, with labels `http_route` (route template, never the raw path), `http_response_status_code`, `error_type` (5xx only) and `job="order-tracker"`. Prometheus runs with `created-timestamp-zero-ingestion` so `increase()` counts the first requests of a new series. Without it, a single 500 after a restart would read as zero.
- `get_order` creates an `order.lookup` span with automatic exception recording turned off. A 404 must not mark the span as an error; only unexpected exceptions set `ERROR` status. App logs go through the `order_tracker` logger, the only stdlib logger bridged to OTel. FastAPI separately logs unhandled exceptions, so a failed lookup produces two error records.
- **5xx alert:** `observability/grafana/provisioning/alerting/order-tracker-alerts.yaml`. Grafana does not expand `${ENV}` in alerting provisioning files, and it silently drops the top-level `dashboardUid`/`panelId` keys. Use the `{{ externalURL }}` template (already ends in `/`) and the `__dashboardUid__`/`__panelId__` annotations instead. `noDataState: OK` is deliberate: a route has no 5xx series until its first 5xx, so "no data" is the healthy state. The `5m` window appears in both the PromQL and the `window`/summary annotations; change them together. The same file provisions the `incident-response` webhook contact point and makes it the default notification policy, grouped by endpoint.
- Metrics export every 10 s (`OTEL_METRIC_EXPORT_INTERVAL`, in ms). The 5 s health check hits `/healthz`, which the dashboard filters out.
- To query backends from the host, go through Grafana's datasource proxy with **GET** requests (`curl -G`), e.g. `http://admin:admin@localhost:3000/api/datasources/proxy/uid/tempo/api/search?q=...`. A POST is rejected for Loki and silently drops the query for Tempo.

## Incident response service

`incident-response/` is its own uv project, image and Compose service (port 8001), with no shared code with `app/`. The root `.dockerignore` excludes it from the app image.

- `POST /alerts` takes Grafana's webhook payload. It saves the incident at once and collects context in a FastAPI background task, so Grafana gets a fast 202. An incident ID is `<startsAt>-<fingerprint>`, stable for the life of a Grafana alert, so repeat and resolved notifications update the same file. Context is collected only once, on the first firing notification.
- `incident_response/context.py` queries Prometheus, Tempo and Loki directly over the Compose network (not through Grafana). It reads the endpoint from the alert's `http_route`/`http_request_method` labels and the time range from the `window` annotation. Without those labels it falls back to service-wide error traces and logs. Each source fails on its own and is recorded in `context.errors`.
- Tempo search drops leading zeros from trace IDs, while Loki keeps all 32 hex digits. `context.py` pads them with `zfill(32)` before using them in the Loki query; keep that.
- Tests swap in an `httpx.MockTransport` through `context.transport`. Starlette's TestClient runs background tasks before returning, so tests can read the finished incident straight after the POST.
- To test delivery from Grafana: in Grafana 13 the old `/api/alertmanager/.../receivers/test` API returns 410. Use `POST /apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers/<base64 of contact point name>/test` with `{"integration": {...}, "alert": {"labels": ..., "annotations": ...}}`.
