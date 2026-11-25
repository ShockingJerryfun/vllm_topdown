"""Print a platform's configured groups for shell adapters."""

import argparse
import sys

from .runner import load_groups


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=("920b", "zen4"))
    args = parser.parse_args()
    for label, events in load_groups(args.profile).items():
        sys.stdout.write(f"{label}|{','.join(events)}\n")


if __name__ == "__main__":
    main()
