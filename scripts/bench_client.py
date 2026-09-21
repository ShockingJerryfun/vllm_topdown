# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Keep vLLM's benchmark inputs and Python runtime alive across collection rounds."""

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path

LOGGER = logging.getLogger(__name__)


async def serve(
    state: Path,
    measure: Callable[[], Awaitable[dict]],
    identity: dict,
    stop: asyncio.Event,
) -> None:
    state.mkdir(parents=True, exist_ok=True)
    address = Path(f"/tmp/kperf-bench-{os.getpid()}.sock")
    lock = asyncio.Lock()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        async with lock:
            try:
                command = json.loads(await reader.readline())
                log = Path(command["log"])
                with (
                    log.open("x") as stream,
                    contextlib.redirect_stdout(stream),
                    contextlib.redirect_stderr(stream),
                ):
                    result = await measure()
                reply = {"status": "ok", "pid": os.getpid(), "result": result}
            except (OSError, ValueError, RuntimeError) as exc:
                LOGGER.exception("Benchmark request failed")
                reply = {"status": "error", "error": str(exc)}
                stop.set()
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(address))
    address.chmod(0o600)
    ready = state / "ready.json"
    temporary = ready.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({**identity, "pid": os.getpid(), "socket": str(address)})
    )
    temporary.replace(ready)
    try:
        async with server:
            await stop.wait()
    finally:
        address.unlink(missing_ok=True)
        ready.unlink(missing_ok=True)


async def run_service(state: Path, arguments: list[str]) -> None:
    # Keep heavy imports in the service process, outside the IPC client path.
    from vllm.benchmarks import serve as benchmark
    from vllm.utils.argparse_utils import FlexibleArgumentParser

    parser = FlexibleArgumentParser()
    benchmark.add_cli_args(parser)
    args = parser.parse_args(arguments)
    prepared = await benchmark.prepare_benchmark(args)
    requests = prepared.kwargs["input_requests"]
    encoded = json.dumps(
        [(row.prompt, row.prompt_len, row.expected_output_len) for row in requests],
        ensure_ascii=False,
    ).encode()
    identity = {
        "arguments": arguments,
        "inputs_sha256": hashlib.sha256(encoded).hexdigest(),
        "input_count": len(requests),
    }
    (state / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def measure() -> dict:
        return await benchmark.main_async(args, prepared)

    await serve(state, measure, identity, stop)


def request(state: Path, log: Path) -> dict:
    ready = json.loads((state / "ready.json").read_text())
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(300)
        connection.connect(ready["socket"])
        connection.sendall((json.dumps({"log": str(log)}) + "\n").encode())
        with connection.makefile("rb") as stream:
            response = json.loads(stream.readline())
    if response.get("status") != "ok" or response.get("pid") != ready["pid"]:
        raise RuntimeError(f"Persistent benchmark failed: {response}")
    log.with_suffix(".client.json").write_text(json.dumps(response, indent=2) + "\n")
    return response


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "request"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--log", type=Path)
    args, remaining = parser.parse_known_args()
    if args.mode == "serve":
        args.state.mkdir(parents=True, exist_ok=True)
        asyncio.run(run_service(args.state, remaining))
    else:
        if args.log is None or remaining:
            parser.error("request requires --log and accepts no benchmark arguments")
        request(args.state, args.log)


if __name__ == "__main__":
    main()
