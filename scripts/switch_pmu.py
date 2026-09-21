#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Drain requests and switch kperf through vLLM's existing Worker RPC."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


def post(url: str, payload: dict[str, object], timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def switch(
    base_url: str,
    round_id: str,
    identity_path: Path,
    stop: bool,
    timeout: float,
) -> dict[str, Any]:
    mode = "disabled" if stop else os.environ["KPERF_MODE"]
    kwargs = {"mode": mode, "round_id": round_id}
    if not stop:
        for key, env in (
            ("codes", "KPERF_RAW_EVENTS"),
            ("names", "KPERF_EVENT_NAMES"),
            ("scope", "KPERF_SCOPE"),
            ("pmu_name", "KPERF_PMU_NAME"),
            ("target", "KPERF_TARGET"),
            ("qualifier", "KPERF_QUALIFIER"),
        ):
            kwargs[key] = os.getenv(env, "thread" if key == "scope" else "")
    post(f"{base_url}/pause?mode=wait&clear_cache=false", {}, timeout)
    response = post(
        f"{base_url}/collective_rpc",
        {"method": "configure_kperf", "kwargs": kwargs, "timeout": timeout},
        timeout,
    )
    results = response["results"]
    if len(results) != 1:
        raise RuntimeError("This report requires one model execution worker")
    result = results[0]
    if result["round_id"] != round_id or result["mode"] != mode:
        raise RuntimeError("Worker did not confirm the requested collection round")
    expected_events = [
        int(code, 0) for code in kwargs.get("codes", "").split(",") if code.strip()
    ]
    expected_names = [
        name.strip() for name in kwargs.get("names", "").split(",") if name.strip()
    ]
    if result["events"] != expected_events or result["names"] != expected_names:
        raise RuntimeError("Worker confirmed a different event group")
    identity = {key: result[key] for key in ("pid", "tid")}
    if identity_path.exists():
        if json.loads(identity_path.read_text()) != identity:
            raise RuntimeError("Model execution PID/TID changed between rounds")
    else:
        identity_path.write_text(json.dumps(identity) + "\n")
    if not stop:
        post(f"{base_url}/resume", {}, timeout)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("round_id")
    parser.add_argument("identity_path", type=Path)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--timeout", type=float, default=360)
    args = parser.parse_args()
    result = switch(
        args.base_url, args.round_id, args.identity_path, args.stop, args.timeout
    )
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
