"""Queries Prometheus, Tempo and Loki for what was happening around an alert."""

import base64
import binascii
import json
import os
import re
from datetime import datetime, timedelta, timezone

import httpx

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
TEMPO_URL = os.getenv("TEMPO_URL", "http://localhost:3200")
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")

REQUEST_COUNT = "http_server_request_duration_seconds_count"
MAX_TRACES = 20
MAX_TRACE_DETAILS = 5
MAX_LOGS = 100
# Loki stream fields that are the same on every record and only add noise.
IGNORED_LOG_FIELDS = {
    "detected_level", "flags", "observed_timestamp", "scope_version", "service_instance_id",
    "service_name", "telemetry_sdk_language", "telemetry_sdk_name", "telemetry_sdk_version",
}

# Tests replace this with an httpx.MockTransport.
transport: httpx.AsyncBaseTransport | None = None


def parse_duration(value: str, default: timedelta = timedelta(minutes=5)) -> timedelta:
    match = re.fullmatch(r"(\d+)([smhd])", value.strip()) if value else None
    if not match:
        return default
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[match[2]]
    return timedelta(**{unit: int(match[1])})


def quote(value: str) -> str:
    """A double-quoted string literal that PromQL, TraceQL and LogQL all accept."""
    return json.dumps(value)


def time_range(alert) -> tuple[datetime, datetime]:
    """From one alert window (plus a minute of slack) before the alert started, until now."""
    window = parse_duration(alert.annotations.get("window", ""))
    start = alert.startsAt - window - timedelta(minutes=1)
    end = datetime.now(timezone.utc)
    if alert.status == "resolved" and alert.endsAt and alert.endsAt.year > 1:
        end = alert.endsAt
    return start, end


async def collect(alert) -> dict:
    """Gather metrics, traces and logs for an alert. A failing source is recorded, not raised."""
    service = alert.labels.get("service", "order-tracker")
    method = alert.labels.get("http_request_method")
    route = alert.labels.get("http_route")
    start, end = time_range(alert)
    context = {
        "service": service,
        "endpoint": {"method": method, "route": route} if route else None,
        "time_range": {"start": start.isoformat(), "end": end.isoformat()},
        "metrics": None,
        "traces": [],
        "logs": [],
        "queries": {},
        "errors": {},
    }
    async with httpx.AsyncClient(timeout=10, transport=transport) as client:
        for name, step in (("metrics", _metrics), ("traces", _traces), ("logs", _logs)):
            try:
                await step(client, context, service, method, route, start, end)
            except Exception as error:
                context["errors"][name] = f"{type(error).__name__}: {error}"
    return context


async def _metrics(client, context, service, method, route, start, end):
    selector = f"job={quote(service)}"
    if route:
        selector += f", http_route={quote(route)}"
    if method:
        selector += f", http_request_method={quote(method)}"
    seconds = max(60, int((end - start).total_seconds()))
    query = (
        f"sum by (http_route, http_request_method, http_response_status_code, error_type) "
        f"(increase({REQUEST_COUNT}{{{selector}}}[{seconds}s]))"
    )
    context["queries"]["prometheus"] = query
    response = await client.get(
        f"{PROMETHEUS_URL}/api/v1/query", params={"query": query, "time": end.timestamp()}
    )
    response.raise_for_status()
    requests = [
        {
            "method": result["metric"].get("http_request_method"),
            "route": result["metric"].get("http_route"),
            "status_code": result["metric"].get("http_response_status_code"),
            "error_type": result["metric"].get("error_type"),
            "count": round(float(result["value"][1])),
        }
        for result in response.json()["data"]["result"]
    ]
    requests = [request for request in requests if request["count"] > 0]
    total = sum(request["count"] for request in requests)
    errors = sum(request["count"] for request in requests if request["status_code"].startswith("5"))
    context["metrics"] = {
        "requests": sorted(requests, key=lambda request: -request["count"]),
        "total_requests": total,
        "server_errors": errors,
        "server_error_rate": round(errors / total, 4) if total else None,
    }


