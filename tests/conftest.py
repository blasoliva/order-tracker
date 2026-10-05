import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import telemetry

# Installed before app.main is imported so the app skips its console exporters.
spans = InMemorySpanExporter()
metric_reader = InMemoryMetricReader()
logs = InMemoryLogRecordExporter()
telemetry.configure(span_exporter=spans, metric_reader=metric_reader, log_exporter=logs)


@pytest.fixture
def telemetry_data():
    spans.clear()
    logs.clear()
    return spans, metric_reader, logs
