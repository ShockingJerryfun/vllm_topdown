# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Fourteen condition-named experiments over run_topdown.sh; two shell entrypoints."""

import argparse
import fcntl
import json
import logging
import os
import shlex
import signal
import subprocess
from pathlib import Path

if __package__:
    from .resume import archive, digest, save
else:
    from resume import archive, digest, save

LOGGER = logging.getLogger(__name__)


def conditions(mode: str) -> list[dict]:
    rows = []
    if mode == "4k":
        rows += [
            dict(model=model, mode=mode, mhz=None, place="near")
            for model in ("Qwen3_8B", "GLM_4_7_Flash_4bit")
        ]
    rows += [
        dict(model="Qwen3_8B", mode=mode, mhz=mhz, place=place)
        for mhz in (2100, 2300, 2500)
        for place in ("near", "far")
    ]
    for row in rows:
        freq = "default" if row["mhz"] is None else str(row["mhz"])
        row["id"] = f"{row['model']}_{mode}_core{freq}_{row['place']}"
    return rows


def run(command: list[str], **kwargs) -> str:
    return subprocess.check_output(command, text=True, **kwargs).strip()


class Controls:
    """Restore only the CPUFreq policies and exec-page knob this invocation changes."""

    def __init__(self, journal: Path, cpu_root: Path, thp: Path, boot: str):
        self.journal, self.cpu_root, self.thp, self.boot = journal, cpu_root, thp, boot
        self.state = {"boot": boot, "policies": {}, "thp": None}
        if journal.exists():
            previous = json.loads(journal.read_text())
            if previous["boot"] == boot:
                self.state = previous
                self.restore()
            else:
                # CPUFreq/sysfs controls are recreated by the kernel at reboot.
                archive(journal)

    def persist(self) -> None:
        save(self.journal, self.state)

    def policies(self, cpus: str) -> list[Path]:
        return sorted(
            {(self.cpu_root / f"cpu{cpu}/cpufreq").resolve() for cpu in cpus.split(",")}
        )

    def supported(self, cpus: str, mhz: int) -> bool:
        target = mhz * 1000
        for policy in self.policies(cpus):
            if (
                "userspace"
                not in (policy / "scaling_available_governors").read_text().split()
            ):
                return False
            minimum = int((policy / "cpuinfo_min_freq").read_text())
            maximum = int((policy / "cpuinfo_max_freq").read_text())
            if not minimum <= target <= maximum:
                return False
            choices = policy / "scaling_available_frequencies"
            if (
                choices.exists()
                and choices.read_text().strip()
                and target not in [int(v) for v in choices.read_text().split()]
            ):
                return False
        return True

    @staticmethod
    def bounds(policy: Path, minimum: str, maximum: str) -> None:
        # Raise max first when moving above the old ceiling, otherwise lower min first.
        order = [("scaling_min_freq", minimum), ("scaling_max_freq", maximum)]
        if int(minimum) > int((policy / "scaling_max_freq").read_text()):
            order.reverse()
        for name, value in order:
            (policy / name).write_text(value + "\n")

    def frequency(self, cpus: str, mhz: int) -> None:
        if not self.supported(cpus, mhz):
            raise ValueError(f"CPUFreq does not expose {mhz} MHz on CPUs {cpus}")
        target = str(mhz * 1000)
        for policy in self.policies(cpus):
            original = {
                name: (policy / name).read_text().strip()
                for name in ("scaling_governor", "scaling_min_freq", "scaling_max_freq")
            }
            if original["scaling_governor"] == "userspace":
                original["scaling_setspeed"] = (
                    (policy / "scaling_setspeed").read_text().strip()
                )
            self.state["policies"].setdefault(str(policy), original)
            self.persist()  # journal before the first write, including partial writes
            (policy / "scaling_governor").write_text("userspace\n")
            self.bounds(policy, target, target)
            (policy / "scaling_setspeed").write_text(target + "\n")
            for name in ("scaling_min_freq", "scaling_max_freq", "scaling_setspeed"):
                if int((policy / name).read_text()) != int(target):
                    raise ValueError(
                        f"CPUFreq did not accept {target}: {policy}/{name}"
                    )

    def exec_pages(self, value: str) -> None:
        if self.state["thp"] is None:
            self.state["thp"] = self.thp.read_text().strip()
            self.persist()
        self.thp.write_text(value + "\n")

    def restore(self) -> None:
        if self.state["thp"] is not None:
            self.thp.write_text(self.state["thp"] + "\n")
            self.state["thp"] = None
            self.persist()
        for path, original in list(self.state["policies"].items()):
            policy = Path(path)
            self.bounds(
                policy, original["scaling_min_freq"], original["scaling_max_freq"]
            )
            if "scaling_setspeed" in original:
                (policy / "scaling_governor").write_text("userspace\n")
                (policy / "scaling_setspeed").write_text(
                    original["scaling_setspeed"] + "\n"
                )
            (policy / "scaling_governor").write_text(
                original["scaling_governor"] + "\n"
            )
            for name, value in original.items():
                if (policy / name).read_text().strip() != value:
                    raise RuntimeError(f"Frequency restore failed: {policy}/{name}")
            del self.state["policies"][path]
            self.persist()
        if self.journal.exists():
            self.journal.unlink()


