"""Structured JSON logs (with trace ids) and Prometheus metrics through prometheus_client (D13).
Tracing lives in tracing.py; both keep the call sites and metric names the service always had."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from contextvars import ContextVar
from typing import Any

from opentelemetry import trace
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest, multiprocess

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Worker metrics are files in this folder. The image's start command empties it; creating it here as well means a
# missing folder can never turn every request into an error.
if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
    os.makedirs(os.environ["PROMETHEUS_MULTIPROC_DIR"], exist_ok=True)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": round(time.time(), 3), "level": record.levelname, "logger": record.name,
               "msg": record.getMessage(), "request_id": request_id_var.get()}
        span = trace.get_current_span().get_span_context()
        if span.is_valid:  # ties this log line to the trace Django started
            out["trace_id"] = f"{span.trace_id:032x}"
        out.update(getattr(record, "extra_fields", {}))
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def configure_logging(level: str) -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)


def log(logger: logging.Logger, msg: str, **fields: object) -> None:
    logger.info(msg, extra={"extra_fields": fields})


_BUCKETS = (5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000)


class Metrics:
    """prometheus_client behind the original call sites: METRICS.inc / observe / render keep their names, and the
    exported series keep theirs (<name>_total, <name>_bucket/_count/_sum).

    With several workers in one container each process keeps its own values, so set PROMETHEUS_MULTIPROC_DIR
    (the image does) and /metrics merges them. Metrics are created on first use with that call's label names.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.registry = CollectorRegistry(auto_describe=True)
        self._metrics: dict[str, Counter | Histogram] = {}

    def _get(self, kind: type, name: str, labels: tuple[str, ...]) -> Any:
        with self._lock:
            m = self._metrics.get(name)
            if m is None:
                extra = {"buckets": _BUCKETS} if kind is Histogram else {}
                m = kind(name, name.replace("_", " "), labels, registry=self.registry, **extra)
                self._metrics[name] = m
            return m

    def inc(self, name: str, **labels: str) -> None:
        c = self._get(Counter, name, tuple(sorted(labels)))
        (c.labels(**labels) if labels else c).inc()

    def observe(self, name: str, value_ms: float, **labels: str) -> None:
        h = self._get(Histogram, name, tuple(sorted(labels)))
        (h.labels(**labels) if labels else h).observe(value_ms)

    def render(self) -> str:
        if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
            merged = CollectorRegistry()
            multiprocess.MultiProcessCollector(merged)  # every worker's files, summed
            return generate_latest(merged).decode()
        return generate_latest(self.registry).decode()


METRICS = Metrics()
