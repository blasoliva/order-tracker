from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from incident_response import context, store


class GrafanaAlert(BaseModel):
    """One alert from a Grafana webhook notification. Unknown fields are kept."""

    model_config = ConfigDict(extra="allow")

    status: str
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    startsAt: datetime
    endsAt: datetime | None = None
    fingerprint: str
    generatorURL: str = ""
    dashboardURL: str = ""
    panelURL: str = ""
    values: dict[str, float] | None = None


class GrafanaNotification(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    alerts: list[GrafanaAlert]


app = FastAPI(title="Incident Response")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def incident_id(alert: GrafanaAlert) -> str:
    # Grafana keeps the fingerprint and start time for the life of an alert, so repeat and
    # resolved notifications for the same problem map to the same incident.
    return f"{alert.startsAt.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-{alert.fingerprint}"


def new_incident(alert: GrafanaAlert) -> dict:
    return {
        "id": incident_id(alert),
        "status": alert.status,
        "title": alert.labels.get("alertname"),
        "summary": alert.annotations.get("summary"),
        "description": alert.annotations.get("description"),
        "endpoint": alert.annotations.get("endpoint"),
        "window": alert.annotations.get("window"),
        "started_at": alert.startsAt.isoformat(),
        "resolved_at": None,
        "received_at": now(),
        "notifications": 0,
        "links": {
            "dashboard": alert.annotations.get("dashboard_url") or alert.dashboardURL or None,
            "panel": alert.panelURL or None,
            "alert_rule": alert.generatorURL or None,
        },
        "values": alert.values,
        "labels": alert.labels,
        "context_status": "pending",
        "context": None,
        "alert": alert.model_dump(mode="json"),
    }


async def collect_context(incident_id: str, alert: GrafanaAlert) -> None:
    try:
        collected = await context.collect(alert)
        context_status = "partial" if collected["errors"] else "complete"
    except Exception as error:
        collected = {"errors": {"collector": f"{type(error).__name__}: {error}"}}
        context_status = "failed"
    incident = store.get(incident_id)
    incident.update(context=collected, context_status=context_status, context_collected_at=now())
    store.save(incident)


@app.post("/alerts", status_code=202)
def receive_alerts(notification: GrafanaNotification, background_tasks: BackgroundTasks):
    """Record each alert straight away and collect the context after responding to Grafana."""
    received = []
    for alert in notification.alerts:
        incident = store.get(incident_id(alert))
        if incident is None:
            incident = new_incident(alert)
            if alert.status == "firing":
                incident["context_status"] = "collecting"
                background_tasks.add_task(collect_context, incident["id"], alert)
            else:
                incident["context_status"] = "skipped"
        incident["notifications"] += 1
        incident["last_notified_at"] = now()
        if alert.status == "firing":
            incident["status"] = "firing"
            incident["values"] = alert.values
            incident["summary"] = alert.annotations.get("summary", incident["summary"])
        else:
            incident["status"] = "resolved"
            incident["resolved_at"] = (alert.endsAt or datetime.now(timezone.utc)).isoformat()
        store.save(incident)
        received.append({"id": incident["id"], "status": incident["status"]})
    return {"incidents": received}


@app.get("/incidents")
def list_incidents():
    return [
        {key: incident.get(key) for key in (
            "id", "status", "title", "summary", "endpoint", "started_at", "resolved_at", "context_status",
        )}
        for incident in store.all_incidents()
    ]


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    try:
        incident = store.get(incident_id)
    except ValueError:
        incident = None
    if incident is None:
        raise HTTPException(404, "Incident not found")
    return incident


@app.get("/healthz")
def health():
    return {"status": "ok"}
