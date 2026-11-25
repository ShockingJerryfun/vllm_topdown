"""Compatibility exports for existing Topdown consumers."""

from analyze.pmu.topdown.probe.profiles.zen4 import (
    PERCENT_METRICS as PERCENT_METRICS,
    SUMMARY_LEVELS as SUMMARY_LEVELS,
    UNAVAILABLE as UNAVAILABLE,
    FREQUENCY_LABEL as FREQUENCY_LABEL,
    FREQUENCY_NOTE as FREQUENCY_NOTE,
    REPORT_NOTE as REPORT_NOTE,
    read_rows as read_rows,
    count as count,
    divide as divide,
    scale as scale,
    subtract as subtract,
    average as average,
    slots_ratio as slots_ratio,
    frequency_mhz as frequency_mhz,
    stage_metrics as stage_metrics,
)
