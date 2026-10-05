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

All configuration is in `observability/`. Telemetry data is kept in Docker volumes (Prometheus keeps 15 days, Loki 7 days). Without Docker, for example under `uv run uvicorn`, the app prints telemetry to the console instead.

## Incident response

`incident-response/` is a separate service on port 8001 (set `INCIDENT_RESPONSE_PORT` to change it). Grafana posts alert notifications to its `POST /alerts` endpoint. For each new alert it saves the alert right away, then collects the context in the background:

- **Metrics** from Prometheus: requests to the affected endpoint by status code and error type, with the error rate.
- **Traces** from Tempo: the failed requests to that endpoint (up to 20), with full span details and exceptions for the 5 most recent.
- **Logs** from Loki: every log line from those failed requests, linked by trace ID.

The time range runs from one alert window before the alert started until it was received. Repeat and resolved notifications update the same incident. If a backend can't be reached, the incident is still saved, marked `partial`, and records the error.

Incidents are JSON files in the `incidents` Docker volume. Run the service's tests with `cd incident-response && uv run --frozen pytest -q`.

### Incident response API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/alerts` | Receive a Grafana alert notification |
| GET | `/incidents` | List incidents, newest first |
| GET | `/incidents/{id}` | Get an incident with its collected context |
| GET | `/healthz` | Health check |

#### Receive an alert

Grafana calls this endpoint; you only need to call it yourself for testing. The body is Grafana's webhook payload. The service uses `status` and, for each alert, `labels`, `annotations`, `startsAt` and `fingerprint` (all required), plus the links Grafana adds. The `http_route` and `http_request_method` labels name the affected endpoint, and the `window` annotation sets how far back to look.

```bash
curl -X POST http://127.0.0.1:8001/alerts \
  -H 'Content-Type: application/json' \
  -d '{
    "status": "firing",
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "Order Tracker 5xx responses",
        "service": "order-tracker",
        "http_request_method": "GET",
        "http_route": "/api/orders/{order_id}"
      },
      "annotations": {
        "summary": "5xx responses from GET /api/orders/{order_id} in the last 5 minutes: 3",
        "endpoint": "GET /api/orders/{order_id}",
        "window": "5m",
        "dashboard_url": "http://127.0.0.1:3000/d/order-tracker/order-tracker?from=now-30m&to=now"
      },
      "startsAt": "2026-10-05T14:20:30Z",
      "fingerprint": "8c2f4b1d9e7a6053",
      "values": {"B": 3, "C": 1}
    }]
  }'
```

`202 Accepted`. The incident is saved before the response; the context is collected right after it, usually within a second:

```json
{"incidents": [{"id": "20261005T142030Z-8c2f4b1d9e7a6053", "status": "firing"}]}
```

Sending the same alert again, or the `resolved` notification (`"status": "resolved"` with an `endsAt` time), updates that same incident instead of creating a new one:

```json
{"incidents": [{"id": "20261005T142030Z-8c2f4b1d9e7a6053", "status": "resolved"}]}
```

A payload without the required fields is rejected with `422 Unprocessable Entity`, and FastAPI's error lists each missing field (for example `{"type": "missing", "loc": ["body", "alerts", 0, "fingerprint"], "msg": "Field required", ...}`). Nothing is saved.

#### List incidents

```bash
curl http://127.0.0.1:8001/incidents
```

`200 OK`. A summary of each incident; `context_status` is `collecting`, `complete`, `partial` (a backend failed), `failed` or `skipped` (first seen already resolved):

```json
[
  {
    "id": "20261005T142030Z-8c2f4b1d9e7a6053",
    "status": "resolved",
    "title": "Order Tracker 5xx responses",
    "summary": "5xx responses from GET /api/orders/{order_id} in the last 5 minutes: 3",
    "endpoint": "GET /api/orders/{order_id}",
    "started_at": "2026-10-05T14:20:30+00:00",
    "resolved_at": "2026-10-05T14:27:10+00:00",
    "context_status": "complete"
  }
]
```

#### Get an incident

```bash
curl http://127.0.0.1:8001/incidents/20261005T142030Z-8c2f4b1d9e7a6053
```

`200 OK`. The full incident. This is the same incident, shortened: there were five failed traces with ten log lines, and the stack traces are left out. `context.queries` holds the exact Prometheus, TraceQL and LogQL queries, so you can rerun them in Grafana. Log lines carry the `trace_id` and `span_id` of the span that wrote them; here the first log line comes from the `order.lookup` span.

