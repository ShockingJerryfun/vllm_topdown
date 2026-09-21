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
    with pytest.raises(KeyError):
        frequency.parse_report(report, [255], 3)
