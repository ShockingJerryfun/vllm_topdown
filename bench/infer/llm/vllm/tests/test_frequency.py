# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "frequency", Path(__file__).parents[2] / "scripts/frequency.py"
)
assert spec is not None and spec.loader is not None
frequency = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frequency)


def test_select_worker_cores_and_local_uncore_from_final_table():
    report = """Per NUMA Frequency Table
| 0 | 2800 | 2400 |
| 3 | 2900 | 2500 |
CPU Core Frequency Table
| 252 | 126 | 3 | 2800 |
| 254 | 127 | 3 | 2900 |
Per NUMA Frequency Table
| 0 | 2800 | 2400 |
| 3 | 2900 | 2700 |
CPU Core Frequency Table
| 250 | 125 | 3 | 2100 |
| 252 | 126 | 3 | 2900 |
| 254 | 127 | 3 | 3000 |
Uncore Devices Table
| 3 | 0 | 0 | 1000 |
"""
    result = frequency.parse_report(report, [252, 254], 3)
    assert result["core_mhz"] == 2950
    assert result["uncore_mhz"] == 2700
    with pytest.raises(ValueError, match="selected Worker"):
        frequency.parse_report(report, [255], 3)


def test_unavailable_rows_do_not_abort_valid_frequency_report():
    report = """Per NUMA Frequency Table
| 3 | 2900 | N/A |
CPU Core Frequency Table
| 252 | 126 | 3 | 2300 |
| 254 | 127 | 3 | N/A |
"""
    result = frequency.parse_report(report, [252, 254], 3)
    assert result["core_mhz"] == 2300
    assert result["per_core_mhz"] == {252: 2300}
    assert result["uncore_mhz"] is None
