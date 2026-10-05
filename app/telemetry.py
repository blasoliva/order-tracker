import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor


SERVICE_NAME = "order-tracker"
logger = logging.getLogger("order_tracker")


def default_exporters():
    """OTLP when OTEL_EXPORTER_OTLP_ENDPOINT is set (as in Docker Compose), otherwise the console."""
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        # The exporters read the endpoint and other OTEL_EXPORTER_OTLP_* settings from the environment.
        return (
            BatchSpanProcessor(OTLPSpanExporter()),
            PeriodicExportingMetricReader(OTLPMetricExporter()),
            BatchLogRecordProcessor(OTLPLogExporter()),
        )
    return (
        SimpleSpanProcessor(ConsoleSpanExporter()),
        PeriodicExportingMetricReader(ConsoleMetricExporter()),
        SimpleLogRecordProcessor(ConsoleLogExporter()),
    )


def configure(span_exporter=None, metric_reader=None, log_exporter=None):
    """Install SDK providers. Tests pass in-memory exporters; otherwise see default_exporters()."""
    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", SERVICE_NAME)})
    if span_exporter or metric_reader or log_exporter:
        span_processor = SimpleSpanProcessor(span_exporter)
        log_processor = SimpleLogRecordProcessor(log_exporter)
    else:
        span_processor, metric_reader, log_processor = default_exporters()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(span_processor)
    trace.set_tracer_provider(tracer_provider)

    # The export interval honors OTEL_METRIC_EXPORT_INTERVAL (milliseconds).
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(log_processor)
    set_logger_provider(logger_provider)
    logger.addHandler(LoggingHandler(logger_provider=logger_provider))
    logger.setLevel(logging.INFO)


def configure_once():
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    configure()
