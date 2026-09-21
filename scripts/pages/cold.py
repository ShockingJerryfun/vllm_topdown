# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Evict only verified task-owned copies before launching any copy users.

No source/original inode is advised. This does not scan all host processes;
the caller must ensure no prior task process is still using these copies.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path


def cold_copies(condition: Path) -> None:
    identity = json.loads((condition / "identity.json").read_text())
    copies = condition / "files"
    if not (copies / ".task_owned_copies").is_file():
        raise ValueError("Missing task-copy marker")
    for record in identity["files"]:
        path = copies / record["path"].lstrip("/")
        status = path.lstat()
        if (status.st_dev, status.st_ino) != (
            record["copy_device"],
            record["copy_inode"],
        ):
            raise ValueError(f"Copy inode changed: {path}")
        if not path.is_file() or path.is_symlink() or status.st_nlink != 1:
            raise ValueError(f"Not an independent ordinary copy: {path}")
        with path.open("rb") as stream:
            os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    logging.info(
        "Advised only %s task-owned copies; prefault must verify cold ranges",
        len(identity["files"]),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("condition", type=Path)
    args = parser.parse_args()
    cold_copies(args.condition)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