```json
{
  "id": "20261005T142030Z-8c2f4b1d9e7a6053",
  "status": "resolved",
  "title": "Order Tracker 5xx responses",
  "endpoint": "GET /api/orders/{order_id}",
  "window": "5m",
  "started_at": "2026-10-05T14:20:30+00:00",
  "resolved_at": "2026-10-05T14:27:10+00:00",
  "notifications": 2,
  "links": {
    "dashboard": "http://127.0.0.1:3000/d/order-tracker/order-tracker?from=now-30m&to=now",
    "panel": "http://127.0.0.1:3000/d/order-tracker/order-tracker?viewPanel=6",
    "alert_rule": "http://127.0.0.1:3000/alerting/grafana/order-tracker-5xx/view"
  },
  "context_status": "complete",
  "context": {
    "endpoint": {"method": "GET", "route": "/api/orders/{order_id}"},
    "time_range": {"start": "2026-10-05T14:14:30+00:00", "end": "2026-10-05T14:57:30.872823+00:00"},
    "metrics": {
      "requests": [
        {"method": "GET", "route": "/api/orders/{order_id}", "status_code": "404", "error_type": null, "count": 5},
        {"method": "GET", "route": "/api/orders/{order_id}", "status_code": "500", "error_type": "ValueError", "count": 5},
        {"method": "GET", "route": "/api/orders/{order_id}", "status_code": "200", "error_type": null, "count": 3}
      ],
      "total_requests": 13,
      "server_errors": 5,
      "server_error_rate": 0.3846
    },
    "traces": [
      {
        "trace_id": "60325d3cbb64553c96504f32f0410608",
        "name": "GET /api/orders/{order_id}",
        "start": "2026-10-05T14:22:06.856937+00:00",
        "duration_ms": 3,
        "spans": [
          {
            "name": "GET /api/orders/{order_id}",
            "span_id": "add210e9ae02fe06",
            "status": "ERROR",
            "attributes": {"http.response.status_code": "500", "error.type": "ValueError", "url.path": "/api/orders/express-1002"}
          },
          {
            "name": "order.lookup",
            "span_id": "ce0a97bc0f8b7c4b",
            "parent_span_id": "34a900e1c8f3c0ce",
            "status": "ERROR",
            "status_message": "day is out of range for month",
            "attributes": {"order.id": "express-1002", "order.found": true, "order.priority": "express"},
            "events": [{"name": "exception", "attributes": {"exception.type": "ValueError", "exception.message": "day is out of range for month"}}]
          }
        ]
      }
    ],
    "logs": [
      {
        "timestamp": "2026-10-05T14:22:06.858559+00:00",
        "message": "Order lookup failed",
        "severity_text": "ERROR",
        "order_id": "express-1002",
        "exception_type": "ValueError",
        "exception_message": "day is out of range for month",
        "trace_id": "60325d3cbb64553c96504f32f0410608",
        "span_id": "ce0a97bc0f8b7c4b"
      }
    ],
    "queries": {
      "prometheus": "sum by (http_route, http_request_method, http_response_status_code, error_type) (increase(http_server_request_duration_seconds_count{job=\"order-tracker\", http_route=\"/api/orders/{order_id}\", http_request_method=\"GET\"}[2580s]))",
      "tempo": "{resource.service.name=\"order-tracker\" && span.http.route=\"/api/orders/{order_id}\" && span.http.request.method=\"GET\" && span.http.response.status_code >= 500}",
      "loki": "{service_name=\"order-tracker\"} | trace_id=~\"60325d3cbb64553c96504f32f0410608|...\""
    },
    "errors": {}
  }
}
```

An unknown ID returns `404 Not Found` with `{"detail": "Incident not found"}`.

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

### Examples

All request and response bodies are JSON. Errors have a `detail` field. It is a message for errors the app raises itself, and a list of field errors when the body doesn't match the expected shape.

#### Check the service

```bash
curl http://127.0.0.1:8000/healthz
```

`200 OK`. The check runs a query against the database, so a failure there returns `500` instead:

```json
{"status": "ok"}
```

#### List orders

```bash
curl http://127.0.0.1:8000/api/orders
```

`200 OK`. All orders, newest first:

```json
[
  {"id": "standard-1001", "customer": "Avery", "item": "Notebook", "priority": "standard", "status": "received", "created_at": "2026-10-05T14:56:11.885751+00:00"},
  {"id": "standard-1003", "customer": "Riley", "item": "Water bottle", "priority": "standard", "status": "shipped", "created_at": "2026-10-05T14:56:11.885751+00:00"},
  {"id": "express-1002", "customer": "Sam", "item": "Headphones", "priority": "express", "status": "preparing", "created_at": "2026-09-30T14:56:11.885751+00:00"}
]
```

#### Check an order

```bash
curl http://127.0.0.1:8000/api/orders/express-1002
```

`200 OK`. Express orders also include `estimated_delivery`, two days after the order was placed:

```json
{"id": "express-1002", "customer": "Sam", "item": "Headphones", "priority": "express", "status": "preparing", "created_at": "2026-09-30T14:56:11.885751+00:00", "estimated_delivery": "2026-10-02"}
```

An ID that doesn't exist, such as `standard-1002`, returns `404 Not Found`:

```json
{"detail": "Order not found"}
```

#### Create an order

```bash
curl -X POST http://127.0.0.1:8000/api/orders \
  -H 'Content-Type: application/json' \
  -d '{"customer": "Taylor", "item": "Mug", "priority": "express"}'
```

`201 Created`. The new order gets a generated ID and starts as `received`. `priority` is optional and defaults to `standard`:

```json
{"id": "57e7c783-82d0-4a95-a73f-60ad37e8fb44", "customer": "Taylor", "item": "Mug", "priority": "express", "status": "received", "created_at": "2026-10-05T14:56:15.318145+00:00", "estimated_delivery": "2026-10-07"}
```

A priority other than `standard` or `express` returns `422 Unprocessable Entity`:

```json
{"detail": "Priority must be standard or express"}
```

An empty or too long `customer` (1–80 characters) or `item` (1–120) also returns `422`, with one entry per invalid field:

```json
{"detail": [{"type": "string_too_short", "loc": ["body", "customer"], "msg": "String should have at least 1 character", "input": "", "ctx": {"min_length": 1}}]}
```

#### Change an order status

```bash
curl -X PATCH http://127.0.0.1:8000/api/orders/standard-1001 \
  -H 'Content-Type: application/json' \
  -d '{"status": "shipped"}'
```

`200 OK`. The updated order:

```json
{"id": "standard-1001", "customer": "Avery", "item": "Notebook", "priority": "standard", "status": "shipped", "created_at": "2026-10-05T14:56:11.885751+00:00"}
```

The status must be `received`, `preparing`, `shipped` or `delivered`. Anything else returns `422` with `{"detail": "Invalid status"}`, and an unknown order returns `404` with `{"detail": "Order not found"}`.

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.
