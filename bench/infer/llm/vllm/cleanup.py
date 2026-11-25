"""Stop the container created for this run."""

import json
import os
import subprocess
from pathlib import Path


def main() -> None:
    context = json.loads(Path(os.environ["INFRAPERF_CONTEXT"]).read_text())
    marker = Path(context["runtime"]) / "container"
    if marker.exists():
        container = marker.read_text(encoding="utf-8")
        subprocess.run(["docker", "stop", "--time", "10", container],
                       check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
