# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

## Telemetry

Docker Compose also starts an observability stack. The app sends metrics, logs and traces over OTLP to an OpenTelemetry Collector, which forwards them to storage:

| Signal | Storage | What the app sends |
| --- | --- | --- |
| Metrics | Prometheus | `http.server.request.duration` for every request, with `http.route`, `http.response.status_code` and, for 5xx, `error.type` |
| Traces | Tempo | An HTTP server span per request, with an `order.lookup` child span for order lookups |
| Logs | Loki | A record per order lookup, plus unhandled exceptions with stack traces, linked to traces by `trace_id` |

Open Grafana at <http://127.0.0.1:3000>. The **Order Tracker** dashboard shows request counts, errors by route and status, error logs and failed-request traces. You can browse without logging in; sign in as `admin` / `admin` to edit (set `GRAFANA_ADMIN_PASSWORD` to change it). If port 3000 is taken, set `GRAFANA_PORT`.

Grafana also has an **Order Tracker 5xx responses** alert (under Alerting → Alert rules). It checks every 30 seconds and fires as soon as any endpoint returns a 5xx in the last 5 minutes, with one alert per endpoint. The alert names the endpoint, the 5xx count and the window, and links to the dashboard. It resolves on its own once an endpoint has had no 5xx for 5 minutes. Grafana sends each alert notification to the incident-response service.

## Incident response

`incident-response/` is a separate service on port 8001 (set `INCIDENT_RESPONSE_PORT` to change it). Grafana posts alert notifications to its `POST /alerts` endpoint. For each new alert it saves the alert right away, then collects the context in the background:

- **Metrics** from Prometheus: requests to the affected endpoint by status code and error type, with the error rate.
- **Traces** from Tempo: the failed requests to that endpoint (up to 20), with full span details and exceptions for the 5 most recent.
- **Logs** from Loki: every log line from those failed requests, linked by trace ID.

The time range runs from one alert window before the alert started until it was received. Repeat and resolved notifications update the same incident. If a backend can't be reached, the incident is still saved, marked `partial`, and records the error.

Incidents are JSON files in the `incidents` Docker volume. Read them with `curl http://127.0.0.1:8001/incidents` and `curl http://127.0.0.1:8001/incidents/<id>`. Run the service's tests with `cd incident-response && uv run --frozen pytest -q`.

All configuration is in `observability/`. Telemetry data is kept in Docker volumes (Prometheus keeps 15 days, Loki 7 days). Without Docker, for example under `uv run uvicorn`, the app prints telemetry to the console instead.

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data and stored telemetry.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.
