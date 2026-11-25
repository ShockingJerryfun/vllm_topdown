"""Run the prepare, collect and analysis steps declared by a configuration."""

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
LOGGER = logging.getLogger(__name__)


def run(config_path: Path) -> str:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    pipeline = config.pop("pipeline")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    runtime_parent = ROOT / "results/.runtime"
    runtime_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=runtime_parent) as directory:
        context = Path(directory) / "context.json"
        context.write_text(
            json.dumps({"root": str(ROOT), "run_id": run_id,
                        "runtime": directory, "settings": config}),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["INFRAPERF_CONTEXT"] = str(context)
        env["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(ROOT), env.get("PYTHONPATH", "")) if value
        )
        try:
            for step in ("prepare", "collect", "analyze"):
                LOGGER.info("%s: %s", run_id, step)
                module = ".".join(Path(pipeline[step]).with_suffix("").parts)
                subprocess.run([sys.executable, "-m", module], env=env, check=True)
        finally:
            module = ".".join(Path(pipeline["cleanup"]).with_suffix("").parts)
            cleanup = subprocess.run(
                [sys.executable, "-m", module], env=env, check=False
            )
            if cleanup.returncode:
                raise RuntimeError(f"Cleanup failed for {run_id}")
    return run_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    LOGGER.info("Run: %s", run(args.config.resolve()))


if __name__ == "__main__":
    main()