def owned_call(command: list[str], log: Path) -> None:
    """Forward cancellation to the collector and wait for its existing cleanup."""
    with log.open("a") as stream:
        process = subprocess.Popen(
            command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            rc = process.wait()
        finally:
            if process.poll() is None:
                # Signal the supervisor, which owns cleanup of its descendants.
                process.send_signal(signal.SIGTERM)
                process.wait()
        if rc:
            raise subprocess.CalledProcessError(rc, command)


def config_values(env: dict, row: dict, project: Path, condition: Path | None) -> dict:
    place = row["place"].upper()
    model = "QWEN" if row["model"] == "Qwen3_8B" else "GLM"
    values = {
        "CHIP": "950",
        "PROJECT": str(project),
        "CONTAINER": env["CONTAINER"],
        "MODEL": env[f"{model}_MODEL"],
        "MODEL_SHORT": row["model"],
        "SERVED_MODEL": row["model"],
        "PYTHON_BIN": env["PYTHON_BIN"],
        "VLLM_BIN": env["VLLM_BIN"],
        "VLLM_SITE": env["VLLM_SITE"],
        "HOST_PYTHON": env["HOST_PYTHON"],
        "SUBREAPER_BIN": env["SUBREAPER_BIN"],
        "SPE_BINARY_CACHE": env["SPE_BINARY_CACHE"],
        "EXPERIMENT_LOCK": env["EXPERIMENT_LOCK"],
        "DEVKIT_BIN": env["DEVKIT_BIN"],
        "WORKER_CPUS": env[f"{place}_CPUS"],
        "WORKER_POOL_CPUS": env[f"{place}_POOL"],
        "WORKER_NUMA_NODE": env[f"{place}_NODE"],
        "SERVICE_CPUS": env["SERVICE_CPUS"],
        "CLIENT_CPUS": env["CLIENT_CPUS"],
        "PLACEMENT_MODE": "worker_set",
        "HOTSPOT_SCOPE": "thread",
        "COLLECTION_PROFILE": "full",
        "RESUME_COLLECTION": "1",
        "SPE_ENABLE": "1",
        "FREQUENCY_ENABLE": "1",
        "GPU_ID": env["GPU_ID"],
        "PORT": env["PORT"],
        "GPU_MEMORY_UTILIZATION": env[f"{model}_GPU_MEMORY"],
        "SERVER_FLAGS": "--no-async-scheduling",
        "RANDOM_INPUT_LEN": "7000",
        "RANDOM_OUTPUT_LEN": "100",
        "MAX_MODEL_LEN": "16384",
        "NUM_WARMUPS": "0",
        "ROUND_WARMUPS": "1",
        "WARMUP_SCOPE": "per_group",
        "GROUP_START_TEMPERATURE": "0",
        "GPU_THERMAL_LIMIT": "0",
        "CODE_PAGE_CONDITION": str(condition) if condition else "",
        "CODE_PAGE_MODE": row["mode"] if condition else "",
        "NATIVE_CODE_PAGE_CHECK": "0" if condition else "1",
    }
    if condition:
        values.update(
            CODE_PAGE_COVERAGE_POLICY="strict",
            CODE_PAGE_RESIDENCY_POLICY="observe",
            CODE_PAGE_AUDIT_MODE="strict",
        )
    return values


def write_config(path: Path, values: dict) -> None:
    text = "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
    # Preserve prior inputs internally if a condition is reconfigured.
    if path.exists() and path.read_text() != text:
        history = path.parent / ".history/original_config.env"
        history.parent.mkdir(parents=True, exist_ok=True)
        if not history.exists():
            history.write_bytes(path.read_bytes())
    path.write_text(text)


def prepare_pages(
    env: dict, project: Path, root: Path, row: dict, controls: Controls
) -> Path:
    name = f"{row['model']}_{row['mode']}_{row['place']}"
    state = Path(env["STATE_ROOT"])
    condition = state / "conditions" / name
    tools = project / "scripts/pages"
    # Inventory comes from this host's actual Qwen baseline Worker, not server 84.
    maps = root / "Qwen3_8B_4k_coredefault_near/run/spe/evidence/before/maps"
    if not maps.is_file():
        raise ValueError(f"Finish the Qwen default baseline first; missing {maps}")
    for binary in ("prefault", "namespace_exec", "process_pages"):
        if not (tools / binary).exists():
            subprocess.run(
                [
                    "cc",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    str(tools / f"{binary}.c"),
                    "-o",
                    str(tools / binary),
                ],
                check=True,
            )
    guard = tools / "code_guard.so"
    if not guard.exists():
        subprocess.run(
            [
                "cc",
                "-O2",
                "-shared",
                "-fPIC",
                "-fvisibility=hidden",
                "-fno-builtin",
                "-fno-stack-protector",
                "-mno-outline-atomics",
                "-nostdlib",
                "-Wl,-z,defs",
                str(tools / "code_guard.c"),
                "-o",
                str(guard),
            ],
            check=True,
        )
    place = row["place"].upper()
    cpu, node = env[f"{place}_CPUS"].split(",")[0], env[f"{place}_NODE"]
    if not (condition / "identity.json").exists():
        archive(condition)
        data_paths = state / "locale_paths.txt"
        paths = {
            line.split(maxsplit=5)[5]
            for line in maps.read_text().splitlines()
            if len(line.split(maxsplit=5)) == 6
            and (line.endswith("/LC_CTYPE") or line.endswith("/gconv-modules.cache"))
        }
        data_paths.write_text("".join(path + "\n" for path in sorted(paths)))
        subprocess.run(
            [
                "docker",
                "exec",
                "-w",
                "/tmp",
                env["CONTAINER"],
                "numactl",
                f"--physcpubind={cpu}",
                f"--membind={node}",
                env["PYTHON_BIN"],
                str(tools / "prepare.py"),
                "--maps",
                str(maps),
                "--data-paths",
                str(data_paths),
                "--code-guard",
                str(guard),
                "--output",
                str(condition),
            ],
            check=True,
        )
    subprocess.run(
        [env["HOST_PYTHON"], str(tools / "cold.py"), str(condition)], check=True
    )
    controls.exec_pages("0x2" if row["mode"] == "64k" else "0x0")
    try:
        subprocess.run(
            [
                str(tools / "prefault"),
                row["mode"],
                cpu,
                node,
                str(condition / "segments.tsv"),
                str(condition / "files"),
                str(condition / "prefault.tsv"),
                "1",
            ],
            check=True,
        )
    finally:
        # Restore the switch before measurement; private copies retain their folios.
        controls.thp.write_text(controls.state["thp"] + "\n")
        controls.state["thp"] = None
        controls.persist()
    return condition


def accepted(folder: Path, settings: dict | None = None) -> bool:
    receipt = folder / "batch_complete.json"
    if not receipt.exists():
        return False
    value = json.loads(receipt.read_text())
    if settings is not None and value["settings"] != settings:
        raise ValueError(f"Settings changed for completed condition: {folder}")
    for name, sha in value["outputs"].items():
        if not (folder / name).is_file() or digest(folder / name) != sha:
            raise ValueError(f"Completed output changed: {folder / name}")
    return value["status"] == "pass"


def initialize_results(root: Path) -> None:
    all_rows = conditions("4k") + conditions("64k")
    expected = {row["id"] for row in all_rows}
    if root.exists() and any(path.name not in expected for path in root.iterdir()):
        raise ValueError("Result root must contain only the fourteen condition folders")
    for row in all_rows:
        (root / row["id"]).mkdir(parents=True, exist_ok=True)


def execute(env: dict, mode: str) -> int:
    root, project = Path(env["BATCH_ROOT"]), Path(env["PROJECT"])
    state = Path(env["STATE_ROOT"])
    state.mkdir(parents=True, exist_ok=True)
    initialize_results(root)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    controls = Controls(
        state / "restore.json",
        Path("/sys/devices/system/cpu"),
        Path("/sys/kernel/mm/transparent_hugepage/thp_exec_enabled"),
        boot,
    )
    failures = []
    try:
        for row in conditions(mode):
            folder = root / row["id"]
            folder.mkdir(parents=True, exist_ok=True)
            settings = config_values(env, row, project, None)
            settings.pop("PROJECT")
            if accepted(folder, settings):
                LOGGER.info("已完成，跳过：%s", row["id"])
                continue
            cpus = env[f"{row['place'].upper()}_CPUS"]
            if row["mhz"] and not controls.supported(cpus, row["mhz"]):
                save(folder / "status.json", {"status": "unsupported_frequency", **row})
                failures.append(row["id"])
                LOGGER.warning(
                    "当前驱动不支持 %s MHz，保留待测：%s", row["mhz"], row["id"]
                )
                continue
            # Leave other GPU workloads untouched. Temperature is recorded only.
            pids = run(
                [
                    "nvidia-smi",
                    "--query-compute-apps=pid",
                    "--format=csv,noheader,nounits",
                ]
            )
            if pids:
                raise RuntimeError(f"GPU occupied (PIDs {pids}); rerun when available")
            if (
                run(["docker", "inspect", "-f", "{{.State.Running}}", env["CONTAINER"]])
                != "true"
            ):
                subprocess.run(["docker", "start", env["CONTAINER"]], check=True)
            tasks = run(["docker", "top", env["CONTAINER"], "-eo", "pid"]).splitlines()
            if len(tasks) != 2:
                raise RuntimeError("Container has live tasks; leave them untouched")
            LOGGER.info("采集：%s", row["id"])
            try:
                # Share the collector lock during page and CPUFreq preparation.
                lock_path = Path(env["EXPERIMENT_LOCK"])
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    page = (
                        prepare_pages(env, project, root, row, controls)
                        if row["mhz"]
                        else None
                    )
                    if row["mhz"]:
                        controls.frequency(cpus, row["mhz"])
                values = config_values(env, row, project, page)
                write_config(folder / "config.env", values)
                owned_call(
                    [
                        "bash",
                        str(project / "scripts/run_topdown.sh"),
                        str(folder / "config.env"),
                        str(folder / "run"),
                    ],
                    folder / "console.log",
                )
                frequency = json.loads(
                    (folder / "run/frequency/summary.json").read_text()
                )
                if row["mhz"]:
                    measured = list(frequency["per_core_mhz"].values())
                    if any(abs(value / row["mhz"] - 1) > 0.05 for value in measured):
                        raise ValueError(
                            f"Observed core frequency differs from target: {measured}"
                        )
                if page is None:
                    for endpoint in ("before", "after"):
                        proof = (
                            folder
                            / f"run/spe/evidence/{endpoint}/native_code_pages.json"
                        )
                        if (
                            not proof.exists()
                            or json.loads(proof.read_text())["status"] != "pass"
                        ):
                            raise ValueError(
                                f"Native 4K backing not established: {proof}"
                            )
                save(
                    folder / "batch_complete.json",
                    {
                        "status": "pass",
                        **row,
                        "measured_frequency": frequency,
                        "settings": settings,
                        "outputs": {
                            str(path.relative_to(folder)): digest(path)
                            for path in [
                                folder / "run/complete.json",
                                Path(
                                    json.loads(
                                        (folder / "run/complete.json").read_text()
                                    )["report"]
                                ),
                            ]
                        },
                        "pages": "verified_private_copies"
                        if page
                        else "native_4k_observed_at_spe_endpoints",
                    },
                )
                save(folder / "status.json", {"status": "complete", **row})
            finally:
                controls.restore()
        save(
            state / f"{mode}_status.json",
            {
                "pending": failures,
                "completed": [
                    row["id"] for row in conditions(mode) if accepted(root / row["id"])
                ],
            },
        )
    finally:
        controls.restore()
    return 2 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("4k", "64k"))
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--setup", action="store_true")
    args = parser.parse_args()
    if args.plan:
        for row in conditions(args.mode):
            LOGGER.info("%s/%s", os.environ["BATCH_ROOT"], row["id"])
        return

    if args.setup:
        initialize_results(Path(os.environ["BATCH_ROOT"]))
        LOGGER.info("准备完成：14 个结果目录；未启动推理。")
        return

    def interrupt(signum, _frame):
        raise InterruptedError(f"Interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    lock_path = Path(os.environ["STATE_ROOT"]) / "batch.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raise SystemExit(execute(dict(os.environ), args.mode))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
