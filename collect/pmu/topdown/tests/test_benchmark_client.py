"""Verify the persistent benchmark client uses the configured temporary directory."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from adapters.vllm.scripts.bench_client import request, serve


def test_benchmark_socket_uses_tmpdir_and_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with tempfile.TemporaryDirectory(prefix="td-", dir="/tmp") as tmp_dir:
        monkeypatch.setenv("TMPDIR", tmp_dir)
        monkeypatch.setattr(tempfile, "tempdir", None)
        state = tmp_path / "state"

        async def exercise() -> None:
            stop = asyncio.Event()

            async def measure() -> dict[str, int]:
                return {"completed": 1}

            task = asyncio.create_task(serve(state, measure, {"input_count": 1}, stop))
            while not (state / "ready.json").exists():
                if task.done():
                    await task
                await asyncio.sleep(0.01)
            socket_path = Path(json.loads((state / "ready.json").read_text())["socket"])
            assert socket_path.parent == Path(tmp_dir) and socket_path.exists()
            response = await asyncio.to_thread(request, state, tmp_path / "benchmark.log")
            assert response["result"]["completed"] == 1
            stop.set()
            await task
            assert not socket_path.exists() and not (state / "ready.json").exists()

        asyncio.run(asyncio.wait_for(exercise(), timeout=5))
