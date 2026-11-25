"""Compatibility exports for existing Topdown consumers."""

from analyze.pmu.topdown.probe.profiles.kunpeng920b import (
    CYCLES_SHARE_METRIC as CYCLES_SHARE_METRIC,
    PERCENT_METRICS as PERCENT_METRICS,
    read_rows as read_rows,
    mean as mean,
    total as total,
    ratio as ratio,
    add as add,
    multiply as multiply,
    residual as residual,
    residual_ratio as residual_ratio,
    average_frequency_mhz as average_frequency_mhz,
    latency_proxy as latency_proxy,
    l2_source_metrics as l2_source_metrics,
    stage_metrics as stage_metrics,
)
