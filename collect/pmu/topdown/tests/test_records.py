"""Parsing does not require vLLM stage names."""

from pathlib import Path

import pytest

from topdown.records import parse_log


def test_arbitrary_region_and_multiplexing_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "measure.log"
    path.write_text("unrelated\nKPERF,worker.custom,2,100,80,0,7,9\n")
    row = parse_log(path, ["cycles", "instructions"], "pmu")["worker.custom"][0]
    assert row["time_enabled"] == 100 and row["time_running"] == 80
    assert row["valid"] == 0 and row["instructions"] == 9


def test_bad_event_count_is_not_accepted(tmp_path: Path) -> None:
    path = tmp_path / "measure.log"
    path.write_text("KPERF,custom,1,100,100,1,7\n")
    with pytest.raises(ValueError, match="Counter count mismatch"):
        parse_log(path, ["cycles", "instructions"], "pmu")
