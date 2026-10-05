import httpx
import pytest
from fastapi.testclient import TestClient

from incident_response import context, main, store

TRACE_ID = "0c60ad507b21628e931d8ac4e8a05c6a"
ROUTE = "/api/orders/{order_id}"


def alert(status="firing", **overrides):
    return {
        "status": status,
        "labels": {"alertname": "Order Tracker 5xx responses", "service": "order-tracker",
                   "http_request_method": "GET", "http_route": ROUTE},
        "annotations": {
            "summary": "5xx responses from GET /api/orders/{order_id} in the last 5 minutes: 3",
            "endpoint": "GET /api/orders/{order_id}",
            "window": "5m",
            "dashboard_url": "http://127.0.0.1:3000/d/order-tracker/order-tracker",
        },
        "startsAt": "2026-10-05T14:15:40Z",
        "endsAt": "2026-10-05T14:27:10Z" if status == "resolved" else "0001-01-01T00:00:00Z",
        "fingerprint": "a1b2c3d4e5f60708",
        "generatorURL": "http://127.0.0.1:3000/alerting/grafana/order-tracker-5xx/view",
        "panelURL": "http://127.0.0.1:3000/d/order-tracker/order-tracker?viewPanel=6",
        "values": {"B": 3, "C": 1},
        **overrides,
    }


def notification(*alerts):
    return {"receiver": "incident-response", "status": alerts[0]["status"], "alerts": list(alerts)}


def backends(request: httpx.Request) -> httpx.Response:
    path, params = request.url.path, request.url.params
    if path == "/api/v1/query":
        assert 'http_route="/api/orders/{order_id}"' in params["query"]
        return httpx.Response(200, json={"data": {"result": [
            {"metric": {"http_route": ROUTE, "http_request_method": "GET",
                        "http_response_status_code": "500", "error_type": "ValueError"},
             "value": [0, "3.02"]},
            {"metric": {"http_route": ROUTE, "http_request_method": "GET",
                        "http_response_status_code": "200"}, "value": [0, "9"]},
            {"metric": {"http_route": ROUTE, "http_request_method": "GET",
                        "http_response_status_code": "404"}, "value": [0, "0"]},
        ]}})
    if path == "/api/search":
        assert "span.http.response.status_code >= 500" in params["q"]
        # Tempo strips the leading zero from this trace ID.
        return httpx.Response(200, json={"traces": [
            {"traceID": TRACE_ID.lstrip("0"), "rootTraceName": "GET /api/orders/{order_id}",
             "startTimeUnixNano": "1791209116291000000", "durationMs": 5},
        ]})
    if path == f"/api/v2/traces/{TRACE_ID}":
        return httpx.Response(200, json={"trace": {"resourceSpans": [{"scopeSpans": [{"spans": [
            {"name": "order.lookup", "spanId": "b", "parentSpanId": "a",
             "startTimeUnixNano": "1791209116292000000", "endTimeUnixNano": "1791209116293000000",
             "status": {"code": "STATUS_CODE_ERROR", "message": "day is out of range for month"},
             "attributes": [{"key": "order.id", "value": {"stringValue": "express-1002"}}],
             "events": [{"name": "exception", "attributes": [
                 {"key": "exception.type", "value": {"stringValue": "ValueError"}}]}]},
            {"name": "GET /api/orders/{order_id}", "spanId": "a",
             "startTimeUnixNano": "1791209116291000000", "endTimeUnixNano": "1791209116296000000",
             "attributes": [{"key": "http.response.status_code", "value": {"intValue": "500"}}]},
        ]}]}]}})
    if path == "/loki/api/v1/query_range":
        assert TRACE_ID in params["query"]
        return httpx.Response(200, json={"data": {"result": [
            {"stream": {"service_name": "order-tracker", "severity_text": "ERROR", "trace_id": TRACE_ID,
                        "order_id": "express-1002", "exception_type": "ValueError",
                        "telemetry_sdk_name": "opentelemetry"},
             "values": [["1791209116292500000", "Order lookup failed"]]},
        ]}})
    return httpx.Response(404)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "incidents")
    monkeypatch.setattr(context, "transport", httpx.MockTransport(backends))
    with TestClient(main.app) as test_client:
        yield test_client


def test_firing_alert_records_endpoint_metrics_traces_and_logs(client):
    response = client.post("/alerts", json=notification(alert()))
    assert response.status_code == 202
    [received] = response.json()["incidents"]

    incident = client.get(f"/incidents/{received['id']}").json()
    assert incident["status"] == "firing"
    assert incident["endpoint"] == "GET /api/orders/{order_id}"
    assert incident["window"] == "5m"
    assert incident["links"]["dashboard"] == "http://127.0.0.1:3000/d/order-tracker/order-tracker"
    assert incident["context_status"] == "complete"

    collected = incident["context"]
    assert collected["endpoint"] == {"method": "GET", "route": ROUTE}
    assert collected["metrics"]["server_errors"] == 3
    assert collected["metrics"]["total_requests"] == 12
    assert collected["metrics"]["requests"][1]["error_type"] == "ValueError"
    [trace] = collected["traces"]
    assert trace["trace_id"] == TRACE_ID
    lookup = next(span for span in trace["spans"] if span["name"] == "order.lookup")
    assert lookup["status"] == "ERROR"
    assert lookup["attributes"]["order.id"] == "express-1002"
    assert lookup["events"][0]["attributes"]["exception.type"] == "ValueError"
    [log] = collected["logs"]
    assert log["message"] == "Order lookup failed"
    assert log["order_id"] == "express-1002"
    assert "telemetry_sdk_name" not in log


def test_repeat_and_resolved_notifications_update_the_same_incident(client):
    first = client.post("/alerts", json=notification(alert())).json()["incidents"][0]
    client.post("/alerts", json=notification(alert()))
    resolved = client.post("/alerts", json=notification(alert("resolved"))).json()["incidents"][0]

    assert resolved["id"] == first["id"]
    assert len(client.get("/incidents").json()) == 1
    incident = client.get(f"/incidents/{first['id']}").json()
    assert incident["status"] == "resolved"
    assert incident["resolved_at"].startswith("2026-10-05T14:27:10")
    assert incident["notifications"] == 3


def test_unreachable_backend_still_saves_the_incident(client, monkeypatch):
    def loki_down(request):
        if request.url.path.startswith("/loki"):
            raise httpx.ConnectError("connection refused")
        return backends(request)

    monkeypatch.setattr(context, "transport", httpx.MockTransport(loki_down))
    incident_id = client.post("/alerts", json=notification(alert())).json()["incidents"][0]["id"]

    incident = client.get(f"/incidents/{incident_id}").json()
    assert incident["context_status"] == "partial"
    assert "ConnectError" in incident["context"]["errors"]["logs"]
    assert len(incident["context"]["traces"]) == 1


def test_unknown_or_invalid_incident_ids_return_404(client):
    assert client.get("/incidents/does-not-exist").status_code == 404
    assert client.get("/incidents/..%2F..%2Fetc%2Fpasswd").status_code == 404