async def _traces(client, context, service, method, route, start, end):
    conditions = [f"resource.service.name={quote(service)}"]
    if route:
        conditions.append(f"span.http.route={quote(route)}")
        if method:
            conditions.append(f"span.http.request.method={quote(method)}")
        conditions.append("span.http.response.status_code >= 500")
    else:
        conditions.append("status=error")
    query = "{" + " && ".join(conditions) + "}"
    context["queries"]["tempo"] = query
    response = await client.get(
        f"{TEMPO_URL}/api/search",
        params={"q": query, "start": int(start.timestamp()), "end": int(end.timestamp()) + 1,
                "limit": MAX_TRACES},
    )
    response.raise_for_status()
    found = sorted(response.json().get("traces", []),
                   key=lambda found_trace: int(found_trace["startTimeUnixNano"]), reverse=True)
    for index, found_trace in enumerate(found):
        trace = {
            # Tempo drops leading zeros from trace IDs; logs keep all 32 hex digits.
            "trace_id": found_trace["traceID"].zfill(32),
            "name": found_trace.get("rootTraceName"),
            "start": _from_nanos(found_trace["startTimeUnixNano"]),
            "duration_ms": found_trace.get("durationMs"),
        }
        if index < MAX_TRACE_DETAILS:
            detail = await client.get(f"{TEMPO_URL}/api/v2/traces/{trace['trace_id']}")
            detail.raise_for_status()
            trace["spans"] = _spans(detail.json())
        context["traces"].append(trace)


def _spans(body: dict) -> list[dict]:
    data = body.get("trace", body)
    spans = []
    for resource_spans in data.get("resourceSpans", []):
        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                status = span.get("status", {})
                spans.append({
                    "name": span.get("name"),
                    "span_id": _hex_id(span.get("spanId")),
                    "parent_span_id": _hex_id(span.get("parentSpanId")),
                    "start": _from_nanos(span.get("startTimeUnixNano", 0)),
                    "duration_ms": round(
                        (int(span.get("endTimeUnixNano", 0)) - int(span.get("startTimeUnixNano", 0))) / 1e6, 3
                    ),
                    "status": status.get("code", "STATUS_CODE_UNSET").removeprefix("STATUS_CODE_"),
                    "status_message": status.get("message"),
                    "attributes": _attributes(span.get("attributes", [])),
                    "events": [
                        {"name": event.get("name"), "attributes": _attributes(event.get("attributes", []))}
                        for event in span.get("events", [])
                    ],
                })
    return sorted(spans, key=lambda span: span["start"])


def _hex_id(value: str | None) -> str | None:
    """Tempo returns span IDs base64-encoded; logs carry them as hex, so convert to match."""
    if not value:
        return None
    try:
        return base64.b64decode(value, validate=True).hex()
    except (binascii.Error, ValueError):
        return value


def _attributes(attributes: list[dict]) -> dict:
    # OTLP JSON wraps each value by type, e.g. {"stringValue": "GET"} or {"intValue": "500"}.
    return {item["key"]: next(iter(item.get("value", {}).values()), None) for item in attributes}


async def _logs(client, context, service, method, route, start, end):
    trace_ids = [trace["trace_id"] for trace in context["traces"]]
    if trace_ids:
        # Every log line from the failed requests, including the ones leading up to the error.
        query = f'{{service_name={quote(service)}}} | trace_id=~{quote("|".join(trace_ids))}'
    else:
        query = f'{{service_name={quote(service)}}} | severity_text=~"ERROR|WARN"'
    context["queries"]["loki"] = query
    response = await client.get(
        f"{LOKI_URL}/loki/api/v1/query_range",
        params={"query": query, "start": int(start.timestamp() * 1e9),
                "end": int(end.timestamp() * 1e9) + 1, "limit": MAX_LOGS, "direction": "backward"},
    )
    response.raise_for_status()
    logs = []
    for stream in response.json()["data"]["result"]:
        fields = {key: value for key, value in stream["stream"].items() if key not in IGNORED_LOG_FIELDS}
        for timestamp, line in stream["values"]:
            logs.append({"timestamp": _from_nanos(timestamp), "message": line, **fields})
    context["logs"] = sorted(logs, key=lambda log: log["timestamp"])


def _from_nanos(nanos) -> str:
    return datetime.fromtimestamp(int(nanos) / 1e9, timezone.utc).isoformat()
