"""Small non-vLLM workload with two measured regions."""

import argparse

from topdown import measure


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=6)
    args = parser.parse_args()
    result = 0
    for _ in range(args.iterations):
        with measure("prepare"):
            values = list(range(40000))
        with measure("compute"):
            result += sum(value * value for value in values)
    if result <= 0:
        raise RuntimeError("Workload did not produce a result")


if __name__ == "__main__":
    main()
