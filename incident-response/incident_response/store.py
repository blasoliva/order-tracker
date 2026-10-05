"""Incidents are stored as one JSON file each, so they can be read without any tooling."""

import json
import os
import re
from pathlib import Path

DATA_DIR = Path(os.getenv("INCIDENT_DATA_DIR", "data/incidents"))
VALID_ID = re.compile(r"^[0-9A-Za-z-]+$")


def _path(incident_id: str) -> Path:
    if not VALID_ID.match(incident_id):
        raise ValueError(f"Invalid incident id: {incident_id!r}")
    return DATA_DIR / f"{incident_id}.json"


def get(incident_id: str) -> dict | None:
    try:
        return json.loads(_path(incident_id).read_text())
    except FileNotFoundError:
        return None


def save(incident: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(incident["id"])
    # Write to a temporary file and rename, so readers never see a half-written incident.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(incident, indent=2, default=str))
    os.replace(tmp, path)


def all_incidents() -> list[dict]:
    if not DATA_DIR.exists():
        return []
    incidents = [json.loads(path.read_text()) for path in DATA_DIR.glob("*.json")]
    return sorted(incidents, key=lambda incident: incident["received_at"], reverse=True)
