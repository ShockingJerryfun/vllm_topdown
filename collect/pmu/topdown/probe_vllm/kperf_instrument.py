"""Compatibility shim for the existing instrumented vLLM source."""

from topdown.probe import (
    configure,
    kperf_begin,
    kperf_finish,
    kperf_span_begin,
    kperf_span_finish,
)

__all__ = [
    "configure", "kperf_begin", "kperf_finish", "kperf_span_begin", "kperf_span_finish"
]
