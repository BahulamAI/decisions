"""OpenTelemetry tracing (D13). Django sends a W3C `traceparent`; every span here continues that trace, and every
log line carries its trace id, so one question can be followed from the chat through Django to this service.

Spans are exported only when DECISION_OTEL_EXPORTER_ENDPOINT names an OTLP/HTTP collector; without one, ids are
still created and logged. No trace context is ever sent on to OpenRouter: a third party gets no internal ids.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_configured = False


def configure_tracing(service_name: str, endpoint: str | None) -> None:
    """Install one tracer provider per process; later calls (tests build many apps) keep the first."""
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=2)))
    trace.set_tracer_provider(provider)
    _configured = True


tracer = trace.get_tracer("decision_service")
