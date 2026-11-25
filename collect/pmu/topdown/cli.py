"""Command-line entry point for standalone workload collection."""

import argparse
import logging
from pathlib import Path

from .runner import collect

LOGGER = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("920b", "zen4"), default="920b")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--discard", type=int, default=0)
    parser.add_argument("--expected-samples", type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = collect(command, args.output.resolve(), args.profile, args.discard, args.expected_samples)
    LOGGER.info("completed %d rounds: %s", len(result["rounds"]), args.output)


if __name__ == "__main__":
    main()
