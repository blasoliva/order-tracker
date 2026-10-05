import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_and_seeded_orders(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    orders = client.get("/api/orders").json()
    assert len(orders) == 3
    assert {order["priority"] for order in orders} == {"standard", "express"}


def test_create_and_update_order(client):
    response = client.post(
        "/api/orders",
        json={"customer": "Taylor", "item": "Mug", "priority": "standard"},
    )
    assert response.status_code == 201
    order_id = response.json()["id"]
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "received"
    updated = client.patch(f"/api/orders/{order_id}", json={"status": "shipped"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "shipped"


@pytest.mark.parametrize(
    ("created_at", "estimated_delivery"),
    [
        ("2026-03-10T09:00:00+00:00", "2026-03-12"),
        ("2026-09-30T09:00:00+00:00", "2026-10-02"),  # last day of a 30-day month
        ("2026-01-31T09:00:00+00:00", "2026-02-02"),  # last day of a 31-day month
        ("2026-12-31T23:00:00+00:00", "2027-01-02"),  # end of year
        ("2026-02-27T09:00:00+00:00", "2026-03-01"),  # second-to-last day of February
        ("2028-02-28T09:00:00+00:00", "2028-03-01"),  # leap year
    ],
)
def test_express_estimated_delivery_crosses_month_end(created_at, estimated_delivery):
    order = main.order_detail(
        {"id": "express-x", "customer": "Sam", "item": "Mug", "priority": "express",
         "status": "received", "created_at": created_at}
    )
    assert order["estimated_delivery"] == estimated_delivery


def test_seeded_express_order_from_month_end_opens(client):
    # The seed places express-1002 on the last day of the previous month.
    response = client.get("/api/orders/express-1002")
    assert response.status_code == 200
    assert "estimated_delivery" in response.json()


def test_missing_order(client):
    assert client.get("/api/orders/missing").status_code == 404


def request_points(metric_reader):
    points = []
    for resource_metrics in metric_reader.get_metrics_data().resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == "http.server.request.duration":
                    points.extend(dict(point.attributes) for point in metric.data.data_points)
    return points


def test_order_lookup_telemetry(client, telemetry_data):
    spans, metric_reader, logs = telemetry_data
    assert client.get("/api/orders/standard-1001").status_code == 200
    assert client.get("/api/orders/missing").status_code == 404

    lookups = [span for span in spans.get_finished_spans() if span.name == "order.lookup"]
    assert [span.attributes["order.found"] for span in lookups] == [True, False]
    assert all(span.status.is_ok for span in lookups)
    assert all(span.parent is not None for span in lookups)  # nested in FastAPI's server span

    messages = [record.log_record.body for record in logs.get_finished_logs()]
    assert messages == ["Order lookup succeeded", "Order not found"]
    assert all(record.log_record.trace_id for record in logs.get_finished_logs())

    points = request_points(metric_reader)
    for status_code in (200, 404):
        expected = {
            "http.request.method": "GET",
            "http.route": "/api/orders/{order_id}",
            "http.response.status_code": status_code,
        }
        assert any(expected.items() <= point.items() for point in points)
